"""Validate a browser-saved NCCPL MTS export against what this hypothesis actually needs.

Run it on the file the user saves from https://www.nccpl.com.pk/market-information
(Export button) before anyone touches psx_data_v2.py:

    python amendments/check_mts_export.py --file "C:\\Users\\110computer\\Downloads\\MTS.xlsx"

This is a validator, not an ingester. It never writes to psx.db, because until the source
passes here there is nothing to justify changing the registered feed.

Required columns come from mts_engine, not from preference:
  REQUIRED_FIELDS      = raw_symbol, mts_volume, open_pct   (parse aborts without them)
  ALLOWED_SIGNAL_COLS  = open_pct, mts_volume, mts_amount   (SPEC signal.column = open_pct)
  weighted_rate                                              diagnostic only, never a signal
"""

import argparse
import re
import sys
from pathlib import Path

import pandas as pd

# What SPEC["signal"]["column"] = "open_pct" must be derived from: MTS open position as a
# percentage of something exchange-published. Without a percentage column the registered
# signal cannot be built from this source at all.
NEEDS = [
    ("raw_symbol",  r"symbol|scrip|security",                       True,  "which stock"),
    ("mts_volume",  r"net open.*vol|open position.*vol",            True,  "outstanding financed shares"),
    ("mts_amount",  r"net open.*(amount|value)|open position.*(value|amount)", False, "outstanding financed rupees"),
    ("open_pct",    r"percent|%|pct|free float|float",              True,  "THE SIGNAL: open position as a %"),
    ("report_date", r"date|as of|report",                           True,  "point-in-time key"),
    ("weighted_rate", r"weight|markup|rate|margin rate",            False, "diagnostic only"),
]

FROZEN_BEFORE = "2026-10-01"   # SPEC freeze_date: data before this can never enter the signal


def read_any(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xls"):
        return pd.read_excel(path)
    if suffix in (".csv", ".txt"):
        return pd.read_csv(path)
    if suffix in (".json",):
        return pd.json_normalize(pd.read_json(path).to_dict("records"))
    if suffix in (".html", ".htm"):
        tables = pd.read_html(path)
        if not tables:
            raise RuntimeError("no <table> found in the saved page")
        return max(tables, key=lambda t: t.shape[0] * t.shape[1])
    raise RuntimeError(f"unsupported file type {suffix!r}; save as xlsx, csv or filtered html")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    a = ap.parse_args()

    path = Path(a.file)
    if not path.exists():
        print(f"[!] file not found: {path}")
        return 2

    df = read_any(path)
    print(f"[+] parsed {path.name}: {df.shape[0]} rows x {df.shape[1]} columns")
    print("    columns as printed by NCCPL:")
    for c in df.columns:
        print(f"      - {str(c)[:80]!r}")

    norm = {re.sub(r"\s+", " ", str(c)).strip().lower(): c for c in df.columns}

    def find(rx: str):
        return [orig for n, orig in norm.items() if re.search(rx, n)]

    print("\n=== FIELD CONTRACT ===")
    missing_required = []
    for field, rx, required, why in NEEDS:
        hits = find(rx)
        tag = "REQUIRED" if required else "optional"
        if hits:
            print(f"  [OK]   {field:14} ({tag:8}) <- {hits[0]!r}")
            for extra in hits[1:]:
                print(f"         {'':14} {'':8}  also matches {extra!r} - confirm which one is meant")
        else:
            print(f"  [MISS] {field:14} ({tag:8}) {why}")
            if required:
                missing_required.append(field)

    print("\n=== POINT-IN-TIME / PEEK FIREWALL ===")
    date_col = find(r"date|as of|report")
    if date_col:
        dates = pd.to_datetime(df[date_col[0]], errors="coerce").dropna()
        if len(dates):
            lo, hi = dates.min().date().isoformat(), dates.max().date().isoformat()
            pre = int((dates.dt.date < pd.Timestamp(FROZEN_BEFORE).date()).sum())
            print(f"  report dates {lo} .. {hi} | rows before {FROZEN_BEFORE}: {pre}/{len(dates)}")
            if pre:
                print(f"  NOTE: pre-{FROZEN_BEFORE} rows are usable ONLY for placebo/MDE sizing.")
                print("        They must never be loaded into mts_snapshots: the signal is a")
                print("        cross-sectional rank, so historical rows would peek at the outcome.")
        else:
            print("  could not parse any date column")
    else:
        print("  [MISS] no date column: this source cannot support a point-in-time panel at all")

    print("\n=== VERDICT ===")
    if missing_required:
        print(f"  NO-GO. Missing required field(s): {missing_required}")
        print("  The registered signal cannot be built from this export. Do NOT change the feed.")
        print("  If this is the only MTS source available, the signal definition needs a")
        print("  pre-data amendment (a methodology change), which must be reviewed before use.")
        return 1
    print("  GO on columns. Next gate before Amendment #18 is written:")
    print("    1. same export on 5 consecutive sessions, with the printed date advancing")
    print("    2. symbol names matching the 138-name eligible universe (no silent drops)")
    print("    3. open_pct values inside [0, 100] and a plausible distribution")
    print("  Nothing was written to psx.db by this check.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
