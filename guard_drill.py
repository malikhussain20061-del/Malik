"""
guard_drill.py — Diagnostic tool for daily shadow guard verification and negative tampering tests.
Note: Kept outside CODE_FILES so it does not affect cryptographic code hashes.
"""
import os
import shutil
import sqlite3
import sys
from pathlib import Path

# Ensure root directory is on sys.path
sys.path.insert(0, str(Path(".").resolve()))
import run_mts_h1

SCRATCH_DIR = Path("scratch")
SCRATCH_DIR.mkdir(parents=True, exist_ok=True)


def run_positive_drill(db_src: str = "psx.db") -> bool:
    """
    Daily post-shadow drill:
    Runs evaluate on an isolated DB copy to verify all runtime tamper guards pass
    before halting at warm-up standby, with zero impact on live ledger.
    """
    drill_db = SCRATCH_DIR / "guard_drill.db"
    if drill_db.exists():
        drill_db.unlink()

    src = sqlite3.connect(db_src)
    dst = sqlite3.connect(drill_db)
    src.backup(dst)
    dst.close()
    src.close()

    print("[*] Running positive guard drill on isolated copy...")
    try:
        run_mts_h1.main("evaluate", db=str(drill_db))
        print("[PASS] Positive drill: all runtime guards verified and halted cleanly at warm-up standby.")
        return True
    except SystemExit as e:
        print(f"[FAIL] Positive drill failed with SystemExit: {e}")
        return False
    except Exception as e:
        print(f"[FAIL] Positive drill failed with unexpected error: {e}")
        return False
    finally:
        import gc
        gc.collect()
        try:
            if drill_db.exists():
                drill_db.unlink()
        except Exception:
            pass


def run_negative_drill(name: str, tamper_fn) -> bool:
    """
    Negative tamper test: intentionally corrupts a specific invariant
    on an isolated copy and verifies that evaluate halts immediately with SystemExit.
    """
    neg_db = SCRATCH_DIR / f"neg_{name}.db"
    try:
        if neg_db.exists():
            neg_db.unlink()
    except Exception:
        pass

    src = sqlite3.connect("psx.db")
    dst = sqlite3.connect(neg_db)
    src.backup(dst)
    src.close()

    # Apply intentional tampering
    tamper_fn(dst)
    dst.commit()
    dst.close()

    try:
        run_mts_h1.main("evaluate", db=str(neg_db))
        print(f"[FAIL] Negative drill '{name}': guard did NOT fire! Pipeline proceeded illegally.")
        return False
    except SystemExit as e:
        print(f"[PASS] Negative drill '{name}' caught tampering:\n       {str(e)[:90]}...")
        return True
    except Exception as e:
        print(f"[WARN] Negative drill '{name}' raised unexpected exception: {e}")
        return False
    finally:
        import gc
        gc.collect()
        try:
            if neg_db.exists():
                neg_db.unlink()
        except Exception:
            pass


def run_all_negative_tests():
    print("\n=== RUNNING COMPREHENSIVE NEGATIVE TAMPER DRILLS ===")

    # 1. Tamper append-only triggers (drop ca_no_delete)
    run_negative_drill(
        "trigger_dropped",
        lambda c: c.execute("DROP TRIGGER ca_no_delete")
    )

    # 2. Tamper historical corporate actions partition (< 2026-10-01)
    run_negative_drill(
        "historical_ca_tampered",
        lambda c: c.execute(
            "INSERT INTO corporate_actions (ex_date, symbol, base_symbol, action_type, amount, ingest_ts, source) "
            "VALUES ('2026-09-01', 'TAMPER', 'TAMPER', 'CASH', 1.0, '2026-09-01T00:00:00', 'tamper')"
        )
    )

    # 3. Tamper MTS eligible universe (delete 1 symbol)
    run_negative_drill(
        "universe_tampered",
        lambda c: c.execute("DELETE FROM mts_eligible WHERE rowid = (SELECT MIN(rowid) FROM mts_eligible)")
    )

    # 4. Tamper forward corporate action provenance (insert forward action with missing source)
    run_negative_drill(
        "forward_ca_missing_provenance",
        lambda c: c.execute(
            "INSERT INTO corporate_actions (ex_date, symbol, base_symbol, action_type, amount, ingest_ts, source) "
            "VALUES ('2026-10-15', 'TEST', 'TEST', 'CASH', 1.0, '2026-10-15T00:00:00', '')"
        )
    )


if __name__ == "__main__":
    pos_ok = run_positive_drill()
    run_all_negative_tests()
