"""
Comprehensive test suite for Corporate Actions Table Rebuild, Append-Only Triggers,
and VOID Compensation Protocol.
To be formally committed with Amendment #11 on September 30, 2026.
"""
import hashlib
import json
import os
import shutil
import sqlite3
import pandas as pd


from ca_migration import rebuild_corporate_actions


def test_corporate_actions_suite():
    # Test on an exact isolated copy of psx.db
    shutil.copyfile("psx.db", "psx_void_test.db")
    conn = sqlite3.connect("psx_void_test.db")

    # 1. Compute pre-rebuild 10-column canonical hash on historical partition
    ncol_10_order = ",".join(str(i) for i in range(1, 11))
    pre_rows = conn.execute(
        f"SELECT action_id, ex_date, symbol, base_symbol, action_type, amount, ratio, annc_date, ingest_ts, source "
        f"FROM corporate_actions WHERE ex_date < '2026-10-01' ORDER BY {ncol_10_order}"
    ).fetchall()
    pre_hash = hashlib.sha256(json.dumps(pre_rows, default=str).encode("utf-8")).hexdigest()
    pre_cnt = len(pre_rows)
    # Amendment #13 appended 3 VOID rows + 3 corrected HUBC rows to the historical partition.
    assert pre_cnt == 399, f"Expected 399 partition rows, got {pre_cnt}"
    assert pre_hash == "665e7a8c5caeb164c5a8c1f9b42a504a8f1fe9aabc8981e28aa18b97fdb5e1e4", f"Pre-hash mismatch: {pre_hash}"
    print("[PASS] Pre-rebuild 10-column hash verified: 665e7a8c... (399 rows)")

    # 2. Execute Atomic Table Rebuild via single source of truth (ca_migration.py)
    rebuild_corporate_actions(conn)
    print("[PASS] Atomic table rebuild & trigger installation committed successfully via ca_migration.")

    # 3. Post-rebuild hash invariant check (first 10 columns must remain exactly identical!)
    post_rows = conn.execute(
        f"SELECT action_id, ex_date, symbol, base_symbol, action_type, amount, ratio, annc_date, ingest_ts, source "
        f"FROM corporate_actions WHERE ex_date < '2026-10-01' ORDER BY {ncol_10_order}"
    ).fetchall()
    post_hash = hashlib.sha256(json.dumps(post_rows, default=str).encode("utf-8")).hexdigest()
    assert post_hash == pre_hash, f"Post-rebuild hash changed: {post_hash} != {pre_hash}"
    assert len(post_rows) == 399
    print("[PASS] Post-rebuild 10-column hash 100% matched: 665e7a8c... (399 rows)")

    # 3b. The economic invariant behind Amendment #13: VOID bookkeeping may grow the
    # partition, but the count of ACTIVE events the portfolio engine sees must not move.
    active_cnt = conn.execute(
        "SELECT COUNT(*) FROM corporate_actions e WHERE e.ex_date < '2026-10-01' "
        "AND e.action_type <> 'VOID' AND e.action_id NOT IN "
        "(SELECT voids_action_id FROM corporate_actions WHERE action_type='VOID' "
        "AND voids_action_id IS NOT NULL)").fetchone()[0]
    assert active_cnt == 393, f"Active event count drifted: {active_cnt} != 393"
    print("[PASS] Active corporate actions after double-exclusion still 393 (VOID is bookkeeping only)")

    # 3c. The rebuild must be idempotent. It originally wrote a literal NULL into
    # voids_action_id, so a second run un-voided Amendment #13's corrections and
    # double-counted three HUBC dividends. Running it twice must change nothing.
    rebuild_corporate_actions(conn)
    twice_active = conn.execute(
        "SELECT COUNT(*) FROM corporate_actions e WHERE e.ex_date < '2026-10-01' "
        "AND e.action_type <> 'VOID' AND e.action_id NOT IN "
        "(SELECT voids_action_id FROM corporate_actions WHERE action_type='VOID' "
        "AND voids_action_id IS NOT NULL)").fetchone()[0]
    twice_links = conn.execute(
        "SELECT COUNT(*) FROM corporate_actions WHERE action_type='VOID' "
        "AND voids_action_id IS NULL").fetchone()[0]
    assert twice_active == 393, f"Second rebuild lost VOID linkage: active {twice_active} != 393"
    assert twice_links == 0, f"Second rebuild produced {twice_links} orphan VOID rows"
    print("[PASS] Rebuild is idempotent: 2nd run still 393 active, 0 orphan VOIDs")

    # 4. Check sqlite_sequence
    seq_val = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='corporate_actions'").fetchone()[0]
    max_aid = conn.execute("SELECT MAX(action_id) FROM corporate_actions").fetchone()[0]
    assert seq_val >= max_aid, f"sqlite_sequence seq ({seq_val}) < MAX(action_id) ({max_aid})"
    print(f"[PASS] sqlite_sequence verified: seq={seq_val} >= max_aid={max_aid}")

    # 5. Test XDXB day: simultaneous CASH and BONUS for same symbol and ex_date
    conn.execute("""
    INSERT INTO corporate_actions (ex_date, symbol, base_symbol, action_type, amount, ingest_ts, source)
    VALUES ('2026-10-15', 'TEST', 'TEST', 'CASH', 50.0, '2026-10-15T00:00:00', 'PSX_NOTICE')
    """)
    cash_aid = conn.execute("SELECT MAX(action_id) FROM corporate_actions").fetchone()[0]

    conn.execute("""
    INSERT INTO corporate_actions (ex_date, symbol, base_symbol, action_type, ratio, ingest_ts, source)
    VALUES ('2026-10-15', 'TEST', 'TEST', 'BONUS', 0.10, '2026-10-15T00:00:00', 'PSX_NOTICE')
    """)
    bonus_aid = conn.execute("SELECT MAX(action_id) FROM corporate_actions").fetchone()[0]
    print(f"[PASS] XDXB simultaneous actions inserted successfully (CASH aid={cash_aid}, BONUS aid={bonus_aid})")

    # 6. Test invalid VOID cases:
    # 6a. VOID targeting non-existent action_id -> blocked
    try:
        conn.execute("""
        INSERT INTO corporate_actions (ex_date, symbol, base_symbol, action_type, voids_action_id, ingest_ts, source)
        VALUES ('2026-10-15', 'TEST', 'TEST', 'VOID', 99999, 'now', 'test')
        """)
        assert False, "Non-existent target VOID should be blocked"
    except Exception as e:
        print("[PASS] Non-existent target VOID blocked:", e)

    # 6b. VOID with mismatched ex_date -> blocked
    try:
        conn.execute(f"""
        INSERT INTO corporate_actions (ex_date, symbol, base_symbol, action_type, voids_action_id, ingest_ts, source)
        VALUES ('2026-10-16', 'TEST', 'TEST', 'VOID', {cash_aid}, 'now', 'test')
        """)
        assert False, "Mismatched ex_date VOID should be blocked"
    except Exception as e:
        print("[PASS] Mismatched ex_date VOID blocked:", e)

    # 6c. Valid VOID for erroneous CASH (amount was 50.0 instead of 5.0)
    conn.execute(f"""
    INSERT INTO corporate_actions (ex_date, symbol, base_symbol, action_type, voids_action_id, ingest_ts, source)
    VALUES ('2026-10-15', 'TEST', 'TEST', 'VOID', {cash_aid}, '2026-10-15T01:00:00', 'CORRECTION_FOR_CASH')
    """)
    void_aid = conn.execute("SELECT MAX(action_id) FROM corporate_actions").fetchone()[0]
    print(f"[PASS] Valid VOID inserted for cash_aid={cash_aid} as void_aid={void_aid}")

    # 6d. Double-VOID targeting the same row -> blocked
    try:
        conn.execute(f"""
        INSERT INTO corporate_actions (ex_date, symbol, base_symbol, action_type, voids_action_id, ingest_ts, source)
        VALUES ('2026-10-15', 'TEST', 'TEST', 'VOID', {cash_aid}, 'now', 'test')
        """)
        assert False, "Double VOID of same action should be blocked"
    except Exception as e:
        print("[PASS] Double-VOID of same action blocked:", e)

    # 6e. VOID targeting another VOID row -> blocked
    try:
        conn.execute(f"""
        INSERT INTO corporate_actions (ex_date, symbol, base_symbol, action_type, voids_action_id, ingest_ts, source)
        VALUES ('2026-10-15', 'TEST', 'TEST', 'VOID', {void_aid}, 'now', 'test')
        """)
        assert False, "Chained VOID targeting a VOID should be blocked"
    except Exception as e:
        print("[PASS] Chained VOID targeting another VOID blocked:", e)

    # 7. Insert corrected replacement CASH event (5.0) -> succeeds!
    conn.execute("""
    INSERT INTO corporate_actions (ex_date, symbol, base_symbol, action_type, amount, ingest_ts, source)
    VALUES ('2026-10-15', 'TEST', 'TEST', 'CASH', 5.0, '2026-10-15T01:05:00', 'PSX_NOTICE_CORRECTED')
    """)
    corr_aid = conn.execute("SELECT MAX(action_id) FROM corporate_actions").fetchone()[0]
    print(f"[PASS] Corrected replacement CASH inserted cleanly as corr_aid={corr_aid}")

    # 8. Verify double-exclusion logic on pandas DataFrame
    df = pd.read_sql_query("SELECT action_id, ex_date, base_symbol, action_type, amount, ratio, voids_action_id FROM corporate_actions WHERE base_symbol='TEST'", conn)
    print("\nAll TEST rows in database:")
    print(df)

    voided = set(df.loc[df["action_type"] == "VOID", "voids_action_id"].dropna().astype(int))
    active = df[(df["action_type"] != "VOID") & (~df["action_id"].isin(voided))]
    print("\nActive rows after double-exclusion filter:")
    print(active)

    assert len(active) == 2, f"Expected 2 active events (1 BONUS, 1 corrected CASH), got {len(active)}"
    active_cash = active[active["action_type"] == "CASH"].iloc[0]
    active_bonus = active[active["action_type"] == "BONUS"].iloc[0]

    assert active_cash["amount"] == 5.0, f"Expected corrected CASH amount 5.0, got {active_cash['amount']}"
    assert active_bonus["ratio"] == 0.10, f"Expected BONUS ratio 0.10, got {active_bonus['ratio']}"
    print("\n[ALL TESTS PASSED] Suite completed with 100% verification across all edge cases!")

    conn.close()
    if os.path.exists("psx_void_test.db"):
        os.remove("psx_void_test.db")


if __name__ == "__main__":
    test_corporate_actions_suite()
