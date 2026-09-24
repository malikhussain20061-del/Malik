"""
check_status.py — Simple 1-click status checker for researcher and user.
Displays latest MTS snapshot date, today's daily job status, and ledger security status.
"""
import os
import sqlite3
import sys
from pathlib import Path

# Ensure UTF-8 output on Windows console
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

print("========================================================")
print("       PSX MTS ALPHA - ROZANA STATUS AUR FEED CHECK")
print("========================================================\n")

if not os.path.exists("psx.db"):
    print("[ERROR] psx.db nahi mila! Folder path check karein.")
    sys.exit(1)

conn = sqlite3.connect("psx.db")

# 1. MTS Latest Report Date
try:
    row = conn.execute("SELECT max(report_date) FROM mts_snapshots").fetchone()
    max_date = row[0] if row and row[0] else "No Data"
    print(f" >> Taza Tareen MTS Report Date: {max_date}")
    if max_date == "2026-09-14":
        print("    [!] Note: Report abhi 2026-09-14 par hai. Roz sham check karein.")
    else:
        print("    [OK] Report aage barh chuki hai!")
except Exception as e:
    print(f" >> MTS Report Date: Error - {e}")

# 2. Daily Job Status File
status_file = "daily_job_status.txt"
if os.path.exists(status_file):
    try:
        with open(status_file, "r", encoding="utf-8") as f:
            st = f.read().strip()
        print(f" >> Aakhri Daily Job Status:     {st}")
    except Exception as e:
        print(f" >> Daily Job Status: Error reading file - {e}")
else:
    print(" >> Aakhri Daily Job Status:     No status file yet")

# 3. Registration Check
try:
    import run_mts_h1
    chk = run_mts_h1.verify_registration(conn, run_mts_h1.SPEC["hypothesis_id"], run_mts_h1.SPEC, run_mts_h1.CODE_FILES)
    if chk.get("all_ok"):
        print(f" >> System Security & Ledger:    MEHFOOZ (Amendment #{chk.get('amendment_no')}) - OK")
    else:
        print(f" >> System Security & Ledger:    WARNING - Mismatch: {chk}")
except Exception as e:
    print(f" >> System Security Check: Error - {e}")

conn.close()

print("\n========================================================")
print("  Agar report date aage barhi hai to sab theek chal raha hai.")
print("========================================================\n")
