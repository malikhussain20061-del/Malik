"""Build sector_map: the single locked sector assignment used by the sector-neutral
spread and the interpretation gate (Amendment #17).

Why this table exists: daily_quotes.sector mixes PSX's 4-digit sector codes with names
copied from a hand-maintained local file. The two disagree - code 0823 appears against
both PHARMACEUTICALS and REFINERY - so the dict the pipeline built with
    SELECT DISTINCT base_symbol, sector FROM daily_quotes  ->  dict(zip(...))
silently split one real sector into two groups, and the sector-neutral test that decides
"Crowding vs Sector Exposure" was computed on that grouping.

Canonical key is the 4-digit code, because it is the exchange's own field and it agreed
with dps.psx.com.pk on 429 of 429 comparable symbols with zero conflicts and zero
multi-code symbols. Names are stored only as an unverified label and are never used to group.

Point-in-time rule: one pre-freeze snapshot, locked for the whole 240-session run. A
symbol that changes sector mid-run keeps the snapshot value, so the universe definition
cannot drift while results are being observed.
"""

import argparse
import re
import sqlite3
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mts_engine as ME

SNAPSHOT_DATE = "2026-09-24"
MW_URL = "https://dps.psx.com.pk/market-watch"
CODE_RE = re.compile(r"^\d{4}$")
ROW_RE = re.compile(r'data-search="([A-Z0-9-]+)"[^>]*>.*?<td>\s*(\d{4})\s*</td>', re.S)
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def fetch_dps() -> dict[str, str]:
    """symbol -> sector code, from the exchange's own market-watch table.

    The row for a defaulted ticker carries an extra <div class="tag">NC</div> between the
    link and the cell boundary, so a pattern anchored on </a> silently drops those symbols.
    Anchoring on the row's data-search attribute and the next 4-digit cell does not.
    """
    html = urllib.request.urlopen(urllib.request.Request(MW_URL, headers=UA), timeout=30).read()
    html = html.decode("utf-8", "ignore")
    out = {}
    for row in html.split("<tr>"):
        m = ROW_RE.search(row)
        if m:
            out.setdefault(m.group(1), m.group(2))
    if len(out) < 400:
        raise RuntimeError(f"DPS parse regressed: only {len(out)} symbol->code rows parsed")
    return out


def db_codes(conn) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for b, s in conn.execute("SELECT base_symbol, sector FROM daily_quotes "
                             "WHERE sector IS NOT NULL AND sector <> '' GROUP BY 1,2"):
        if b and CODE_RE.match(s):
            out.setdefault(b, set()).add(s)
    return out


def infer_names(conn) -> dict[str, str]:
    """code -> name, kept only where every symbol carrying both forms agrees."""
    pairs: dict[str, set[str]] = {}
    per: dict[str, dict[str, set[str]]] = {}
    for b, s in conn.execute("SELECT base_symbol, sector FROM daily_quotes "
                             "WHERE sector IS NOT NULL AND sector <> '' GROUP BY 1,2"):
        if not b:
            continue
        per.setdefault(b, {"c": set(), "n": set()})["c" if CODE_RE.match(s) else "n"].add(s)
    for b, v in per.items():
        if len(v["c"]) == 1 and len(v["n"]) == 1:
            pairs.setdefault(next(iter(v["c"])), set()).add(next(iter(v["n"])))
    return {c: next(iter(n)) for c, n in pairs.items() if len(n) == 1}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="psx.db")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    conn = sqlite3.connect(a.db)
    ME.init_schema(conn)

    dps = fetch_dps()
    dbc = db_codes(conn)
    names = infer_names(conn)

    conflicts = {s: (sorted(v), dps[s]) for s, v in dbc.items()
                 if s in dps and len(v) == 1 and next(iter(v)) != dps[s]}
    multi = {s: sorted(v) for s, v in dbc.items() if len(v) > 1}
    print(f"[*] DPS snapshot {SNAPSHOT_DATE}: {len(dps)} symbols")
    print(f"[*] DB code conflicts vs DPS: {len(conflicts)} | DB multi-code symbols: {len(multi)}")
    for s, v in list(conflicts.items())[:5]:
        print(f"      {s}: DB {v[0]} vs DPS {v[1]}")

    rows, missing = [], []
    for (sym,) in conn.execute("SELECT symbol FROM mts_eligible ORDER BY symbol"):
        code = dps.get(sym)
        src = "DPS_MARKET_WATCH"
        if code is None:
            dbv = dbc.get(sym)
            if dbv and len(dbv) == 1:
                code, src = next(iter(dbv)), "DAILY_QUOTES_CODE"
        if code is None:
            missing.append(sym)
            continue
        rows.append((sym, code, names.get(code), src, SNAPSHOT_DATE))

    universe = {r[0] for r in rows} | set(missing)
    print(f"[*] eligible universe: {len(universe)} | mapped: {len(rows)} | UNMAPPED: {len(missing)}")
    if missing:
        raise SystemExit(f"REFUSING to write: {len(missing)} symbols have no sector from any "
                         f"source: {missing}")
    for s, v in multi.items():
        if s in universe:
            raise SystemExit(f"REFUSING to write: {s} carries multiple sector codes {v}")

    distinct = sorted({r[1] for r in rows})
    print(f"[*] distinct sectors across the universe: {len(distinct)}")
    print(f"    (daily_quotes.sector would have presented {len(set(distinct) | set(names.values()))} "
          f"groups because codes and names were counted separately)")

    if a.dry_run:
        print("\n[DRY RUN] nothing written.")
        return

    conn.execute("DELETE FROM sector_map")
    conn.executemany("INSERT INTO sector_map(symbol, sector_code, sector_name, source, snapshot_date) "
                     "VALUES (?,?,?,?,?)", rows)
    conn.commit()
    sha, cnt = ME.sector_map_sha256(conn)
    print(f"\n[+] sector_map written: {cnt} rows, sha256={sha}")
    conn.close()


if __name__ == "__main__":
    main()
