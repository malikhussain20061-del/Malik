"""vendor_mts_reconcile.py - prove a vendor's MTS column IS the NCCPL column, and find out what
the vendor means by a date, before anything else is done with a vendor token.

Why this exists and why it comes first: the whole signal is `open_pct` ("MTS Open Percentage" in
the NCCPL PDF). Capital Stake documents a `pct` field described as "percentage of margin
utilization". They look like the same thing and may not be. Joining 240 sessions of history to
the wrong column would not be a small error - it would silently redefine the hypothesis.

Two questions, answered in one run, because they are entangled:
  A. Is the vendor's `pct` the NCCPL open percentage?
  B. Does the vendor stamp a row with the REPORT date or with the date the positions are AS OF?
     NCCPL's own two dates differ: the 14-Sep report carries 11-Sep positions, and the 24-Sep
     report carries 23-Sep positions. Whichever convention the vendor uses changes every lag in
     V4, so this is measured, not assumed.

Deliberate limits, all of them safety:
  * No network code. The vendor file is supplied by the human; nothing here can poll a vendor API.
  * Read-only on psx.db (mode=ro). No INSERT/UPDATE/DELETE anywhere.
  * Refuses any date outside the four dates of the two reports we hold as PDFs. "Pull a few more
    days to be sure" is what opening old data before the V4 rule is written looks like in practice.
  * Never reads, writes or prints a token.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd

# The only ground truth we can show a vendor against: reports we archived ourselves as PDFs.
SNAPSHOTS = (
    {"report_date": "2026-09-14", "data_as_of": "2026-09-11"},
    {"report_date": "2026-09-24", "data_as_of": "2026-09-23"},
)
AUTHORISED_DATES = sorted({d for s in SNAPSHOTS for d in (s["report_date"], s["data_as_of"])})

# vendor field -> our column. Defaults come from the Capital Stake docs page; --map overrides.
FIELD_MAP = {"novol": "mts_volume", "nomad": "mts_amount", "pct": "open_pct", "wave": "weighted_rate"}

# Stated in advance, not fitted to whatever the data shows.
# volume: integer shares, exact. amount: 2-dp currency, 1 rupee slack. rates: both published to
# 2 decimals, so 0.011 is a rounding difference and not a mismatch.
TOL = {"mts_volume": 0.0, "mts_amount": 1.0, "open_pct": 0.011, "weighted_rate": 0.011}
MIN_PAIRS = 50


def read_vendor(path: Path) -> pd.DataFrame:
    s = path.suffix.lower()
    if s == ".csv":
        df = pd.read_csv(path, engine="python")
    elif s in (".xlsx", ".xls"):
        df = pd.read_excel(path)
    elif s == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        df = pd.DataFrame(payload if isinstance(payload, list) else payload.get("data", payload))
    else:
        raise SystemExit(f"unsupported vendor file type {s!r}")
    df.columns = [str(c).strip().lower() for c in df.columns]
    return df


def num(series) -> pd.Series:
    return pd.to_numeric(series.astype(str).str.replace(",", "", regex=False), errors="coerce")


def to_pkt_date(value):
    """Vendor docs: "All historical data is time-stamped in UTC", and the end-of-day endpoints
    return `time` as seconds since epoch, UTC. Pakistan is UTC+5, so a row stamped 19:00-23:59
    UTC belongs to the NEXT Pakistani calendar day. Reading that epoch as a UTC date shifts the
    row back one session - and this entire test is about which session was knowable when.

    Returns a YYYY-MM-DD string in Asia/Karachi, or None if the value is not a timestamp.
    """
    from datetime import datetime, timedelta, timezone
    PKT = timezone(timedelta(hours=5))
    s = str(value).strip()
    dt = None
    if s.isdigit() and len(s) >= 10:                       # epoch seconds (or ms)
        sec = int(s[:10])
        dt = datetime.fromtimestamp(sec, tz=timezone.utc)
    else:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                parsed = datetime.strptime(s.replace("+00:00", "").replace("Z", ""), fmt.rstrip("Z"))
                dt = parsed.replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    return dt.astimezone(PKT).date().isoformat()


def normalise_dates(df: pd.DataFrame, date_col: str | None) -> tuple[pd.DataFrame, list[str]]:
    """Turn whatever the vendor calls a date into a Pakistani calendar date, and say what was
    changed so the reader can disagree with it."""
    notes = []
    if date_col and date_col in df.columns:
        raw = df[date_col]
        if pd.api.types.is_numeric_dtype(raw) or raw.astype(str).str.contains(r"T|:\d\d", regex=True).any():
            converted = raw.map(to_pkt_date)
            if converted.notna().all():
                shifted = int((converted.astype(str) != raw.astype(str).str.slice(0, 10)).sum())
                notes.append(f"'{date_col}' converted from UTC timestamps to Asia/Karachi dates; "
                             f"{shifted} row(s) land on a different calendar day than the raw value")
                df = df.copy()
                df[date_col] = converted
            else:
                notes.append(f"'{date_col}' looked like a timestamp but could not be parsed - "
                             f"LEFT ALONE, check it by hand")
    return df, notes


def load_truth(report_date: str) -> pd.DataFrame:
    conn = sqlite3.connect("file:psx.db?mode=ro", uri=True)
    try:
        df = pd.read_sql_query(
            "SELECT symbol, mts_volume, mts_amount, weighted_rate, open_pct FROM mts_snapshots "
            "WHERE report_date=?", conn, params=(report_date,))
    finally:
        conn.close()
    if df.empty:
        raise SystemExit(f"no archived NCCPL rows for report_date {report_date}")
    return df


def probe_matrix(vendor: pd.DataFrame, truth: pd.DataFrame, key: str) -> list[str]:
    """Compare EVERY vendor numeric field against EVERY NCCPL column, instead of trusting the
    field map. Labels lie: the vendor documents 'wave' as "Weighted average price" while our
    column of the same idea holds a rate near 13.0, so matching on names would have been wrong in
    both directions. Field names are prefixed while comparing so a vendor column that happens to
    share our column's name cannot collide with it in the join.
    """
    ours = ["open_pct", "mts_volume", "mts_amount", "weighted_rate"]
    lines = ["      which vendor field actually equals which NCCPL column "
             "(differ count over joined symbols):"]
    vn = pd.DataFrame({key: vendor[key].astype(str).str.strip().str.upper()})
    for c in vendor.columns:
        if c != key:
            vn[f"x_{c}"] = num(vendor[c])
    j = vn.merge(truth[[key] + ours], on=key, how="inner")
    if j.empty:
        return lines + ["        (nothing joined, so no field identity can be probed)"]
    for col in ours:
        b = j[col]
        scored = []
        for vc in [c for c in vn.columns if c != key]:
            a = j[vc]
            both = ~(a.isna() | b.isna())
            if int(both.sum()) < MIN_PAIRS:
                continue
            bad = int(((a - b).abs() > TOL.get(col, 0.011))[both].sum())
            scored.append((bad, int(both.sum()), vc[2:]))
        if not scored:
            lines.append(f"        {col:<14} no vendor column had {MIN_PAIRS}+ paired values")
            continue
        scored.sort()
        shown = ", ".join(f"{vv}={d}differ/{n}pairs" for d, n, vv in scored[:3])
        exact = [vv for d, n, vv in scored if d == 0]
        lines.append(f"        {col:<14} {shown}"
                     + (f"   <- exact candidates: {exact}" if exact else ""))
    return lines


def compare(vendor: pd.DataFrame, truth: pd.DataFrame, mapping: dict, key: str) -> tuple[list[str], dict]:
    """Returns printed lines and a stats dict. stats['matched'] only becomes True by an
    open_pct comparison that actually ran clean over enough symbols."""
    out, stats = [], {"matched": False, "joined": 0, "pairs": 0, "differ": None}
    v = pd.DataFrame({key: vendor[key].astype(str).str.strip().str.upper()})
    for src, ours in mapping.items():
        if src not in vendor.columns:
            out.append(f"      {ours:<14} <- vendor field '{src}' NOT PRESENT")
            continue
        v[ours] = num(vendor[src])

    j = v.merge(truth[[key, "open_pct", "mts_volume", "mts_amount", "weighted_rate"]], on=key,
                how="inner", suffixes=("_v", "_t"))
    stats["joined"] = len(j)
    out.append(f"      joined on '{key}': {len(j)} vendor rows vs {len(truth)} PDF rows")
    if len(j) < min(len(v), len(truth)) * 0.8:
        out.append("      WARNING: under 80% joined - symbol conventions differ (ex-dividend "
                   "suffixes?) or this is not the same report. Not compared further.")
        return out, stats

    for ours in ("open_pct", "mts_volume", "mts_amount", "weighted_rate"):
        av, at = f"{ours}_v", f"{ours}_t"
        if av not in j.columns or at not in j.columns:
            out.append(f"      {ours:<14} NOT COMPARED - no vendor field mapped to it")
            continue
        a, b = j[av], j[at]
        both = ~(a.isna() | b.isna())
        if not both.any():
            out.append(f"      {ours:<14} NOT COMPARED - every paired value is blank")
            continue
        diff = (a - b).abs()
        rel = (diff / b.where(b != 0)).replace([float("inf")], None)
        bad = both & (diff > TOL[ours])
        out.append(f"      {ours:<14} n={int(both.sum()):>3}  match={int((both & ~bad).sum()):>3}  "
                   f"differ={int(bad.sum()):>3}  max rel diff={float(rel[both].max()):.4%}")
        if bad.any():
            for r in j.loc[bad, [key, av, at]].head(6).itertuples(index=False):
                out.append(f"        {ours} {r[0]:<10} vendor={r[1]:>10.4f}  NCCPL={r[2]:>10.4f}")
        if ours == "open_pct":
            stats["pairs"] = int(both.sum())
            stats["differ"] = int(bad.sum())
            stats["matched"] = int(bad.sum()) == 0 and int(both.sum()) >= MIN_PAIRS
    out += probe_matrix(vendor, truth, key)
    return out, stats


def convention(snap: dict, vendor_date: str, matched: bool) -> str:
    if not matched:
        return "no match on this report, so it says nothing about the date convention"
    if vendor_date == snap["report_date"] and vendor_date == snap["data_as_of"]:
        return "AMBIGUOUS - report date and as-of date are the same here"
    if vendor_date == snap["report_date"]:
        return f"vendor stamps the REPORT date ({snap['report_date']}), positions are as of " \
               f"{snap['data_as_of']} - a real lag exists"
    if vendor_date == snap["data_as_of"]:
        return f"vendor stamps the AS-OF date ({snap['data_as_of']}), report was published " \
               f"{snap['report_date']}"
    return f"vendor date {vendor_date} is neither of this report's dates - do not proceed"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vendor-file", required=True)
    ap.add_argument("--vendor-date", required=True,
                    help="the date the vendor stamps these rows with")
    ap.add_argument("--map", default="")
    ap.add_argument("--key", default="symbol")
    ap.add_argument("--date-col", default="date",
                    help="the vendor's own date/timestamp column, normalised to Asia/Karachi")
    ap.add_argument("--allow-other-dates", action="store_true",
                    help="required to use any date outside the two archived reports")
    a = ap.parse_args()

    if a.vendor_date not in AUTHORISED_DATES and not a.allow_other_dates:
        raise SystemExit(
            f"REFUSED: {a.vendor_date} is not one of the authorised reconciliation dates "
            f"{AUTHORISED_DATES}.\n"
            "Those four dates are the report dates and as-of dates of the only two NCCPL reports "
            "we hold as PDFs, so they are the only dates on which a vendor column can be PROVEN "
            "to match. Passing --allow-other-dates would mean opening older history before the "
            "V4 rule is written and hash-anchored; use it only on the user's explicit word.")

    path = Path(a.vendor_file)
    if not path.exists():
        raise SystemExit(f"file not found: {path}")

    mapping = dict(FIELD_MAP)
    for part in filter(None, (x.strip() for x in a.map.split(","))):
        src, ours = part.split("=", 1)
        mapping[src.strip().lower()] = ours.strip()

    vendor = read_vendor(path)
    if a.key not in vendor.columns:
        raise SystemExit(f"vendor file has no '{a.key}' column. Columns: {list(vendor.columns)}")
    vendor, notes = normalise_dates(vendor, a.date_col)

    print(f"=== VENDOR MTS RECONCILIATION  file={path.name}  vendor_date={a.vendor_date} ===")
    print(f"  field map: {mapping}")
    print(f"  tolerances: {TOL}   min paired symbols for a verdict: {MIN_PAIRS}")
    for n in notes:
        print(f"  [timezone] {n}")
    if a.date_col in vendor.columns:
        seen = sorted(set(vendor[a.date_col].astype(str).str.slice(0, 10)))
        print(f"  dates inside the file ({a.date_col}): {seen[:6]}"
              + (" ..." if len(seen) > 6 else ""))
        if a.vendor_date not in seen:
            print(f"  WARNING: --vendor-date {a.vendor_date} is not one of the dates in the file. "
                  f"If the file carries a timestamp, this is the UTC/PKT boundary - check the "
                  f"[timezone] line above before trusting any verdict.")

    results = []
    for snap in SNAPSHOTS:
        truth = load_truth(snap["report_date"])
        print(f"\n  against NCCPL report_date={snap['report_date']} "
              f"(positions as of {snap['data_as_of']}, {len(truth)} rows):")
        lines, stats = compare(vendor, truth, mapping, a.key)
        print("\n".join(lines))
        print(f"      date convention: {convention(snap, a.vendor_date, stats['matched'])}")
        results.append((snap, stats))

    hit = [(s, st) for s, st in results if st["matched"]]
    print("\n" + "=" * 72)
    if not hit:
        worst = ", ".join(f"{s['report_date']}: differ={st['differ']} n={st['pairs']}"
                          for s, st in results)
        print(f"VERDICT: NO report matched cleanly ({worst}).")
        print("  -> The vendor's pct is NOT proven to be NCCPL's MTS Open Percentage (or the "
              "wrong day was supplied). Do not write the field into any spec, and do not "
              "download history. Ask the vendor what pct is computed from.")
        print("  -> Do not guess a substitute column and do not redefine the signal to fit it.")
        return 2
    if len(hit) > 1:
        print(f"VERDICT: AMBIGUOUS - both reports match, so their values are indistinguishable. "
              f"Ask the vendor for a second date before concluding anything.")
        return 2

    snap, st = hit[0]
    print(f"VERDICT on the load-bearing column (pct vs open_pct): MATCH "
          f"({st['pairs']} symbols, 0 differences) against report_date={snap['report_date']}.")
    print(f"DATE CONVENTION: {convention(snap, a.vendor_date, True)}")
    print("  -> This is the input to V4's lag rule. Safe to name in an amendment only after the "
          "user has seen both lines above.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
