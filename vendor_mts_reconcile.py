"""vendor_mts_reconcile.py - prove a vendor's MTS column IS the NCCPL column, before anything
else is done with a vendor token.

Why this exists and why it comes first: the whole signal is `open_pct` ("MTS Open Percentage" in
the NCCPL PDF). Capital Stake documents a `pct` field described as "percentage of margin
utilization". They look like the same thing and may not be. Joining 240 sessions of history to
the wrong column would not be a small error - it would silently define the hypothesis. So the
order is fixed: compare two dates we already hold as PDFs (2026-09-14 and 2026-09-24), show the
match table, and only then let any rule or amendment talk about the vendor field.

Deliberate limits, all of them safety:
  * No network code. The vendor file is supplied by the human; nothing here can poll a vendor API.
  * Read-only on psx.db (mode=ro). It writes no rows and imports nothing that could.
  * Refuses any report date outside the two authorised ones. Comparing extra history "while we
    are at it" is how pre-registration dies, so it needs an explicit --allow-other-dates.
  * Never prints, reads or needs a token.

Usage:
  python vendor_mts_reconcile.py --vendor-file scratch/vendor_2026-09-14.csv --report-date 2026-09-14
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd

AUTHORISED_DATES = ("2026-09-14", "2026-09-24")

# vendor field -> our column. Defaults come from the Capital Stake docs page; --map overrides.
FIELD_MAP = {"novol": "mts_volume", "nomad": "mts_amount", "pct": "open_pct", "wave": "weighted_rate"}

# Tolerances are stated in advance, not fitted to whatever the data shows.
# volume: integer shares, must match exactly. amount: 2-dp currency, 1 rupee slack.
# rates: both published to 2 decimals, 0.011 slack so a rounding difference is not a mismatch.
TOL = {"mts_volume": 0.0, "mts_amount": 1.0, "open_pct": 0.011, "weighted_rate": 0.011}


def read_vendor(path: Path) -> pd.DataFrame:
    s = path.suffix.lower()
    if s == ".csv":
        df = pd.read_csv(path, engine="python")
    elif s in (".xlsx", ".xls"):
        df = pd.read_excel(path)
    elif s == ".json":
        df = pd.DataFrame(json.loads(path.read_text(encoding="utf-8")))
    else:
        raise SystemExit(f"unsupported vendor file type {s!r}")
    df.columns = [str(c).strip().lower() for c in df.columns]
    return df


def num(series) -> pd.Series:
    return pd.to_numeric(series.astype(str).str.replace(",", "", regex=False), errors="coerce")


def load_ground_truth(report_date: str) -> pd.DataFrame:
    conn = sqlite3.connect("file:psx.db?mode=ro", uri=True)
    try:
        df = pd.read_sql_query(
            "SELECT symbol, raw_symbol, mts_volume, mts_amount, weighted_rate, open_pct, "
            "data_as_of FROM mts_snapshots WHERE report_date=?",
            conn, params=(report_date,))
    finally:
        conn.close()
    if df.empty:
        raise SystemExit(f"no archived NCCPL rows for report_date {report_date} - nothing to "
                         f"compare against. Authorised dates: {list(AUTHORISED_DATES)}")
    return df


def compare(vendor: pd.DataFrame, truth: pd.DataFrame, mapping: dict, key: str) -> tuple[list[str], bool]:
    # pct_ok starts False and is only earned by an open_pct comparison that actually ran and
    # found zero differences. A guard whose default is "pass" reports success while checking
    # nothing - which is exactly what an earlier version of this function did.
    out, pct_ok = [], False
    v = pd.DataFrame({key: vendor[key].astype(str).str.strip().str.upper()})
    for src, ours in mapping.items():
        if src not in vendor.columns:
            out.append(f"  {ours:<14} <- vendor '{src}': NOT PRESENT in the file")
            continue
        v[ours] = num(vendor[src])

    j = v.merge(truth[[key, "open_pct", "mts_volume", "mts_amount", "weighted_rate"]], on=key,
                how="inner", suffixes=("_v", "_t"))
    out.append(f"  joined on '{key}': {len(j)} of {len(v)} vendor rows and {len(truth)} PDF rows")
    if len(j) < min(len(v), len(truth)) * 0.8:
        out.append("  WARNING: under 80% of symbols joined. Either the symbol conventions differ "
                   "(ex-dividend suffixes?) or this is not the same report.")
        return out, False

    for ours in ("open_pct", "mts_volume", "mts_amount", "weighted_rate"):
        av, at = f"{ours}_v", f"{ours}_t"
        if av not in j.columns or at not in j.columns:
            out.append(f"  {ours:<14} NOT COMPARED - no vendor field mapped to it")
            continue
        a, b = j[av], j[at]
        both = ~(a.isna() | b.isna())
        if not both.any():
            out.append(f"  {ours:<14} NOT COMPARED - every paired value is blank")
            continue
        diff = (a - b).abs()
        rel = (diff / b.where(b != 0)).replace([float("inf")], None)
        bad = both & (diff > TOL[ours])
        out.append(f"  {ours:<14} n={int(both.sum()):>3}  match={int((both & ~bad).sum()):>3}  "
                   f"differ={int(bad.sum()):>3}  max rel diff={float(rel[both].max()):.4%}")
        if ours == "open_pct" and int(bad.sum()) == 0 and int(both.sum()) >= 50:
            pct_ok = True
        if bad.any():
            worst = j.loc[bad, [key, av, at]].head(6)
            out.append(f"      {ours} mismatches (vendor vs NCCPL PDF):")
            for r in worst.itertuples(index=False):
                out.append(f"        {r[0]:<10} {r[1]:>10.4f}   {r[2]:>10.4f}")
    return out, pct_ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vendor-file", required=True)
    ap.add_argument("--report-date", required=True)
    ap.add_argument("--map", default="", help="e.g. 'pct=open_pct,novol=mts_volume'")
    ap.add_argument("--key", default="symbol")
    ap.add_argument("--allow-other-dates", action="store_true",
                    help="required to compare a date other than the two already held as PDFs")
    a = ap.parse_args()

    if a.report_date not in AUTHORISED_DATES and not a.allow_other_dates:
        raise SystemExit(
            f"REFUSED: {a.report_date} is not one of the two authorised reconciliation dates "
            f"{list(AUTHORISED_DATES)}.\n"
            "Those are the only dates we hold the NCCPL PDF for, so they are the only dates on "
            "which a vendor column can be PROVEN to match. Pulling further history through this "
            "tool would mean opening old data before the V4 rule is written and hash-anchored. "
            "Pass --allow-other-dates only on the user's explicit instruction.")

    path = Path(a.vendor_file)
    if not path.exists():
        raise SystemExit(f"file not found: {path}")

    mapping = dict(FIELD_MAP)
    for part in filter(None, (x.strip() for x in a.map.split(","))):
        src, ours = part.split("=", 1)
        mapping[src.strip().lower()] = ours.strip()

    vendor = read_vendor(path)
    truth = load_ground_truth(a.report_date)
    if a.key not in vendor.columns:
        raise SystemExit(f"vendor file has no '{a.key}' column. Columns: {list(vendor.columns)}")

    print(f"=== RECONCILIATION  vendor:{path.name}  vs  NCCPL PDF report_date={a.report_date} "
          f"({len(truth)} rows) ===")
    print(f"  field map: {mapping}")
    print(f"  tolerances: {TOL}")
    lines, pct_ok = compare(vendor, truth, mapping, a.key)
    print("\n".join(lines))

    dates = {str(d) for d in truth["data_as_of"].dropna().unique()}
    print(f"  NCCPL positions are as of: {sorted(dates) or ['not recorded on this report']}")
    print("\nVERDICT on the load-bearing column (pct vs open_pct):",
          "MATCH - safe to name it in an amendment" if pct_ok
          else "DOES NOT MATCH - do not write this into any spec or download history")
    if not pct_ok:
        print("  Next: ask the vendor what 'pct' is computed from. Do not guess a replacement "
              "column, and do not redefine the signal to fit the vendor.")
    return 0 if pct_ok else 2


if __name__ == "__main__":
    sys.exit(main())
