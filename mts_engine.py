"""

mts_engine.py — MTS_CROWDING_XS_H10 signal layer.

Archive/parse NCCPL MTS report, sanitize symbols, build PIT signal panel,

liquid filter, cross-sectional quantiles, publication-lag enforcement,

symmetric lock predicates (shared with the portfolio engine).

"""

from __future__ import annotations



import hashlib

import logging

import re

import sqlite3

from dataclasses import dataclass

from pathlib import Path

from typing import Mapping



import numpy as np

import pandas as pd



log = logging.getLogger("mts_engine")

PKT = "Asia/Karachi"



SCHEMA = """

CREATE TABLE IF NOT EXISTS mts_raw_reports(

  report_date TEXT NOT NULL,

  captured_at TEXT NOT NULL,

  source_url  TEXT,

  sha256      TEXT NOT NULL UNIQUE,

  blob        BLOB NOT NULL

);

CREATE TABLE IF NOT EXISTS mts_snapshots(

  report_date TEXT NOT NULL,

  symbol      TEXT NOT NULL,

  raw_symbol  TEXT NOT NULL,

  mts_volume REAL, mts_amount REAL, new_mts_volume REAL, new_mts_amount REAL,

  weighted_rate REAL, open_pct REAL, implied_denominator REAL,

  captured_at TEXT NOT NULL,

  report_sha256 TEXT NOT NULL,

  PRIMARY KEY(report_date, symbol)

);

CREATE TABLE IF NOT EXISTS mts_eligible(

  effective_date TEXT NOT NULL,

  symbol TEXT NOT NULL,

  PRIMARY KEY(effective_date, symbol)

);

CREATE TABLE IF NOT EXISTS mts_anomalies(

  report_date TEXT, symbol TEXT, kind TEXT, detail TEXT

);

"""





def init_schema(conn: sqlite3.Connection) -> None:

    conn.executescript(SCHEMA)

    conn.commit()





class StaleReportError(RuntimeError):

    """Identical PDF bytes served for a different report date (page not refreshed)."""





class UnknownSymbolError(KeyError):

    pass





def to_number(x) -> float:

    if x is None:

        return np.nan

    s = str(x).strip().replace(",", "").replace("%", "").replace(" ", "")

    if s in ("", "-", "--", "N/A", "NA", "nil"):

        return np.nan

    neg = s.startswith("(") and s.endswith(")")

    s = s.strip("()")

    try:

        v = float(s)

    except ValueError:

        return np.nan

    return -v if neg else v





# Ordered, specific first. TUNE to the exact header text of your NCCPL PDF.

DEFAULT_COLUMN_PATTERNS: dict[str, str] = {

    "raw_symbol": r"^(symbol|scrip|security code|security)$",

    "new_mts_volume": r"new.*(vol|qty|quantity)",

    "new_mts_amount": r"new.*(amount|value)",

    "mts_volume": r"(net\s*open|outstanding).*(vol|qty|quantity)",

    "mts_amount": r"(net\s*open|outstanding).*(amount|value)",

    "weighted_rate": r"(weighted|wtd|avg).*(rate|markup)",

    "open_pct": r"(open|outstanding).*(%|percent|pct)|(%|percent|pct).*(float|capital)",

}

REQUIRED_FIELDS = ("raw_symbol", "mts_volume", "open_pct")

NON_SYMBOL_ROWS = {"TOTAL", "GRANDTOTAL", "SUBTOTAL"}





def _map_header(cells: list[str], patterns: Mapping[str, str]) -> dict[str, int] | None:

    lowered = [c.lower() for c in cells]

    mapping: dict[str, int] = {}

    used: set[int] = set()

    for field, pat in patterns.items():

        hits = [i for i, c in enumerate(lowered) if i not in used and c and re.search(pat, c)]

        if len(hits) == 1:

            mapping[field] = hits[0]

            used.add(hits[0])

        elif len(hits) > 1:

            return None  # ambiguous header -> refine patterns

    return mapping if all(f in mapping for f in REQUIRED_FIELDS) else None





class ParseIntegrityError(RuntimeError):
    pass

