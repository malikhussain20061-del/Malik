"""
psx_data_v2.py — Institutional Point-in-Time Data Layer & Perishable Feeds Engine

Key Architectural Enhancements:
  1. Perishable Feeds: Captures daily NCCPL FIPI / LIPI institutional flows.
  2. Raw Archive: Gzip-compressed storage of raw HTML/JSON with SHA-256 checksums.
  3. UPSERT Logic: Eliminates data corruption from midday runs (final close always wins).
  4. Base Symbol Normalization: Strips XD, XB, NC, XR suffixes for continuous time-series.
  5. Sanity Checks & Quality Flags: Flags invalid price/volume combinations.
  6. Audit Logging: Every ingest run (success or fail) recorded in `ingest_runs`.
  7. Gap Detection: Identifies missing trading calendar dates.
"""

from __future__ import annotations

import sys
import os
import gzip
import hashlib
import json
import sqlite3
import datetime as dt
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo
import pandas as pd
from bs4 import BeautifulSoup

# Ensure Windows terminal UTF-8 encoding
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

PKT = ZoneInfo("Asia/Karachi")
DB_PATH = "psx.db"
RAW_DIR = Path("raw_archive")
MW_URL = "https://dps.psx.com.pk/market-watch"
FIPILIPI_URL = "https://www.scstrade.com/FIPILIPI.aspx/loadlipi"

SUFFIXES = ("XDXB", "XDXR", "XBXR", "XD", "XB", "XR", "NC", "EX", "BC")

HEADER_MAP = {
    "symbol": {"symbol", "scrip"},
    "sector": {"sector"},
    "market": {"listed in", "market", "listedin"},
    "ldcp":   {"ldcp"},
    "open":   {"open"},
    "high":   {"high"},
    "low":    {"low"},
    "close":  {"current", "close", "last"},
    "volume": {"volume", "vol"},
}
REQUIRED = {"symbol", "ldcp", "open", "high", "low", "close", "volume"}


class FeedError(RuntimeError):
    """Feed unavailable ya malformed. Yeh exception KABHI silently swallow na karein."""


def base_symbol(sym: str) -> str:
    """LUCKXD -> LUCK, SYSXB -> SYS (Normalization prevents fragmented time series)."""
    s = sym.upper().strip()
    for suf in sorted(SUFFIXES, key=len, reverse=True):
        if s.endswith(suf) and len(s) > len(suf):
            return s[:-len(suf)]
    # Rights voucher handling: STLR -> STL, SGPLR -> SGPL (excludes genuine stocks ending in R)
    if s.endswith("R") and len(s) >= 4 and s not in {"POWER", "HCAR", "MACTER", "DCR", "GTYR", "PAKQATAR", "NNAR"}:
        return s[:-1]
    return s


def archive_raw(feed: str, content: bytes) -> str:
    """Parser galat nikla to poori history dobara parse ho sakegi."""
    RAW_DIR.mkdir(exist_ok=True)
    sha = hashlib.sha256(content).hexdigest()
    stamp = dt.datetime.now(PKT).strftime("%Y%m%d_%H%M%S")
    archive_path = RAW_DIR / f"{feed}_{stamp}.gz"
    archive_path.write_bytes(gzip.compress(content))
    return sha


def validate_row(r: dict) -> str:
    """Khali string = clean. Warna comma-separated quality flags."""
    f = []
    high = r.get("high")
    low = r.get("low")
    close = r.get("close")
    ldcp = r.get("ldcp")
    vol = r.get("volume") or 0.0

    if vol == 0.0:
        f.append("ZERO_VOLUME")
    if close is None:
        f.append("NO_CLOSE")
    if high is not None and low is not None and high < low - 1e-9:
        f.append("HIGH_LT_LOW")
    if close is not None and high is not None and close > high + 1e-9:
        f.append("CLOSE_GT_HIGH")
    if close is not None and low is not None and close < low - 1e-9:
        f.append("CLOSE_LT_LOW")
    if ldcp is not None and close is not None and abs(close - ldcp) > 0.05 and vol == 0:
        f.append("PRICE_MOVE_ZERO_VOL")
    return ",".join(f)


