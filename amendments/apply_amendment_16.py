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
EL = spec["data_integrity_hashes"]["mts_eligible_sha256_superseded"]

diff_text = """
A. UNIVERSE REPAIR (the data change this amendment is named for)
- mts_eligible: deleted the single row 'HU'. 139 -> 138.
  Verification of all 139 registered names against two independent sources:
    * DISTINCT symbol FROM daily_quotes (2129 exchange-reported tickers over 238 sessions)
    * live https://dps.psx.com.pk/market-watch on 2026-09-24 (494 tickers)
  Exactly one name failed both: 'HU'. It has zero quote rows and is absent from PSX market-watch,
  while 'HUBC' - the real ticker it was derived from by the pre-#13 phantom-base defect - was already
  a separate member. No other phantom (PA, GR, SR, JSR, AMT, BLU, BAFL-JU) was present in the
  universe, and all 138 survivors appear on today's market-watch.
- New hash 5c44519bf807... (138 rows); superseded d758bde4b123... (139 rows) retained in SPEC and
  anchor with the reason recorded.
- SPEC universe text now says 138 and states plainly that it was registered as 139 until #16.
  Removing 'HU' changes no cohort: it could never form one, so this corrects the count, not the test.

B. POWER / MDE RE-DERIVATION on the repaired panel (amendments/recheck_placebo_mde.py, new)
- The original scratch/test_sector_matched_mde.py could not produce a number at all: it keyed
  q5_composition by PSX sector NAMES while daily_quotes.sector holds a mix of names and 4-digit
  codes written by two different ingest paths, so every one of the 200 draws was skipped and the
  script printed NaN medians without raising. The replacement builds a name-first sector map,
  prints each sector pool size, and raises if any draw survives.
  All pools satisfied (Banks 11>=5, E&P 4>=2, Cement 7>=2, OMC 3>=1, Power 4>=1, Fert 3>=1), 200/200 draws.
- Repaired panel vs registered figures:
    sigma (median daily placebo SD)  0.626% -> 0.730%
    simulated MDE 10d at T=240      1.400% -> 1.600%
    analytic  MDE 10d at T=240      1.226% -> 1.525%
  Every delta exceeds the 0.05% re-derivation threshold, so power_statement and
  interpretation_gate.null_framing were updated (1.40% -> 1.60% detectability claim).
  Cause: the phantom-base repair moved correctly-based tickers (HUBC, GRR, SRR, JSRR, AMTEX,
  BLUEX, PABC) out of merged phantom keys into the panel, which raises placebo dispersion.
  The 'power at a 1.0% 10-day spread is ~52.4%' clause was computed pre-repair and is explicitly
  marked superseded-and-pending rather than quietly restated.

C. APPEND-ONLY MIGRATION IDEMPOTENCY (ca_migration.py)
- rebuild_corporate_actions() now reads voids_action_id from the source table when that column
  exists and only substitutes NULL on the first migration, before the column existed.
  The unconditional NULL from Amendment #11 un-voided every corrected row on a re-run.
- test_corporate_actions_void.py gained two assertions: active events must stay 393 after a
  rebuild, and a SECOND consecutive rebuild must still report 393 active with zero orphan VOID
  rows. Both fail against the pre-fix code (observed 396).

D. NIGHTLY INTEGRITY STEP (daily_job.py)
- Added Step 4 running guard_drill.py, alerting GUARD_DRILL_FAILED and exiting 1 on failure.
  Motivation: run_mts_h1.py enforces verify_registration only after its shadow-mode branch has
  already returned, so during the whole shadow window nothing but this drill detects a drifted
  CODE_FILES hash. The drill works on an isolated copy, so the ledger is not exposed.

E. AUDIT TRAIL
- Historical apply scripts for Amendments #8, #9, #10 and #11 copied from gitignored scratch/
  into tracked amendments/, so every registered amendment now has its applying code in the repo.
"""

reason = (
    "V3.16 Pre-Data Universe Correction and Power Re-derivation: "
    "1. 'HU' removed from mts_eligible (139 -> 138) after checking all 139 names against both the "
    "238-session exchange-reported ticker set and live PSX market-watch - it was the only phantom and "
    "it had zero quotes, so the registered count was wrong but no cohort or signal changes; "
    "2. Placebo MDE re-derived on the repaired panel and the original script's silent NaN bug fixed: "
    "sigma 0.626% -> 0.730%, simulated 10d MDE 1.400% -> 1.600%, analytic 1.226% -> 1.525%. All deltas "
    "exceed the 0.05% threshold so power_statement and null_framing were updated; the un-recomputed "
    "fat-tail power clause is marked superseded rather than carried forward silently; "
    "3. rebuild_corporate_actions made idempotent over voids_action_id, with a two-consecutive-rebuild "
    "test that fails against the pre-fix code at 396 active events; "
    "4. guard_drill added as nightly Step 4 with alerting, because shadow mode returns before "
    "verify_registration and was therefore running with no code-hash check at all; "
    "5. Amendments #8-#11 apply scripts brought under version control. "
    "Everything here is pre-data: no evaluation session has been observed."
)

res = record_amendment(
    conn=conn, hypothesis_id=spec["hypothesis_id"], amendment_no=16, pre_data=True,
    reason=reason, diff_text=diff_text.strip(), spec=spec, code_paths=code_files
)
print("Recorded Amendment #16:")
print(f"  Spec SHA256: {res['spec_sha256']}")
print(f"  Diff SHA256: {res['diff_sha256']}")
print(f"  eligible: {EL['hash'][:12]}... ({EL['count']}) -> "
      f"{spec['data_integrity_hashes']['mts_eligible_sha256'][:12]}... "
      f"({spec['data_integrity_hashes']['mts_eligible_count']})")

chk = verify_registration(conn, spec["hypothesis_id"], spec, code_files)
print(f"Verify registration: {chk}")
assert chk["all_ok"] and chk["amendment_no"] == 16, f"Verification failed: {chk}"

code_hashes = {Path(p).name: file_sha256(p) for p in code_files}
anchor = json.loads(Path("v3_spec_immutable_anchor.json").read_text(encoding="utf-8"))
anchor.update({
    "amendment_no": 16,
    "status": "REGISTERED_PRE_DATA_POSTPONED",
    "spec_sha256": res["spec_sha256"],
    "spec": spec,
    "code_sha256": code_hashes,
    "data_sha256": {
        "corporate_actions": spec["data_integrity_hashes"]["corporate_actions_sha256"],
        "corporate_actions_superseded": spec["data_integrity_hashes"]["corporate_actions_sha256_superseded"]["hash"],
        "mts_eligible": spec["data_integrity_hashes"]["mts_eligible_sha256"],
        "mts_eligible_superseded": EL["hash"],
    },
})
Path("v3_spec_immutable_anchor.json").write_text(json.dumps(anchor, indent=2), encoding="utf-8")
print("\n[SUCCESS] Amendment #16 registered and anchor updated.")