def _is_header(cells) -> bool:
    if len(cells) < 14:
        return False
    c2 = re.sub(r"\s+", " ", str(cells[2] or "")).strip().lower()
    c11 = re.sub(r"\s+", " ", str(cells[11] or "")).strip().lower()
    return bool(re.search(r"symbol", c2)) and bool(re.search(r"open.*vol", c11))

def parse_mts_pdf(pdf_path: str | Path, amount_tol: float = 0.01) -> tuple[pd.DataFrame, dict]:
    import pymupdf
    doc = pymupdf.open(str(pdf_path))
    first_text = doc[0].get_text()
    DATE_RE = re.compile(r"([A-Za-z]+ \d{1,2}, \d{4}|\d{1,2}[-/ ][A-Za-z]{3}[-/ ]\d{4}|\d{4}-\d{2}-\d{2})")
    dm = DATE_RE.search(first_text)
    if dm is None:
        doc.close()
        raise ParseIntegrityError("Report date not found in PDF text")
    try:
        report_date = pd.Timestamp(dm.group(1)).strftime("%Y-%m-%d")
    except Exception:
        report_date = pd.to_datetime(dm.group(1)).strftime("%Y-%m-%d")

    records, rejected, header_seen, grand_total = [], [], False, None
    for pno in range(len(doc)):
        page = doc[pno]
        for tab in page.find_tables():
            for r in tab.extract():
                if _is_header(r):
                    header_seen = True
                    continue
                if len(r) < 14:
                    continue
                sym_raw = re.sub(r"[^A-Z0-9]", "", str(r[2] or "").upper())
                if not sym_raw or sym_raw in NON_SYMBOL_ROWS or "TOTAL" in re.sub(r"\s+", " ", " ".join(map(str, r[:3]))).upper():
                    gt = to_number(r[12])
                    if np.isfinite(gt) and gt > 1e8:
                        grand_total = gt
                    continue
                if not re.fullmatch(r"[A-Z][A-Z0-9]{1,13}", sym_raw):
                    rejected.append((pno, sym_raw, r))
                    continue
                rec = {
                    "raw_symbol": sym_raw,
                    "new_mts_volume": to_number(r[8]),
                    "new_mts_amount": to_number(r[9]),
                    "weighted_rate": to_number(r[10]),
                    "mts_volume": to_number(r[11]),
                    "mts_amount": to_number(r[12]),
                    "open_pct": to_number(r[13]),
                }
                if not np.isfinite(rec["mts_volume"]) or not np.isfinite(rec["open_pct"]):
                    rejected.append((pno, sym_raw, r))
                    continue
                records.append(rec)

    doc.close()
    if not header_seen:
        raise ParseIntegrityError("Expected header layout not found in PDF tables")
    df = pd.DataFrame.from_records(records)
    if df.empty:
        raise ParseIntegrityError("No data rows parsed from PDF")
    if (df["open_pct"] < 0).any() or (df["open_pct"] > 100).any():
        raise ParseIntegrityError("open_pct out of [0, 100] bounds")
    if not df["weighted_rate"].between(5.0, 35.0).all():
        raise ParseIntegrityError("weighted_rate out of range [5, 35]")
    if df["open_pct"].median() > 5.0:
        raise ParseIntegrityError("open_pct distribution implausible (median > 5%)")

    diag = {
        "report_date": report_date,
        "n_rows": len(df),
        "n_rejected": len(rejected),
        "grand_total": grand_total,
        "parsed_total": float(df["mts_amount"].sum())
    }
    if grand_total is not None:
        diff = abs(diag["parsed_total"] - grand_total)
        max_diff = max(1.0, 1e-7 * abs(grand_total))
        if diff > max_diff:
            raise ParseIntegrityError(f"Amount reconciliation failed: parsed {diag['parsed_total']}, grand {grand_total}, diff {diff} > tol {max_diff}")
    return df, diag





_SUFFIX_RE = re.compile(r"^(?P<base>[A-Z0-9]+?)(?P<suf>XDXB|XDXR|XBXR|XD|XB|XR)$")





