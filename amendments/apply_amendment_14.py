import json
import sqlite3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(".").resolve()))

from econometric_audit import record_amendment, verify_registration, file_sha256
import run_mts_h1

conn = sqlite3.connect("psx.db")
conn.execute("PRAGMA journal_mode=WAL")

spec = run_mts_h1.SPEC
code_files = run_mts_h1.CODE_FILES

diff_text = """
- daily_job.py (send_alert):
  * Telegram credentials are now read from alerts_config.json first, falling back to the
    TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID environment variables.
  * When neither source supplies credentials the job logs an explicit warning instead of
    silently skipping the notification. Until this change no alert had ever been delivered:
    the credentials were absent from both the process environment and HKCU\\Environment, and
    send_alert wrote only the local status file, so the gap was invisible.
  * alerts_config.json is added to .gitignore; alerts_config.example.json is the tracked template.
  * Motivation for a file over an environment variable: a Task Scheduler job configured as
    "run whether the user is logged on or not" does not load user environment variables, so an
    env-var-only design would break the moment the logon mode is changed.
- test_pipeline_regressions.py:
  * Runs against a throwaway backup copy of psx.db instead of psx.db itself, and only seeds
    corporate_actions when that table is empty. The suite previously aborted on the Amendment #11
    ca_no_replace trigger because seed_known_actions re-inserted rows that already exist.
  * Test 3 now also asserts zero phantom base_symbol values, which is the Amendment #13 invariant.
- test_corporate_actions_void.py:
  * Historical partition expectations moved 393 -> 399 rows and 10-column hash f9f5d688 -> 665e7a8c,
    reflecting the 3 VOID + 3 corrected rows appended by Amendment #13.
  * Added the invariant that actually matters: active events after double-exclusion must remain 393,
    proving VOID bookkeeping changed no economics.
"""

reason = (
    "V3.14 Pre-Data Operational Hardening (no spec, signal, or data change): "
    "1. Alerting credentials move to a gitignored config file with an explicit warning when unset - "
    "verification showed TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID were never configured, so not one pipeline "
    "alert had ever been delivered and the failure was invisible; "
    "2. test_pipeline_regressions.py isolated onto a throwaway DB copy so it stops colliding with the "
    "Amendment #11 append-only triggers it was itself protecting; "
    "3. test_corporate_actions_void.py re-anchored to the Amendment #13 partition (399 rows, 665e7a8c) "
    "and extended to assert the active-event count is still 393, which is the proof that the VOID repair "
    "changed bookkeeping and not economics. "
    "SPEC, hashes and the evaluation_start condition are byte-identical to Amendment #13."
)

res = record_amendment(
    conn=conn, hypothesis_id=spec["hypothesis_id"], amendment_no=14, pre_data=True,
    reason=reason, diff_text=diff_text.strip(), spec=spec, code_paths=code_files
)
print("Recorded Amendment #14:")
print(f"  Spec SHA256: {res['spec_sha256']}")
print(f"  Diff SHA256: {res['diff_sha256']}")

chk = verify_registration(conn, spec["hypothesis_id"], spec, code_files)
print(f"Verify registration: {chk}")
assert chk["all_ok"], f"Verification failed: {chk}"
assert chk["amendment_no"] == 14

code_hashes = {Path(p).name: file_sha256(p) for p in code_files}
anchor = json.loads(Path("v3_spec_immutable_anchor.json").read_text(encoding="utf-8"))
anchor.update({
    "amendment_no": 14,
    "spec_sha256": res["spec_sha256"],
    "spec": spec,
    "code_sha256": code_hashes,
})
Path("v3_spec_immutable_anchor.json").write_text(json.dumps(anchor, indent=2), encoding="utf-8")
print("\n[SUCCESS] Amendment #14 registered and anchor updated.")
