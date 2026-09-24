"""
repair_base_symbol.py — Amendment #13 data repair (quotes join key + VOID of CA rows).

The stored daily_quotes.base_symbol column was written by an older suffix stripper
that treated trailing letters of genuine tickers as ex-dividend markers, so 8 real
PSX tickers were filed under phantom bases that do not exist on the exchange
(HUBC->HU, PABC->PA, AMTEX->AMT, BLUEX->BLU, GRR->GR, SRR->SR, JSRR->JSR,
BAFL-JUNC->BAFL-JU). mts_eligible and the MTS report use the real tickers, so those
names silently fell out of the panel and the MTS ingest aborted on them.

Run against a scratch copy first; --apply-live is required to touch psx.db.
"""

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psx_data_v2 as P

EX_SUFFIXES = P.EX_SUFFIXES


def traded_symbols(conn) -> set[str]:
    """Proxy for the official listed-company directory: every ticker the exchange
    itself reported into our quotes feed. Cross-checked against dps.psx.com.pk
    market-watch on 2026-09-24 (HU/AMT/BLU/GR/SR/JSR/PA/BAFL-JU absent, HUBC/AMTEX/
    BLUEX/GRR/SRR/JSRR/PABC present)."""
    return {r[0] for r in conn.execute(
        "SELECT DISTINCT symbol FROM daily_quotes WHERE symbol IS NOT NULL") if r[0]}


def derive_base(sym: str, known: set[str]) -> str:
    """Strip a legal PSX ex-suffix only when the remainder is itself a traded ticker.

    The old function returned the symbol unchanged whenever it appeared in `known`,
    which stopped real ex-tickers collapsing; stripping unconditionally invented
    phantom bases. Both failure modes are closed by requiring the stripped form to
    be a ticker the exchange actually reported.
    """
    s = str(sym).upper().strip()
    for suf in EX_SUFFIXES:
        if s.endswith(suf):
            cand = s[:-len(suf)]
            if cand in known:
                return cand
    return s


def plan(conn):
    known = traded_symbols(conn)
    rows = list(conn.execute("SELECT rowid, symbol, base_symbol FROM daily_quotes"))
    changes = [(rid, s, b, derive_base(s, known)) for rid, s, b in rows
               if s and derive_base(s, known) != b]
    pairs = {(r[0], r[1]) for r in conn.execute(
        "SELECT DISTINCT symbol, base_symbol FROM daily_quotes "
        "WHERE symbol IS NOT NULL AND base_symbol IS NOT NULL")}
    phantom = {b: s for s, b in pairs if b not in known}
    if len(set(phantom.values())) != len(phantom):
        raise RuntimeError(f"phantom base maps ambiguously: {phantom}")
    return known, changes, phantom


def repair_quotes(conn, changes) -> int:
    conn.executemany("UPDATE daily_quotes SET base_symbol=? WHERE rowid=?",
                     [(new, rid) for rid, _, _, new in changes])
    conn.commit()
    return len(changes)


def void_and_replace(conn, phantom) -> list[tuple[int, str, str, str]]:
    """VOID each corporate action filed under a phantom base, then append the same
    event under its real ticker. The VOID must carry the target's base_symbol and
    ex_date or ca_no_replace rejects it."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    done = []
    for bad_base, real_sym in sorted(phantom.items()):
        targets = list(conn.execute(
            "SELECT action_id, ex_date, symbol, action_type, amount, ratio, annc_date, source "
            "FROM corporate_actions WHERE base_symbol=? AND action_type<>'VOID'", (bad_base,)))
        for t in targets:
            conn.execute(
                "INSERT INTO corporate_actions(ex_date, symbol, base_symbol, action_type, "
                "annc_date, ingest_ts, source, voids_action_id) VALUES (?,?,?,?,?,?,?,?)",
                (t[1], t[2], bad_base, "VOID", t[6], now,
                 f"AMENDMENT_13_BASE_SYMBOL_REPAIR voids {t[0]}", t[0]))
            conn.execute(
                "INSERT INTO corporate_actions(ex_date, symbol, base_symbol, action_type, "
                "amount, ratio, annc_date, ingest_ts, source) VALUES (?,?,?,?,?,?,?,?,?)",
                (t[1], real_sym, real_sym, t[3], t[4], t[5], t[6], now,
                 f"AMENDMENT_13_CORRECTED from action {t[0]} via base {bad_base}"))
            done.append((t[0], bad_base, real_sym, t[1]))
    conn.commit()
    return done


def verify(conn, freeze_date: str) -> dict:
    known = traded_symbols(conn)
    out = {}
    out["phantom_bases"] = sorted({b for (s, b) in conn.execute(
        "SELECT DISTINCT symbol, base_symbol FROM daily_quotes "
        "WHERE base_symbol IS NOT NULL AND base_symbol NOT IN (SELECT DISTINCT symbol FROM daily_quotes)")})
    out["ex_ticker_collapses"] = {s: derive_base(s, known) for s in ("LUCKXD", "SYSXB", "AHLXD")}
    out["mts_eligible_missing_from_panel"] = [r[0] for r in conn.execute(
        "SELECT symbol FROM mts_eligible WHERE symbol NOT IN "
        "(SELECT DISTINCT base_symbol FROM daily_quotes)")]
    out["active_ca_rows"] = conn.execute(
        "SELECT COUNT(*) FROM corporate_actions WHERE action_type<>'VOID' "
        "AND action_id NOT IN (SELECT voids_action_id FROM corporate_actions "
        "WHERE action_type='VOID' AND voids_action_id IS NOT NULL)").fetchone()[0]
    import run_mts_h1 as R
    out["ca_partition"] = R.table_sha256(conn, "corporate_actions", "WHERE ex_date < ?", (freeze_date,))
    out["triggers"] = sorted(r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='corporate_actions'"))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="psx.db")
    ap.add_argument("--apply-live", action="store_true")
    ap.add_argument("--freeze-date", default="2026-10-01")
    a = ap.parse_args()

    if a.apply_live and Path(a.db).name == "psx.db":
        import shutil
        shutil.copy2(a.db, "psx_pre_amendment13_backup.db")
        print("[*] safety copy written: psx_pre_amendment13_backup.db")

    conn = sqlite3.connect(a.db)
    conn.execute("PRAGMA journal_mode=WAL")

    known, changes, phantom = plan(conn)
    print(f"[*] traded-symbol proxy size: {len(known)}")
    print(f"[*] quote rows to re-derive : {len(changes)} across {len({c[1] for c in changes})} symbols")
    print(f"[*] phantom bases           : {phantom}")
    for rid, s, b, new in changes[:6]:
        print(f"      {s:11} {b!r:11} -> {new!r}")

    if not a.apply_live:
        print("\n[DRY RUN] nothing written. Re-run with --apply-live to repair.")
        return

    n = repair_quotes(conn, changes)
    voided = void_and_replace(conn, phantom)
    print(f"\n[+] daily_quotes rows updated: {n}")
    print(f"[+] corporate actions VOIDed + replaced: {len(voided)}")
    for aid, bad, real, exd in voided:
        print(f"      action {aid}: {bad!r} -> {real!r} on {exd}")

    v = verify(conn, a.freeze_date)
    print("\n=== POST-REPAIR VERIFICATION ===")
    for k, val in v.items():
        print(f"  {k}: {val}")
    conn.close()


if __name__ == "__main__":
    main()