def init_db(path: str = DB_PATH) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    # Safe schema migration for existing databases
    existing_cols = {row[1] for row in con.execute("PRAGMA table_info(daily_quotes)").fetchall()}
    if existing_cols:
        if "base_symbol" not in existing_cols:
            con.execute("ALTER TABLE daily_quotes ADD COLUMN base_symbol TEXT;")
        if "market" not in existing_cols:
            con.execute("ALTER TABLE daily_quotes ADD COLUMN market TEXT;")
        if "is_final" not in existing_cols:
            con.execute("ALTER TABLE daily_quotes ADD COLUMN is_final INTEGER NOT NULL DEFAULT 0;")
        if "quality_flags" not in existing_cols:
            con.execute("ALTER TABLE daily_quotes ADD COLUMN quality_flags TEXT;")
        con.commit()

    existing_mts = {row[1] for row in con.execute("PRAGMA table_info(mts_quotes)").fetchall()}
    if existing_mts:
        for col in ["new_mts_volume", "new_mts_amount", "weighted_rate", "open_pct"]:
            if col not in existing_mts:
                con.execute(f"ALTER TABLE mts_quotes ADD COLUMN {col} REAL;")
        con.commit()

    con.executescript("""
    CREATE TABLE IF NOT EXISTS daily_quotes (
        trade_date    TEXT NOT NULL,
        symbol        TEXT NOT NULL,
        base_symbol   TEXT NOT NULL,
        sector        TEXT,
        market        TEXT,
        ldcp          REAL,
        open          REAL,
        high          REAL,
        low           REAL,
        close         REAL,
        volume        REAL,
        is_final      INTEGER NOT NULL DEFAULT 0,
        quality_flags TEXT,
        ingest_ts     TEXT NOT NULL,
        PRIMARY KEY (trade_date, symbol)
    );
    CREATE INDEX IF NOT EXISTS ix_quotes_base ON daily_quotes(base_symbol, trade_date);

    -- Perishable Feed: NCCPL Daily FIPI / LIPI flows by category
    CREATE TABLE IF NOT EXISTS fipi_lipi (
        trade_date   TEXT NOT NULL,
        category     TEXT NOT NULL,
        market       TEXT NOT NULL,
        buy_volume   REAL,
        buy_value    REAL,
        sell_volume  REAL,
        sell_value   REAL,
        net_value    REAL,
        ingest_ts    TEXT NOT NULL,
        PRIMARY KEY (trade_date, category, market)
    );

    -- Perishable Feed: NCCPL Daily Per-Symbol MTS Outstanding & Daily Flow
    CREATE TABLE IF NOT EXISTS mts_quotes (
        trade_date      TEXT NOT NULL,
        symbol          TEXT NOT NULL,
        base_symbol     TEXT NOT NULL,
        mts_volume      REAL, -- Net Open MTS Volume (EOD Total Outstanding)
        mts_amount      REAL, -- Net Open MTS Amount (EOD Total Outstanding Value)
        new_mts_volume  REAL, -- Current Day MTS Volume (Fresh Financing Today)
        new_mts_amount  REAL, -- Current Day MTS Amount
        weighted_rate   REAL, -- Weighted Average Financing Rate (%)
        open_pct        REAL, -- MTS Open Percentage (%)
        ingest_ts       TEXT NOT NULL,
        PRIMARY KEY (trade_date, symbol)
    );
    CREATE INDEX IF NOT EXISTS ix_mts_base ON mts_quotes(base_symbol, trade_date);

    CREATE TABLE IF NOT EXISTS ingest_runs (
        run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
        started_ts    TEXT NOT NULL,
        feed          TEXT NOT NULL,
        status        TEXT NOT NULL,
        rows_written  INTEGER NOT NULL DEFAULT 0,
        error         TEXT,
        raw_sha256    TEXT
    );

    CREATE TABLE IF NOT EXISTS signals (
        signal_id     INTEGER PRIMARY KEY AUTOINCREMENT,
        created_ts    TEXT NOT NULL,
        trade_date    TEXT NOT NULL,
        symbol        TEXT NOT NULL,
        rule_name     TEXT NOT NULL,
        direction     INTEGER NOT NULL,    -- +1 Long, -1 Short
        strength      REAL,
        ref_price     REAL NOT NULL,
        exec_basis    TEXT NOT NULL DEFAULT 'next_open',
        notes         TEXT
    );
    CREATE INDEX IF NOT EXISTS ix_sig ON signals(rule_name, trade_date);
    """)
    con.commit()
    return con