class SymbolSanitizer:

    """known_base: canonical base symbols (e.g. from your 56-transition mapping + quotes)."""



    def __init__(self, known_base: set[str], explicit_map: Mapping[str, str] | None = None):

        self.known = {s.upper() for s in known_base}

        self.explicit = {k.upper(): v.upper() for k, v in (explicit_map or {}).items()}



    def __call__(self, raw: str) -> str:

        s = re.sub(r"[^A-Z0-9]", "", str(raw).upper())

        if s in self.explicit:

            return self.explicit[s]

        if s in self.known:

            return s

        m = _SUFFIX_RE.match(s)

        if m and m["base"] in self.known:

            return m["base"]

        raise UnknownSymbolError(s)





def ingest_report(conn: sqlite3.Connection, pdf_path: str | Path, report_date: str,

                  captured_at: str, sanitizer: SymbolSanitizer, source_url: str | None = None,

                  patterns: Mapping[str, str] | None = None,

                  denom_jump_tol: float = 0.05) -> pd.DataFrame:

    ts = pd.Timestamp(captured_at)

    if ts.tzinfo is None:

        raise ValueError("captured_at must be timezone-aware (e.g. 2026-09-22T18:05:00+05:00)")

    rd = pd.Timestamp(report_date).strftime("%Y-%m-%d")

    blob = Path(pdf_path).read_bytes()

    sha = hashlib.sha256(blob).hexdigest()



    row = conn.execute("SELECT report_date FROM mts_raw_reports WHERE sha256=?", (sha,)).fetchone()

    if row is not None and row[0] != rd:

        raise StaleReportError(f"PDF {sha[:12]} already archived as {row[0]}, not {rd}")

    if row is None:

        conn.execute("INSERT INTO mts_raw_reports VALUES (?,?,?,?,?)",

                     (rd, ts.isoformat(), source_url, sha, blob))



    existing = conn.execute("SELECT DISTINCT report_sha256 FROM mts_snapshots WHERE report_date=?",

                            (rd,)).fetchall()

    if existing:

        if existing[0][0] != sha:

            conn.execute("INSERT INTO mts_anomalies VALUES (?,?,?,?)",

                         (rd, "*", "REVISION_IGNORED", f"first={existing[0][0][:12]} new={sha[:12]}"))

            log.warning("Report %s revised; first capture kept (PIT).", rd)

        conn.commit()

        return pd.read_sql_query("SELECT * FROM mts_snapshots WHERE report_date=?", conn, params=(rd,))



    df, diag = parse_mts_pdf(pdf_path)
    rd = diag["report_date"]
    df["symbol"] = [sanitizer(s) for s in df["raw_symbol"]]
    dup = df["symbol"].duplicated(keep=False)

    if dup.any():

        raise ValueError(f"Duplicate symbols after sanitizing: {sorted(df.loc[dup, 'symbol'].unique())}")

    pct = df["open_pct"]
    df["implied_denominator"] = np.where(pct > 0, df["mts_volume"] / (pct / 100.0), np.nan)
    df["report_date"] = rd
    df["captured_at"] = ts.isoformat()
    df["report_sha256"] = sha

    # Sanity: implied price vs close on report date (catches column shift between volume/amount)
    quotes_close = pd.read_sql_query(
        "SELECT base_symbol AS symbol, close FROM daily_quotes WHERE trade_date=? AND is_final=1",
        conn, params=(rd,)
    )
    if not quotes_close.empty:
        chk_m = df.merge(quotes_close, on="symbol", how="inner")
        if not chk_m.empty:
            implied_px = chk_m["mts_amount"] / chk_m["mts_volume"]
            ratios = implied_px / chk_m["close"]
            mismatches = chk_m[~ratios.between(0.5, 1.8)]
            if not mismatches.empty:
                raise ParseIntegrityError(
                    f"Implied price vs close ratio mismatch for {mismatches['symbol'].tolist()}; "
                    f"possible column shift between volume/amount fields!"
                )

    cols = ["report_date", "symbol", "raw_symbol", "mts_volume", "mts_amount", "new_mts_volume",
            "new_mts_amount", "weighted_rate", "open_pct", "implied_denominator",
            "captured_at", "report_sha256"]
    conn.executemany(f"INSERT INTO mts_snapshots({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                     df[cols].itertuples(index=False, name=None))

    _log_denominator_anomalies(conn, df, rd, denom_jump_tol)

    conn.commit()

    return df[cols]





def _log_denominator_anomalies(conn, df, rd, tol):

    prev = pd.read_sql_query(

        "SELECT symbol, implied_denominator AS d_prev FROM mts_snapshots WHERE report_date="

        "(SELECT MAX(report_date) FROM mts_snapshots WHERE report_date < ?)", conn, params=(rd,))

    if prev.empty:

        return

    m = df[["symbol", "implied_denominator"]].merge(prev, on="symbol")

    jump = (m["implied_denominator"] / m["d_prev"] - 1.0).abs()

    for _, r in m[jump > tol].iterrows():

        conn.execute("INSERT INTO mts_anomalies VALUES (?,?,?,?)",

                     (rd, r.symbol, "DENOMINATOR_JUMP", f"{r.d_prev:.0f}->{r.implied_denominator:.0f}"))





ALLOWED_SIGNAL_COLS = {"open_pct", "mts_volume", "mts_amount"}





def load_signal_panel(conn: sqlite3.Connection, signal_col: str = "open_pct") -> pd.DataFrame:

    if signal_col not in ALLOWED_SIGNAL_COLS:

        raise ValueError(signal_col)

    snaps = pd.read_sql_query(

        f"SELECT report_date, symbol, {signal_col} AS L, captured_at FROM mts_snapshots", conn)

    elig = pd.read_sql_query("SELECT effective_date, symbol FROM mts_eligible", conn)

    if elig.empty:
        raise RuntimeError("mts_eligible table is empty. Live pipeline requires official NCCPL eligible securities list to avoid circular selection bias.")

    out = []
    for rd, g in snaps.groupby("report_date"):
        eff = elig.loc[elig["effective_date"] <= rd, "effective_date"]
        if eff.empty:
            raise RuntimeError(f"{rd}: no eligible securities list in force on or prior to this report date. Ingest official NCCPL circular.")

        members = set(elig.loc[elig["effective_date"] == eff.max(), "symbol"])
        missing = sorted(members - set(g["symbol"]))
        extra = set(g["symbol"]) - members
        if extra:
            log.info("%s: %d financed symbols not on eligible list: %s", rd, len(extra), sorted(extra))

        # Vanished-symbol rule: if stock had L > 0 on previous report date but missing today,
        # do NOT assign L = 0 (which dumps it into Q0). Exclude it and log to mts_anomalies.
        prev_rd = snaps.loc[snaps["report_date"] < rd, "report_date"].max()
        if pd.notna(prev_rd):
            prev_pos = set(snaps.loc[(snaps["report_date"] == prev_rd) & (snaps["L"] > 0), "symbol"])
            vanished = set(missing) & prev_pos
            for s in sorted(vanished):
                conn.execute("INSERT INTO mts_anomalies VALUES (?,?,?,?)", (rd, s, "VANISHED", "excluded from cohort"))
            missing = sorted(set(missing) - vanished)

        add = pd.DataFrame({"report_date": rd, "symbol": missing, "L": 0.0,
                            "captured_at": g["captured_at"].iloc[0]})
        out.append(pd.concat([g, add], ignore_index=True))

    conn.commit()
    panel = pd.concat(out, ignore_index=True)
    n_nan = panel["L"].isna().sum()
    if n_nan:
        log.warning("Dropping %d rows with missing signal value.", n_nan)
    return panel.dropna(subset=["L"])





@dataclass(frozen=True)
class SignalConfig:
    n_quantiles: int = 5
    min_volume: float = 50_000
    min_price: float = 10.0
    min_names: int = 25
    min_financed: int = 40
    signal_col: str = "open_pct"


def assign_buckets(L: pd.Series, n_q: int = 5, min_financed: int = 40) -> np.ndarray | None:
    """
    Separates non-financed stocks (L == 0) into 'Q0' (control bucket),
    and strictly partitions financed stocks (L > 0) into Q1..Q5 quintiles.
    Prevents empty Q1 collapse and rank dilution.
    Raises ValueError if NaN or negative values reach bucketing.
    Returns None if financed symbols count < min_financed.
    """
    v = pd.to_numeric(L, errors="coerce").to_numpy(float)
    if np.isnan(v).any() or (v < 0).any():
        raise ValueError("NaN/negative L reached bucketing; upstream bug")
    pos = v > 0
    if pos.sum() < min_financed:
        return None
    out = np.full(len(v), "Q0", dtype=object)
    r = pd.Series(v[pos]).rank(method="average").to_numpy()
    out[pos] = [f"Q{k}" for k in np.clip(np.ceil(n_q * r / pos.sum()), 1, n_q).astype(int)]
    return out


def build_cohorts(signal: pd.DataFrame, quotes: pd.DataFrame, cfg: SignalConfig) -> pd.DataFrame:
    """quotes: long frame [date(YYYY-MM-DD), symbol, close, volume] raw formation-day values.
    Filter FIRST (liquid, eligible), THEN rank."""
    sessions = set(quotes["date"])
    m = signal.merge(quotes[["date", "symbol", "close", "volume"]],
                     left_on=["report_date", "symbol"], right_on=["date", "symbol"], how="left")
    rows = []
    for rd, g in m.groupby("report_date"):
        if rd not in sessions:
            log.warning("%s is not a trading session; skipped.", rd)
            continue
        liq = g[(g["volume"] >= cfg.min_volume) & (g["close"] >= cfg.min_price)]
        if len(liq) < cfg.min_names:
            log.warning("%s: only %d liquid eligible names (<%d); no cohort.", rd, len(liq), cfg.min_names)
            continue
        b = assign_buckets(liq["L"], cfg.n_quantiles, min_financed=cfg.min_financed)
        if b is None:
            log.warning("%s: fewer than %d financed active symbols; no cohort.", rd, cfg.min_financed)
            continue
        rows.append(pd.DataFrame({
            "formation_date": rd, "symbol": liq["symbol"].to_numpy(),
            "L": liq["L"].to_numpy(), "bucket": b,
            "captured_at": liq["captured_at"].to_numpy()
        }))
    cols = ["formation_date", "symbol", "L", "bucket", "captured_at"]
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=cols)





