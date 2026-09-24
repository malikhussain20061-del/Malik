"""
================================================================================
  PSX QUANT PIPELINE REGRESSION TEST SUITE
================================================================================
Protects against the 3 verified historical bugs:
  1. LSEFSL 10:1 Stock Split (Corporate Actions / Read-Time Adjustment)
  2. BUXL Consecutive Lower-Lock Trapping (Exit Realism)
  3. LUCK / LUCKXD Suffix Desynchronization (Base Symbol Matching)
================================================================================
"""

import os
import sqlite3
import tempfile

import pandas as pd
from corporate_adjuster import init_corporate_actions_db, seed_known_actions, get_adjusted_series
from psx_scorer import get_psx_circuit_band

SOURCE_DB = "psx.db"


def _isolated_db() -> str:
    """Throwaway copy of the panel. These tests read real quote history, and seeding must
    never collide with the append-only triggers on psx.db."""
    path = os.path.join(tempfile.gettempdir(), "psx_regression_test.db")
    src = sqlite3.connect(SOURCE_DB)
    dst = sqlite3.connect(path)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    return path


DB_PATH = _isolated_db()


def test_lsefsl_split_adjustment():
    print("\n[TEST 1] Verifying LSEFSL 10:1 Stock Split Read-Time Adjustment...")
    con = sqlite3.connect(DB_PATH)
    init_corporate_actions_db(con)
    if con.execute("SELECT COUNT(*) FROM corporate_actions").fetchone()[0] == 0:
        seed_known_actions(con)   # only on an empty panel; the real one is already seeded

    # Fetch LSEFSL quotes by base_symbol
    df = pd.read_sql_query(
        "SELECT trade_date, symbol, base_symbol, ldcp, open, high, low, close, volume "
        "FROM daily_quotes WHERE base_symbol = 'LSEFSL' ORDER BY trade_date ASC",
        con
    )
    assert not df.empty, "LSEFSL data must exist in psx.db"

    adj_df, unexplained = get_adjusted_series(df, con)
    con.close()

    # Pre-split entry on 2026-08-27, post-split exit on 2026-09-03
    row_entry = adj_df[adj_df["trade_date"] == "2026-08-27"].iloc[0]
    row_exit = adj_df[adj_df["trade_date"] == "2026-09-03"].iloc[0]

    # Raw prices
    raw_p0 = row_entry["open"]
    raw_p1 = row_exit["open"]
    raw_ret = (raw_p1 - raw_p0) / raw_p0

    # Adjusted prices
    adj_p0 = row_entry["adj_open"]
    adj_p1 = row_exit["adj_open"]
    adj_ret = (adj_p1 - adj_p0) / adj_p0

    print(f"   Raw Entry: Rs. {raw_p0:.2f} | Raw Exit: Rs. {raw_p1:.2f} -> Raw Return: {raw_ret * 100:.2f}% (OLD BUG)")
    print(f"   Adj Entry: Rs. {adj_p0:.2f} | Adj Exit: Rs. {adj_p1:.2f} -> Adj Return: {adj_ret * 100:.2f}% (FIXED)")

    assert raw_ret < -0.80, "Raw return should show fake crash"
    assert -0.10 < adj_ret < 0.05, f"Adjusted return must be realistic (-4.5%), got {adj_ret * 100:.2f}%"
    print("   [PASS] Test 1 PASSED: Stock split adjustment eliminates fake -90% crash! ✅")


def test_buxl_lower_lock_exit_trapping():
    print("\n[TEST 2] Verifying Lower-Lock Exit Defense on BUXL...")
    con = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        "SELECT trade_date, symbol, base_symbol, ldcp, open, high, low, close, volume "
        "FROM daily_quotes WHERE base_symbol = 'BUXL' ORDER BY trade_date ASC",
        con
    )
    con.close()

    # Look at 2026-09-10 to 2026-09-15 where BUXL was locked limit-down
    for d in ["2026-09-10", "2026-09-11", "2026-09-14", "2026-09-15"]:
        row = df[df["trade_date"] == d].iloc[0]
        ldcp = row["ldcp"]
        open_p = row["open"]
        upper, lower = get_psx_circuit_band(d, ldcp)

        # Check if open is locked at lower limit
        is_locked_down = (open_p <= lower + 0.05) or (open_p <= lower * 1.005)
        print(f"   {d}: LDCP={ldcp:.2f} | Lower Lock={lower:.2f} | Open={open_p:.2f} | Locked Down={is_locked_down}")
        assert is_locked_down, f"BUXL must be detected as locked down on {d}"

    print("   [PASS] Test 2 PASSED: Lower-lock detector correctly flags impossible exits! ✅")


def test_base_symbol_continuity_join():
    print("\n[TEST 3] Verifying Base Symbol Continuity Join...")
    con = sqlite3.connect(DB_PATH)
    # Check distinct symbols under same base_symbol in database
    multi_symbols = con.execute("""
    SELECT base_symbol, COUNT(DISTINCT symbol) as cnt, GROUP_CONCAT(DISTINCT symbol) 
    FROM daily_quotes 
    GROUP BY base_symbol 
    HAVING cnt > 1
    """).fetchall()
    # Amendment #13 guard: a base_symbol must itself be a ticker the exchange reported.
    phantoms = con.execute("""
    SELECT DISTINCT base_symbol FROM daily_quotes
    WHERE base_symbol IS NOT NULL
      AND base_symbol NOT IN (SELECT DISTINCT symbol FROM daily_quotes WHERE symbol IS NOT NULL)
    ORDER BY base_symbol
    """).fetchall()
    con.close()

    print(f"   Stocks with multiple symbols across corporate transitions: {len(multi_symbols)}")
    for item in multi_symbols[:5]:
        print(f"   Base: {item[0]} -> Symbols: {item[2]}")

    assert len(multi_symbols) > 0, "Database must track base_symbol grouping across suffix transitions"

    assert not phantoms, (
        f"Phantom base_symbol rows present ({[p[0] for p in phantoms]}): a real ticker is being "
        "merged into a symbol PSX does not list, which silently drops it from the panel")
    print("   [PASS] Test 3 PASSED: base grouping joins transitions, zero phantom bases ✅")


if __name__ == "__main__":
    print("=" * 80)
    print("       RUNNING QUANT PIPELINE REGRESSION TEST SUITE")
    print("=" * 80)
    test_lsefsl_split_adjustment()
    test_buxl_lower_lock_exit_trapping()
    test_base_symbol_continuity_join()
    print("\n" + "=" * 80)
    print("       ALL 3 REGRESSION TESTS PASSED! 🛡️")
    print("=" * 80)