def log_run(con: sqlite3.Connection, feed: str, status: str, rows: int = 0,
            error: str | None = None, sha: str | None = None):
    con.execute(
        "INSERT INTO ingest_runs (started_ts, feed, status, rows_written, error, raw_sha256) "
        "VALUES (?,?,?,?,?,?)",
        (dt.datetime.now(PKT).isoformat(timespec="seconds"), feed, status, rows, error, sha)
    )
    con.commit()


def _norm(s: str) -> str:
    return " ".join(s.lower().split()).replace("(", "").replace(")", "").strip()


def fetch_market_watch(timeout: int = 20) -> tuple[list[dict], str]:
    """Fetches PSX Market Watch, archives raw HTML, returns parsed rows."""
    req = urllib.request.Request(MW_URL, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    try:
        raw_bytes = urllib.request.urlopen(req, timeout=timeout).read()
        sha = archive_raw("market_watch", raw_bytes)
        html = raw_bytes.decode("utf-8", "ignore")
    except Exception as e:
        raise FeedError(f"market-watch unreachable: {e}") from e

    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None:
        raise FeedError("no <table> found — page layout changed")

    headers = [_norm(th.get_text()) for th in table.find_all("th")]
    if not headers:
        raise FeedError("no <th> headers — cannot validate schema")

    idx: dict[str, int] = {}
    for canon, aliases in HEADER_MAP.items():
        for i, h in enumerate(headers):
            if h in aliases:
                idx[canon] = i
                break

    missing = REQUIRED - idx.keys()
    if missing:
        raise FeedError(f"schema changed, missing {sorted(missing)}; saw {headers}")

    now = dt.datetime.now(PKT)
    trade_date = now.date().isoformat()
    rows = []
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < len(REQUIRED):
            continue
        row = {"trade_date": trade_date}
        for canon, col_idx in idx.items():
            txt = tds[col_idx].get_text().strip().replace(",", "")
            row[canon] = txt
        rows.append(row)

    if not rows:
        raise FeedError("table found but 0 valid rows parsed")

    # Clean and convert types
    cleaned_rows = []
    for r in rows:
        try:
            r["ldcp"] = float(r["ldcp"]) if r.get("ldcp") else None
            r["open"] = float(r["open"]) if r.get("open") else None
            r["high"] = float(r["high"]) if r.get("high") else None
            r["low"] = float(r["low"]) if r.get("low") else None
            r["close"] = float(r["close"]) if r.get("close") else None
            r["volume"] = float(r["volume"]) if r.get("volume") else 0.0
            # Store all rows to track halts, suspensions, and un-traded stocks for survivorship bias defense
            if r.get("symbol"):
                cleaned_rows.append(r)
        except Exception:
            continue

    return cleaned_rows, sha


def upsert_quotes(con: sqlite3.Connection, rows: list[dict], is_final: bool) -> int:
    """
    UPSERT: Final evening close always wins; midday partial never overwrites final.
    """
    sql = """
    INSERT INTO daily_quotes (
        trade_date, symbol, base_symbol, sector, market,
        ldcp, open, high, low, close, volume, is_final, quality_flags, ingest_ts
    )
    VALUES (
        :trade_date, :symbol, :base_symbol, :sector, :market,
        :ldcp, :open, :high, :low, :close, :volume, :is_final, :quality_flags, :ingest_ts
    )
    ON CONFLICT(trade_date, symbol) DO UPDATE SET
        sector=excluded.sector, market=excluded.market,
        open=excluded.open, high=excluded.high, low=excluded.low,
        close=excluded.close, volume=excluded.volume,
        is_final=excluded.is_final, quality_flags=excluded.quality_flags,
        ingest_ts=excluded.ingest_ts
    WHERE excluded.is_final >= daily_quotes.is_final;
    """
    now = dt.datetime.now(PKT).isoformat(timespec="seconds")
    prepared = []
    for r in rows:
        r_copy = dict(r)
        r_copy["base_symbol"] = base_symbol(r_copy["symbol"])
        r_copy["is_final"] = 1 if is_final else 0
        r_copy["quality_flags"] = validate_row(r_copy)
        r_copy["ingest_ts"] = now
        r_copy.setdefault("market", "")
        r_copy.setdefault("sector", "")
        prepared.append(r_copy)

    con.executemany(sql, prepared)
    con.commit()
    return len(prepared)


def fetch_and_store_fipi_lipi(con: sqlite3.Connection, target_date: dt.date | None = None) -> int:
    """
    Captures Perishable NCCPL Daily FIPI / LIPI Institutional Flows.
    If market is currently open and today's numbers are not yet finalized by NCCPL,
    it archives the most recent finalized trading day.
    """
    if target_date is None:
        target_date = dt.datetime.now(PKT).date()

    def _fetch_date(d: dt.date):
        date_str = d.strftime("%m/%d/%Y")
        payload = json.dumps({'date1': date_str, 'date2': date_str}).encode('utf-8')
        req = urllib.request.Request(
            FIPILIPI_URL,
            data=payload,
            headers={'User-Agent': 'Mozilla/5.0', 'Content-Type': 'application/json; charset=UTF-8'}
        )
        raw_bytes = urllib.request.urlopen(req, timeout=12).read()
        data = json.loads(raw_bytes.decode('utf-8'))
        records = json.loads(data['d']) if isinstance(data.get('d'), str) else data.get('d', [])
        return records, raw_bytes

    try:
        records, raw_bytes = _fetch_date(target_date)
        actual_date = target_date
        # If today is empty (intraday before NCCPL publish at 5:30 PM), fetch previous trading day
        if not records:
            days_back = 1
            while days_back <= 4:
                prev_d = target_date - dt.timedelta(days=days_back)
                if prev_d.weekday() < 5:  # Mon-Fri only
                    records, raw_bytes = _fetch_date(prev_d)
                    if records:
                        actual_date = prev_d
                        break
                days_back += 1
        
        sha = archive_raw("fipi_lipi", raw_bytes) if raw_bytes else None
    except Exception as e:
        log_run(con, "fipi_lipi", "FAILED", 0, error=str(e))
        raise FeedError(f"FIPI/LIPI feed error: {e}") from e

    if not records:
        log_run(con, "fipi_lipi", "EMPTY", 0, error="No records returned for date", sha=sha)
        return 0

    trade_date_iso = actual_date.isoformat()
    now = dt.datetime.now(PKT).isoformat(timespec="seconds")
    sql = """
    INSERT INTO fipi_lipi (
        trade_date, category, market, buy_volume, buy_value, sell_volume, sell_value, net_value, ingest_ts
    )
    VALUES (?,?,?,?,?,?,?,?,?)
    ON CONFLICT(trade_date, category, market) DO UPDATE SET
        buy_volume=excluded.buy_volume, buy_value=excluded.buy_value,
        sell_volume=excluded.sell_volume, sell_value=excluded.sell_value,
        net_value=excluded.net_value, ingest_ts=excluded.ingest_ts;
    """
    rows = []
    for item in records:
        cat = item.get('FLType', '').strip()
        mkt = item.get('FLMarket', 'ALL').strip()
        bv = float(item.get('FLBuyVolume', 0.0))
        bval = float(item.get('FLBuyValue', 0.0))
        sv = float(item.get('FLSellVolume', 0.0))
        sval = float(item.get('FLSellValue', 0.0))
        net = float(item.get('NetValue', bval + sval))
        rows.append((trade_date_iso, cat, mkt, bv, bval, sv, sval, net, now))

    con.executemany(sql, rows)
    con.commit()
    log_run(con, "fipi_lipi", "SUCCESS", len(rows), sha=sha)
    return len(rows)


MTS_PDF_URL = "http://www.scstrade.com/research/Research%20Reports/General/MTS%20Report.pdf"
BACKUP_DIR = Path("backups")

def fetch_and_store_mts(con: sqlite3.Connection) -> int:
    """
    Captures Perishable NCCPL Daily Per-Symbol MTS Outstanding.
    Extracts Symbol, Open MTS Volume, and Open MTS Amount from daily clearing report.
    """
    req = urllib.request.Request(MTS_PDF_URL, headers={"User-Agent": "Mozilla/5.0"})
    try:
        raw_bytes = urllib.request.urlopen(req, timeout=15).read()
        sha = archive_raw("mts_report", raw_bytes)
    except Exception as e:
        log_run(con, "mts_report", "FAILED", 0, error=str(e))
        raise FeedError(f"MTS report unreachable: {e}") from e

    import pymupdf
    import re
    doc = pymupdf.open(stream=raw_bytes, filetype="pdf")
    
    # Page 0 has report date: e.g. "September 11, 2026"
    p0_text = doc[0].get_text()
    date_match = re.search(r'([A-Za-z]+ \d{1,2}, \d{4})', p0_text)
    if date_match:
        try:
            report_dt = dt.datetime.strptime(date_match.group(1), "%B %d, %Y").date()
            trade_date_iso = report_dt.isoformat()
        except Exception:
            trade_date_iso = dt.datetime.now(PKT).date().isoformat()
    else:
        trade_date_iso = dt.datetime.now(PKT).date().isoformat()

    now = dt.datetime.now(PKT).isoformat(timespec="seconds")
    rows = []
    # Tables on Page 1 and 2 contain Symbol Code, Open MTS, Current Day MTS, Net Open MTS
    for pno in [1, 2]:
        if pno >= len(doc):
            break
        tables = doc[pno].find_tables()
        for tab in tables:
            grid = tab.extract()
            for r in grid[1:]:
                # Table has 15 columns; Symbol is at index 2
                if len(r) >= 14 and r[2] and r[2].strip():
                    sym = r[2].strip().upper()
                    try:
                        # Net Open MTS Volume/Amount (Col 11 & 12) is the true EOD outstanding position
                        net_vol = float(str(r[11]).replace(',', '').strip())
                        net_amt = float(str(r[12]).replace(',', '').strip())
                        # Current Day MTS Volume/Amount (Col 8 & 9) is the fresh financing today
                        new_vol = float(str(r[8]).replace(',', '').strip())
                        new_amt = float(str(r[9]).replace(',', '').strip())
                        # Weighted Average financing rate % (Col 10)
                        w_rate = float(str(r[10]).replace(',', '').strip())
                        # MTS Open Percentage of float/capital (Col 13)
                        open_pct = float(str(r[13]).replace(',', '').strip())
                        base_sym = base_symbol(sym)
                        rows.append((trade_date_iso, sym, base_sym, net_vol, net_amt, new_vol, new_amt, w_rate, open_pct, now))
                    except Exception:
                        continue

    if not rows:
        log_run(con, "mts_report", "EMPTY", 0, error="Could not extract MTS rows", sha=sha)
        return 0

    # Write temporary file to pass to hardened mts_engine.ingest_report
    tmp_pdf = RAW_DIR / f"temp_mts_{sha[:8]}.pdf"
    try:
        tmp_pdf.write_bytes(raw_bytes)
        try:
            import mts_engine
            mts_engine.ingest_report(
                con,
                tmp_pdf,
                report_date=trade_date_iso,
                captured_at=now,
                sanitizer=base_symbol,
                source_url=MTS_PDF_URL
            )
        except Exception as e_engine:
            # If report is from an earlier date or stale, log it
            log_run(con, "mts_engine_ingest", "WARNING", 0, error=str(e_engine), sha=sha)
    finally:
        if tmp_pdf.exists():
            tmp_pdf.unlink()

    sql = """
    INSERT INTO mts_quotes (
        trade_date, symbol, base_symbol, mts_volume, mts_amount,
        new_mts_volume, new_mts_amount, weighted_rate, open_pct, ingest_ts
    )
    VALUES (?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(trade_date, symbol) DO UPDATE SET
        mts_volume=excluded.mts_volume,
        mts_amount=excluded.mts_amount,
        new_mts_volume=excluded.new_mts_volume,
        new_mts_amount=excluded.new_mts_amount,
        weighted_rate=excluded.weighted_rate,
        open_pct=excluded.open_pct,
        ingest_ts=excluded.ingest_ts;
    """
    con.executemany(sql, rows)
    con.commit()
    log_run(con, "mts_report", "SUCCESS", len(rows), sha=sha)
    return len(rows)


def create_automated_backup() -> str | None:
    """
    Disaster Recovery: Creates a compressed zip backup of psx.db into backups/ folder.
    """
    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = dt.datetime.now(PKT).strftime("%Y%m%d_%H%M%S")
    backup_file = BACKUP_DIR / f"psx_backup_{stamp}.zip"
    import zipfile
    try:
        with zipfile.ZipFile(backup_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            if os.path.exists(DB_PATH):
                zf.write(DB_PATH, arcname="psx.db")
        return str(backup_file)
    except Exception as e:
        print(f"[!] Backup error: {e}")
        return None


def missing_days(con: sqlite3.Connection, since: str) -> list[str]:
    """Detects missing trading day gaps in the database (excluding verified holidays)."""
    quotes_dates = {r[0] for r in con.execute(
        "SELECT DISTINCT trade_date FROM daily_quotes WHERE trade_date >= ?", (since,)
    )}
    holiday_dates = {r[0] for r in con.execute(
        "SELECT DISTINCT SUBSTR(started_ts, 1, 10) FROM ingest_runs WHERE status IN ('HOLIDAY', 'CLOSED') AND started_ts >= ?", (since,)
    )}
    have = quotes_dates | holiday_dates
    d = dt.date.fromisoformat(since)
    today = dt.datetime.now(PKT).date()
    missing = []
    while d <= today:
        if d.weekday() < 5 and d.isoformat() not in have:  # Weekdays only
            missing.append(d.isoformat())
        d += dt.timedelta(days=1)
    return missing


if __name__ == "__main__":
    print("=" * 80)
    print("      PSX POINT-IN-TIME DATA LAYER V2 — INSTITUTIONAL AUDIT ENGINE")
    print("=" * 80)
    con = init_db()

    # 1. Ingest Daily Quotes with UPSERT & Raw Gzip Archiving
    print("\n[*] [1/3] Ingesting PSX Market Watch Snapshot...")
    is_evening = dt.datetime.now(PKT).hour >= 16
    try:
        rows, sha = fetch_market_watch()
        n_quotes = upsert_quotes(con, rows, is_final=is_evening)
        log_run(con, "market_watch", "SUCCESS", n_quotes, sha=sha)
        print(f" [+] Success: {n_quotes} quotes upserted (is_final={is_evening}) | Raw SHA: {sha[:10]}...")
    except FeedError as e:
        log_run(con, "market_watch", "FAILED", 0, error=str(e))
        print(f" [!] Feed Error: {e}")

    # 2. Ingest Perishable NCCPL FIPI / LIPI Flows
    print("\n[*] [2/3] Ingesting Perishable NCCPL FIPI/LIPI Institutional Flows...")
    try:
        n_fipi = fetch_and_store_fipi_lipi(con)
        print(f" [+] Success: {n_fipi} institutional flow categories archived for today!")
    except Exception as e:
        print(f" [!] Note on FIPI/LIPI: {e}")

    # 3. Ingest Perishable NCCPL Daily Per-Symbol MTS Outstanding
    print("\n[*] [3/3] Ingesting Perishable NCCPL Daily Per-Symbol MTS Outstanding...")
    try:
        n_mts = fetch_and_store_mts(con)
        print(f" [+] Success: {n_mts} stocks MTS leverage positions archived!")
    except Exception as e:
        print(f" [!] Note on MTS: {e}")

    # 4. Automated Backup (Disaster Recovery Insurance)
    backup_path = create_automated_backup()
    if backup_path:
        print(f"\n [🛡️] Automated Disaster Recovery Backup Created: {backup_path}")

    # 5. Audit Check
    total_q = con.execute("SELECT count(*) FROM daily_quotes").fetchone()[0]
    total_f = con.execute("SELECT count(*) FROM fipi_lipi").fetchone()[0]
    total_m = con.execute("SELECT count(*) FROM mts_quotes").fetchone()[0]
    runs = con.execute("SELECT count(*) FROM ingest_runs").fetchone()[0]
    print("\n" + "─" * 80)
    print(f" 📊 DATABASE STATUS: {total_q:,} Quotes | {total_f:,} FIPI/LIPI Rows | {total_m:,} MTS Rows | {runs} Audit Runs Logged")
    
    # Check for gaps since beginning of month
    since_date = dt.date.today().replace(day=1).isoformat()
    gaps = missing_days(con, since_date)
    if gaps:
        print(f" ⚠️ Missing trading days since {since_date}: {gaps}")
    else:
        print(f" ✅ Zero data gaps detected since {since_date}.")
    print("=" * 80 + "\n")
