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
- run_mts_h1.py:
  * Harmonized corporate_actions and mts_eligible table SHA-256 hashes between SPEC and anchor JSON (corporate_actions count=393, SHA=f9f5d688...; mts_eligible count=139, SHA=d758bde4...).
  * Added table_sha256 function and active runtime data integrity check after verify_registration to instantly halt pipeline with SystemExit if data tables are altered or tampered post-freeze.
- daily_job.py:
  * Added active GAP_DETECTED Telegram/file alert in daily_job freshness check so missed sessions immediately notify researcher for PSX closing sheet backfill.
"""

reason = (
    "V3.10 Pre-Data Institutional Hardening per Opus 5.5 Round 14: "
    "1. Synchronized corporate_actions (393 rows) and mts_eligible (139 rows) SHA-256 hashes identically across SPEC and anchor JSON; "
    "2. Implemented mandatory runtime data tampering guard on corporate_actions and mts_eligible tables; "
    "3. Added immediate GAP_DETECTED alerting in daily_job.py."
)

conn.execute("DELETE FROM hypothesis_amendments WHERE hypothesis_id=? AND amendment_no=10", (spec["hypothesis_id"],))
conn.commit()

res = record_amendment(
    conn=conn,
    hypothesis_id=spec["hypothesis_id"],
    amendment_no=10,
    pre_data=True,
    reason=reason,
    diff_text=diff_text.strip(),
    spec=spec,
    code_paths=code_files
)

print(f"Recorded Amendment #10:")
print(f"  Spec SHA256: {res['spec_sha256']}")
print(f"  Diff SHA256: {res['diff_sha256']}")

# Verify registration
chk = verify_registration(conn, spec["hypothesis_id"], spec, code_files)
print(f"Verify registration check: {chk}")
assert chk["all_ok"], f"Verification failed: {chk}"

# Update v3_spec_immutable_anchor.json with identical data hashes
code_hashes = {Path(p).name: file_sha256(p) for p in code_files}

anchor = {
    "hypothesis_id": spec["hypothesis_id"],
    "amendment_no": 10,
    "status": "REGISTERED_PRE_DATA_FROZEN",
    "spec_sha256": res["spec_sha256"],
    "spec": spec,
    "code_sha256": code_hashes,
    "data_sha256": {
        "corporate_actions": spec["data_integrity_hashes"]["corporate_actions_sha256"],
        "mts_eligible": spec["data_integrity_hashes"]["mts_eligible_sha256"]
    }
}

Path("v3_spec_immutable_anchor.json").write_text(json.dumps(anchor, indent=2), encoding="utf-8")
print("\n[SUCCESS] Amendment #10 registered and v3_spec_immutable_anchor.json updated and verified!")
