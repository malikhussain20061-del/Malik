"""backfill_manual.py - fill a missing trading day from a file the USER downloaded by hand.

Why manual: on 2026-09-24 dps.psx.com.pk/market-watch began returning 404 and /announcements
403, so the automated capture could not get that day's quotes. Downloading one copy of a PSX
file for personal use is inside the Terms of Use; continuing to scrape while asking for
permission is not.

This tool never guesses a file layout. Run --inspect first, read what it prints, then pass the
mapping explicitly. Nothing is written to psx.db without --apply, and --apply writes to a
scratch copy unless --live is also given.

Provenance is recorded rather than assumed away: the raw file is archived gzip + SHA-256, and
every inserted row carries quality_flags='MANUAL_DOWNLOAD:sha256=<first12>'.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "amendments"))

import mts_engine as ME
import psx_data_v2 as P
import repair_base_symbol as RB
import run_mts_h1 as R

PKT = ZoneInfo("Asia/Karachi")
FIELDS = ("symbol", "close", "volume", "ldcp", "open", "high", "low", "trade_date")
REQUIRED = ("symbol", "close", "volume")
# Footer/aggregate lines in PSX "all companies" downloads carry a label where a ticker
# belongs and a number in every numeric column, so the type filters below cannot drop them.
AGGREGATE_LABELS = {"TOTAL", "TOTALS", "SUM", "SUBTOTAL", "GRANDTOTAL", "OVERALL",
                    "KSE100", "KSEALLSHARE", "ALLSHARES", "OTHERS", "REST"}


NAME_HINTS = {"symbol", "stock", "scrip", "closing", "current", "price", "volume",
              "ldcp", "open", "high", "low", "sector", "previous", "traded", "company", "date"}


def _field_counts(path: Path) -> list[int]:
    """Field count per PHYSICAL line, blank lines as 0, so indexes match skiprows=."""
    import csv as _csv
    out = []
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as fh:
        for row in _csv.reader(fh):
            out.append(len(row) if row and any(c.strip() for c in row) else 0)
    return out


def read_any(path: Path) -> pd.DataFrame:
    s = path.suffix.lower()
    if s in (".xlsx", ".xls"):
        try:
            return pd.read_excel(path)
        except Exception:
            # PSX has historically served "xls" that is really an HTML table.
            tables = pd.read_html(path.read_bytes())
            print(f"[i] {s} is not a real Excel workbook; parsed it as HTML")
            return max(tables, key=lambda t: t.shape[0] * t.shape[1])
    if s == ".csv":
        # PSX CSVs put one or two title lines above the real header. Picking the wrong line
        # silently produces a table of nonsense, so the header is chosen by two hard facts:
        # it must have the same field count as the data rows, and it must contain words that
        # look like column names. Title lines fail the second test, so they cannot win.
        widths = _field_counts(path)
        nonzero = [w for w in widths if w]
        if not nonzero:
            raise SystemExit(f"{path.name} has no readable rows")
        rest = nonzero[1:]
        data_width = max(set(rest), key=rest.count) if rest else nonzero[0]
        best = None
        for hdr in range(min(12, len(widths))):
            if widths[hdr] != data_width or data_width < 3:
                continue
            try:
                d = pd.read_csv(path, skiprows=hdr, engine="python")
            except Exception:
                continue
            if d.shape[0] < 4:
                continue
            named = [str(c) for c in d.columns]
            hits = sum(any(h in n.lower() for h in NAME_HINTS) for n in named)
            unnamed = sum(n.startswith("Unnamed:") or n.startswith("Unnamed_") for n in named)
            score = (hits - unnamed, hdr)
            if hits and (best is None or score > best[0]):
                best = (score, hdr, d)
        if best is None:
            raise SystemExit(
                f"could not find a header row in {path.name}: data rows have {data_width} "
                f"fields and none of the first 12 lines with that width look like column names. "
                f"Open the file, find the real header line, and check you downloaded the "
                f"closing-rates report rather than a summary page.")
        _, hdr, df = best
        print(f"[i] CSV: header taken from line {hdr + 1} ({data_width} columns); "
              f"rows below it: {len(df)}")
        return df
    if s in (".htm", ".html"):
        tables = pd.read_html(path)
        return max(tables, key=lambda t: t.shape[0] * t.shape[1])
    raise SystemExit(f"unsupported file type {s!r}")


def inspect(path: Path) -> None:
    df = read_any(path)
    print(f"[+] {path.name}: {df.shape[0]} rows x {df.shape[1]} columns")
    print("    columns as printed by PSX:")
    for c in df.columns:
        print(f"      - {str(c)[:70]!r}")
    print("\n    first 4 rows:")
    print(df.head(4).to_string(index=False)[:1600])
    dates = None
    for c in df.columns:
        col = df[c]
        if col.dtype != object:
            continue  # ints parse as epoch 1970 and read as a date column when they are not
        parsed = pd.to_datetime(col.astype(str), format="mixed", errors="coerce").dropna()
        if len(parsed) > max(3, len(df) * 0.5) and parsed.nunique() > 1:
            dates = parsed
            print(f"\n    date-like column {str(c)!r}: {parsed.min().date()} .. {parsed.max().date()}")
            break
    if dates is None:
        print("\n    no date-like column found; pass --date explicitly")
    print("\nNext: pass --map with the fields you can see above, e.g.")
    print('      --map "symbol=Stock,close=Closing Price,volume=Traded Volume"')


def _num(series) -> pd.Series:
    return pd.to_numeric(series.astype(str).str.replace(",", "", regex=False)
                         .str.replace("-", "", regex=False), errors="coerce")


def build(path: Path, target: str, mapping: dict[str, str]) -> pd.DataFrame:
    df = read_any(path)
    missing_src = [v for v in mapping.values() if v not in [str(c) for c in df.columns]]
    if missing_src:
        raise SystemExit(f"these source columns are not in the file: {missing_src}\n"
                         f"available: {[str(c) for c in df.columns]}")
    for f in REQUIRED:
        if f not in mapping:
            raise SystemExit(f"required field not mapped: {f} (need {REQUIRED})")

    out = pd.DataFrame()
    out["symbol"] = df[mapping["symbol"]].astype(str).str.strip().str.upper()
    out["close"] = _num(df[mapping["close"]])
    out["volume"] = _num(df[mapping["volume"]])
    for f in ("ldcp", "open", "high", "low"):
        out[f] = _num(df[mapping[f]]) if f in mapping else pd.NA
    out["trade_date"] = (df[mapping["trade_date"]].astype(str) if "trade_date" in mapping
                         else pd.Series([target] * len(df), index=df.index))
    out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")

    out = out.dropna(subset=["symbol", "close", "volume"])
    out = out[out["symbol"].str.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{1,14}")]
    agg = out["symbol"].str.replace(r"[^A-Z0-9]", "", regex=True).isin(AGGREGATE_LABELS)
    if agg.any():
        print(f"[i] dropped {int(agg.sum())} aggregate/footer row(s): "
              f"{sorted(out.loc[agg, 'symbol'])}")
        out = out[~agg]
    off_day = sorted(set(out["trade_date"]) - {target})
    if off_day:
        raise SystemExit(f"file contains dates other than {target}: {off_day[:6]} - "
                         f"refusing to guess which rows belong to the missing day")
    if out["symbol"].duplicated().any():
        dup = sorted(out.loc[out["symbol"].duplicated(), "symbol"])[:10]
        raise SystemExit(f"duplicate symbols in file: {dup}")
    if len(out) < 50:
        raise SystemExit(f"only {len(out)} usable rows - a PSX session has hundreds. "
                         f"Wrong file, wrong sheet, or wrong column mapping.")
    return out.reset_index(drop=True)


def derive_columns(out: pd.DataFrame, conn) -> pd.DataFrame:
    """base_symbol from the Amendment #13 rule, sector from the locked sector_map.

    Using the raw file's own sector text, or the old base_symbol stripper, would put back the
    two defects that #13 and #17 were registered to remove.
    """
    traded = RB.traded_symbols(conn)
    known = traded | set(out["symbol"])
    out["base_symbol"] = [RB.derive_base(s, known) for s in out["symbol"]]

    unseen = sorted(set(out["symbol"]) - traded)
    if unseen:
        frac = len(unseen) / len(out)
        msg = (f"{len(unseen)} of {len(out)} symbols here never appear in our quote history")
        if frac > 0.10:
            raise SystemExit(f"{msg} ({frac:.0%}). One session does not bring in that many new "
                             f"listings at once, so the symbol column is almost certainly "
                             f"mis-mapped. Unseen: {unseen[:20]}")
        print(f"[i] {msg} ({frac:.1%}) - new listings are normal, but check these: "
              f"{unseen[:15]}")

    # The nightly ingest fills this column with psx_data_v2.base_symbol(), not derive_base().
    # If the two rules ever disagree on a row, the manually backfilled day would carry a join
    # key no automated run could reproduce - which is exactly the class of silent divergence
    # #13 was registered to eliminate. Refuse rather than ship a day like that.
    live_known = {r[0] for r in conn.execute(
        "SELECT DISTINCT base_symbol FROM daily_quotes WHERE base_symbol IS NOT NULL") if r[0]}
    disagree = {s: (b, P.base_symbol(s, known=live_known))
                for s, b in zip(out["symbol"], out["base_symbol"])
                if P.base_symbol(s, known=live_known) != b}
    if disagree:
        raise SystemExit(f"base_symbol rule conflict (manual vs nightly ingest): {disagree}")

    # The locked sector_map only covers the 138-name MTS universe, which is correct - the
    # research code must never read daily_quotes.sector (Amendment #17). This column is
    # descriptive, so fill the rest from the sector code our own DB already holds for that
    # symbol rather than from the downloaded file's sector text.
    smap = ME.sector_map_map(conn)
    last = "SELECT symbol, sector, market FROM daily_quotes WHERE trade_date=(SELECT MAX(trade_date) FROM daily_quotes)"
    carried = {r[0]: ((r[1] or ""), (r[2] or "")) for r in conn.execute(last)}
    out["sector"] = [smap.get(s, smap.get(b, carried.get(s, carried.get(b, ("", "")))[0]))
                     for s, b in zip(out["symbol"], out["base_symbol"])]
    out["market"] = [carried.get(s, carried.get(b, ("", "")))[1]
                     for s, b in zip(out["symbol"], out["base_symbol"])]
    out["quality_flags"] = ""
    return out


def sanity(out: pd.DataFrame) -> tuple[list[str], str]:
    """(printed notes, verdict). The verdict is written into quality_flags so the band check is
    auditable from the database alone - the clean-session rule drafted in
    amendments/apply_amendment_20.py depends on that, not on someone remembering."""
    notes = []
    if "ldcp" in out and out["ldcp"].notna().any():
        ratio = (out["close"] / out["ldcp"]).dropna()
        odd = int(((ratio < 0.5) | (ratio > 2.0)).sum())
        wide = int(((ratio - 1).abs() > 0.15).sum())
        notes.append(f"close/ldcp over {len(ratio)} paired rows: median {ratio.median():.4f}, "
                     f"min {ratio.min():.4f}, max {ratio.max():.4f}")
        notes.append(f"  outside [0.5, 2.0]: {odd} ({odd/len(ratio):.1%})  "
                     f"beyond +-15%: {wide} ({wide/len(ratio):.1%})")
        if odd > len(out) * 0.05:
            notes.append("WARNING: >5% of rows have an implausible close/ldcp - check the mapping")
            verdict = "LDCP_SUSPECT"
        else:
            notes.append("close-vs-ldcp band check PASSED (a +-15% tail is normal: PSX price "
                         "bands are wider than 15% for some categories)")
            verdict = "LDCP_OK"
    else:
        notes.append("ldcp not mapped: the close-vs-ldcp check is UNAVAILABLE for this backfill")
        verdict = "LDCP_UNAVAILABLE"
    notes.append(f"rows {len(out)}, volume zero/NA {int((out['volume'].fillna(0) <= 0).sum())}")
    return notes, verdict


def write(out: pd.DataFrame, conn, sha: str, live: bool, verdict: str = "LDCP_UNAVAILABLE") -> int:
    ts = datetime.now(PKT).isoformat(timespec="seconds")
    flag = f"MANUAL_DOWNLOAD:sha256={sha[:12]}:{verdict}"
    if not live:
        flag += ":SCRATCH"
    rows = [(r.trade_date, r.symbol, r.sector, r.ldcp, r.open, r.high, r.low, r.close,
             r.volume, ts, r.base_symbol, r.market, 1, flag) for r in out.itertuples()]
    conn.executemany("INSERT OR REPLACE INTO daily_quotes(trade_date, symbol, sector, ldcp, open, "
                     "high, low, close, volume, ingest_ts, base_symbol, market, is_final, "
                     "quality_flags) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return len(rows)


def check_on(conn, target: str, label: str) -> None:
    """Run the same completeness assertion the nightly pipeline runs, on this connection."""
    import jegadeesh_titman_portfolio as J
    J.verify_corporate_actions_completeness(conn)
    n, syms = conn.execute("SELECT COUNT(*), COUNT(DISTINCT symbol) FROM daily_quotes "
                           "WHERE trade_date=?", (target,)).fetchone()
    bases = conn.execute("SELECT COUNT(DISTINCT base_symbol) FROM daily_quotes "
                         "WHERE trade_date=?", (target,)).fetchone()[0]
    print(f"[+] {label}: {n} rows / {syms} symbols / {bases} bases for {target}, "
          f"corporate-actions completeness PASSED")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--inspect", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--live", action="store_true", help="write psx.db; default is a scratch copy")
    ap.add_argument("--date")
    ap.add_argument("--map", default="")
    ap.add_argument("--overwrite", action="store_true",
                    help="allow replacing rows on a date the pipeline already captured")
    ap.add_argument("--force", action="store_true",
                    help="allow --live even if the close-vs-ldcp band check fails")
    a = ap.parse_args()

    path = Path(a.file)
    if not path.exists():
        raise SystemExit(f"file not found: {path}")
    if a.inspect or not a.apply:
        inspect(path)
        if not a.apply:
            return

    mapping = {}
    for part in re.split(r",\s*(?=[a-z_]+=)", a.map):
        if "=" in part:
            k, v = part.split("=", 1)
            mapping[k.strip()] = v.strip()
    if not a.date:
        raise SystemExit("--date is required with --apply")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", a.date):
        raise SystemExit(f"--date must be YYYY-MM-DD, got {a.date!r}")

    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    print(f"[+] source {path.name} sha256={sha}")

    conn = sqlite3.connect("psx.db", timeout=60)
    try:
        already = conn.execute("SELECT COUNT(*) FROM daily_quotes WHERE trade_date=?",
                               (a.date,)).fetchone()[0]
        if already and not a.overwrite:
            raise SystemExit(f"{a.date} already holds {already} rows in psx.db. Refusing to "
                             f"clobber a captured day - re-run with --overwrite if intended.")
        out = derive_columns(build(path, a.date, mapping), conn)
        print("=== SANITY ===")
        notes, verdict = sanity(out)
        for n in notes:
            print("  ", n)
        print(f"   verdict recorded on every inserted row: {verdict}")

        if not a.live:
            scratch = Path("scratch") / f"backfill_{a.date}_test.db"
            scratch.parent.mkdir(exist_ok=True)
            scratch.unlink(missing_ok=True)
            src = sqlite3.connect("psx.db")
            dst = sqlite3.connect(scratch)
            src.backup(dst)
            src.close()
            try:
                n = write(out, dst, sha, False, verdict)
                check_on(dst, a.date, f"scratch ({scratch})")
                print(f"[+] {n} rows written to the scratch copy only - psx.db untouched")
                print("    re-run with --live once you have read the sanity lines above")
            except Exception as e:
                print(f"[!] scratch check FAILED: {type(e).__name__}: {str(e)[:300]}")
                print("    NOT touching psx.db")
            finally:
                dst.close()
            return

        if verdict == "LDCP_SUSPECT" and not a.force:
            raise SystemExit("close-vs-ldcp band check FAILED, so this day cannot be a clean "
                             "session. Fix the mapping and re-run, or use --force if the wide "
                             "moves are real (the row flag will say LDCP_SUSPECT either way).")
        P.archive_raw(f"manual_backfill_{a.date}", path.read_bytes())
        n = write(out, conn, sha, True, verdict)
        check_on(conn, a.date, "psx.db")
        print(f"[+] wrote {n} rows into psx.db for {a.date} with "
              f"quality_flags='MANUAL_DOWNLOAD:sha256={sha[:12]}:{verdict}'")
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT trade_date FROM daily_quotes WHERE is_final=1 "
            "AND trade_date>='2026-09-01' ORDER BY trade_date")]
        print(f"[i] final-quote dates since 1 Sep ({len(dates)}): {dates[-8:]}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
