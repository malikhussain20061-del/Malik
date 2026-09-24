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
- ca_migration.py (rebuild_corporate_actions):
  * The table-copy SELECT passed a literal NULL for voids_action_id instead of the stored value,
    so any rebuild erased the VOID linkage of every row it carried.
  * Now selects voids_action_id verbatim.
  * Discovered by the invariant assertion added in Amendment #14: after a rebuild the active event
    count read 396 instead of 393, because the 3 phantom-base 'HU' cash dividends VOIDed under
    Amendment #13 were resurrected alongside their 3 'HUBC' replacements - i.e. HUBC's Rs 5.00
    dividend was double-counted on 2025-11-10, 2026-03-06 and 2026-05-05.
  * The rebuilt table also contained VOID rows with a NULL voids_action_id, a state the
    ca_no_replace trigger itself refuses to insert, which is what made this provably a defect
    rather than an intended representation.
  * Impact was latent, not live: the rebuild only runs from this migration path and from
    test_corporate_actions_void.py, and psx.db was never rebuilt after Amendment #13. Any future
    re-run would have silently corrupted total-return adjustment.
  * test_corporate_actions_void.py now passes end to end, including the active-count == 393 check.
"""

reason = (
    "V3.15 Pre-Data Bug Fix in the append-only migration path: rebuild_corporate_actions dropped "
    "voids_action_id, which would have un-voided the Amendment #13 corrections and double-counted "
    "three HUBC dividends the next time the table is rebuilt. Found by the invariant added in "
    "Amendment #14, not by an assertion failing on data that mattered today. SPEC, data hashes and "
    "evaluation_start are unchanged; only ca_migration.py differs from Amendment #14."
)

res = record_amendment(
    conn=conn, hypothesis_id=spec["hypothesis_id"], amendment_no=15, pre_data=True,
    reason=reason, diff_text=diff_text.strip(), spec=spec, code_paths=code_files
)
print("Recorded Amendment #15:")
print(f"  Spec SHA256: {res['spec_sha256']}  (must equal #13/#14: 72fb5c9d...)")
print(f"  Diff SHA256: {res['diff_sha256']}")

chk = verify_registration(conn, spec["hypothesis_id"], spec, code_files)
print(f"Verify registration: {chk}")
assert chk["all_ok"] and chk["amendment_no"] == 15, f"Verification failed: {chk}"

code_hashes = {Path(p).name: file_sha256(p) for p in code_files}
anchor = json.loads(Path("v3_spec_immutable_anchor.json").read_text(encoding="utf-8"))
anchor.update({"amendment_no": 15, "spec_sha256": res["spec_sha256"],
               "spec": spec, "code_sha256": code_hashes})
Path("v3_spec_immutable_anchor.json").write_text(json.dumps(anchor, indent=2), encoding="utf-8")
print("\n[SUCCESS] Amendment #15 registered and anchor updated.")
