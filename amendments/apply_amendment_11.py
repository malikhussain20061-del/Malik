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
- corporate_actions Table & Triggers (ca_migration.py):
  * Rebuilt corporate_actions with voids_action_id column (INTEGER REFERENCES corporate_actions(action_id)).
  * Shifted active uniqueness enforcement to ca_no_replace trigger so that voided erroneous actions can be cleanly replaced with corrected events without table constraint violation.
  * Installed 3 comprehensive append-only triggers: ca_no_update, ca_no_delete, ca_no_replace.
  * ca_no_replace blocks explicit action_id replace, duplicate active events on same date, invalid VOIDs (missing target, chained VOID, double-VOID, or mismatched date/symbol).
- jegadeesh_titman_portfolio.py:
  * Implemented double-exclusion filter in load_market_panel and verify_corporate_actions_completeness:
    voided = set(df.loc[df.action_type == 'VOID', 'voids_action_id'].dropna().astype(int))
    active = df[(df.action_type != 'VOID') & (~df.action_id.isin(voided))]
- run_mts_h1.py:
  * Added mandatory runtime trigger integrity check in main() raising SystemExit if ca_no_update, ca_no_delete, or ca_no_replace are missing or altered.
  * Updated corporate_actions historical partition hash to 11-column canonical SHA-256 (9bd574f0... for 393 rows).
  * Added ca_migration.py and add_corporate_action.py to CODE_FILES.
- add_corporate_action.py:
  * CLI tool for researchers enforcing plain INSERT, yield sanity check, ratio check, and trigger abort handling.
- test_corporate_actions_void.py:
  * Permanent test suite covering XDXB, double-exclusion, rebuild hash invariance, and trigger rejections.
"""

reason = (
    "V3.11 Pre-Data Institutional Hardening per Opus 5.5 Round 16: "
    "1. Atomic table rebuild with voids_action_id column and trigger-enforced active uniqueness; "
    "2. Three append-only triggers (ca_no_update, ca_no_delete, ca_no_replace) with hard SystemExit guard in run_mts_h1.py; "
    "3. VOID compensation protocol with double-exclusion filtering in jegadeesh_titman_portfolio.py; "
    "4. Single source of truth migration script (ca_migration.py) and researcher forward entry tool (add_corporate_action.py); "
    "5. Canonical 11-column hash update (9bd574f0...) in SPEC and anchor JSON."
)

res = record_amendment(
    conn=conn,
    hypothesis_id=spec["hypothesis_id"],
    amendment_no=11,
    pre_data=True,
    reason=reason,
    diff_text=diff_text.strip(),
    spec=spec,
    code_paths=code_files
)

print("Recorded Amendment #11:")
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
    "amendment_no": 11,
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
print("\n[SUCCESS] Amendment #11 registered and v3_spec_immutable_anchor.json updated and verified!")