def enforce_publication_lag(cohorts: pd.DataFrame, sessions: list[str], lag: int = 2,

                            open_time: str = "09:30") -> pd.DataFrame:

    """entry_date = sessions[idx(formation)+lag]; cohort dropped if captured_at >= entry open."""

    idx = {d: i for i, d in enumerate(sessions)}

    hh, mm = map(int, open_time.split(":"))

    keep = []

    for fd, g in cohorts.groupby("formation_date"):

        i = idx.get(fd)

        if i is None or i + lag >= len(sessions):

            continue

        entry = sessions[i + lag]

        entry_open = (pd.Timestamp(entry).tz_localize(PKT)

                      + pd.Timedelta(hours=hh, minutes=mm)).tz_convert("UTC")

        cap = pd.to_datetime(g["captured_at"], utc=True).max()

        if cap >= entry_open:

            log.warning("%s: captured %s after entry open %s; dropped.", fd, cap, entry_open)

            continue

        keep.append(g.assign(entry_date=entry))

    return pd.concat(keep, ignore_index=True) if keep else cohorts.iloc[0:0].assign(entry_date=[])





def entry_schedule(cohorts: pd.DataFrame, bucket: str | None) -> dict[str, list[str]]:

    """bucket='Q5' etc.; bucket=None or 'U' -> full eligible liquid universe."""

    g = cohorts if bucket in (None, "U") else cohorts[cohorts["bucket"] == bucket]

    return {d: sorted(x["symbol"].unique()) for d, x in g.groupby("entry_date")}





def is_upper_locked(open_px: float, upper_limit: float, tol: float = 0.005) -> bool:

    return bool(open_px >= upper_limit * (1.0 - tol))





def is_lower_locked(open_px: float, lower_limit: float, tol: float = 0.005) -> bool:

    return bool(open_px <= lower_limit * (1.0 + tol))