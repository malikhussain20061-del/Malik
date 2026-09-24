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
- mts_engine.py (parse_mts_pdf coordinate-based cell reader):
  * Numeric cells are now read by word geometry, not by regex-trimming the table cell string.
    _page_words() de-duplicates the ~21x overprinted glyph runs this report stamps per page
    (13,105 word instances vs 604 unique on page 2).
  * _contained_num() accepts only words lying ENTIRELY inside the cell rect. Footnote glyphs
    sit on the cell border and spill outside it, so 'a 13.05' -> 13.05 and '12.80\\nM' -> 12.80.
    Tokens are never trimmed, because chopping characters risks deleting real digits.
  * Contained words are re-joined in x order before parsing, because this report renders one
    amount across several spans ("36,961,696.9" + "2").
  * Two different numbers in one cell now raise ParseIntegrityError instead of guessing.
  * _assert_layout() verifies the column titles at every parsed position and raises
    LayoutDriftError if upstream reorders them, so positional indexing can never mislabel.
  * weighted_rate [5.0, 35.0] band UNCHANGED and still enforced on every observed value.
    Only a printed '-' (no same-day financing) is treated as missing, and is counted in
    diag['n_rate_missing'] and logged as RATE_MISSING_COUNT. weighted_rate is diagnostic only
    (ALLOWED_SIGNAL_COLS = open_pct, mts_volume, mts_amount), never a signal input.
  * Rows the parser cannot read are logged per symbol as PARSE_ROW_REJECTED; nothing vanishes.
- run_mts_h1.py (SPEC):
  * freeze_date narrowed to its real meaning: the data partition boundary whose corporate_actions
    SHA-256 (9bd574f0..., 393 rows) is ledger-locked. Value unchanged, so the hash invariant holds.
  * Added evaluation_start = null and evaluation_start_condition: the 240-session clock now starts
    only after a verified NCCPL primary source delivers 5 consecutive sessions with sequentially
    advancing internal report dates and zero parse rejections. No calendar date pre-committed.
  * Recorded shadow_window_1_outcome and feed_frequency_decision_outcome = POSTPONED.
  * Text correction: historical corporate action count 383 -> 393.
- daily_job.py:
  * Mode switch now reads evaluation_start instead of freeze_date. While evaluation_start is null the
    pipeline stays in shadow, so 2026-10-01 can no longer auto-promote to live evaluation.
  * Fallback on spec import failure changed from a hard-coded date to None (fail toward shadow).
- test_mts_parser.py (new permanent suite, golden file = archived 2026-09-14 report):
  * Asserts exactly 66 parsed rows, grand-total reconciliation to 17,783,397,456.31, and 9 previously
    destroyed rates recovered (FATIMA 12.80, FCCL 13.05, FFC 12.96, FFL 14.75, GCIL 15.02, HBL 13.13,
    PPL 12.74, PSO 12.90, SNBL 15.00).
  * Asserts boundary-glyph exclusion, fragmented-number rejoin, ambiguous-cell abort, band enforcement,
    missing-rate counting, and layout-drift abort. 7/7 pass.
- Finding recorded, history NOT rewritten:
  * The legacy 2026-09-14 snapshot holds 67 rows, of which SLGL is entirely NULL except open_pct=0.0
    (its volume cell prints '-'), and 12 rows carry NULL weighted_rate that the old reader destroyed
    via column bleed. The corrected parser yields 66 fully readable rows and reconciles to the printed
    grand total, proving SLGL contributed nothing. The archived PIT snapshot is left untouched.
"""

reason = (
    "V3.12 Pre-Data Postponement + MTS Parser Integrity Fix: "
    "1. feed_frequency_decision_rule's stagnation condition was met on shadow Day 1 (2026-09-24): the only "
    "archived MTS report is internal-dated 2026-09-14 and the configured source still serves identical bytes; "
    "2. Root cause of the dead feed identified and fixed - the positional reader corrupted 17 of 67 rate cells "
    "via footnote-glyph bleed, and the fail-closed [5,35] band aborted the whole file on the first one, so MTS "
    "ingest could never succeed; "
    "3. Replaced with coordinate-based cell reading plus explicit layout-drift detection; the [5,35] band was NOT "
    "loosened, only NaN-vs-unparseable was distinguished, which is a validator correctness fix not a tolerance change; "
    "4. October 1 automatic promotion to live evaluation removed: evaluation_start is now a pre-registered "
    "condition, not a date, per the zero-cost postponement branch of the decision rule; "
    "5. Weekly-sleeve alternative deliberately NOT taken - it would dilute the carrying-cost mechanism under test; "
    "if no daily primary source exists, that is a new hypothesis (V4), not an amendment to this one."
)

res = record_amendment(
    conn=conn,
    hypothesis_id=spec["hypothesis_id"],
    amendment_no=12,
    pre_data=True,
    reason=reason,
    diff_text=diff_text.strip(),
    spec=spec,
    code_paths=code_files
)

print("Recorded Amendment #12:")
print(f"  Spec SHA256: {res['spec_sha256']}")
print(f"  Diff SHA256: {res['diff_sha256']}")

chk = verify_registration(conn, spec["hypothesis_id"], spec, code_files)
print(f"Verify registration check: {chk}")
assert chk["all_ok"], f"Verification failed: {chk}"
assert chk["amendment_no"] == 12

code_hashes = {Path(p).name: file_sha256(p) for p in code_files}

anchor = {
    "hypothesis_id": spec["hypothesis_id"],
    "amendment_no": 12,
    "status": "REGISTERED_PRE_DATA_POSTPONED",
    "spec_sha256": res["spec_sha256"],
    "spec": spec,
    "code_sha256": code_hashes,
    "data_sha256": {
        "corporate_actions": spec["data_integrity_hashes"]["corporate_actions_sha256"],
        "mts_eligible": spec["data_integrity_hashes"]["mts_eligible_sha256"]
    }
}

Path("v3_spec_immutable_anchor.json").write_text(json.dumps(anchor, indent=2), encoding="utf-8")
print("\n[SUCCESS] Amendment #12 registered; v3_spec_immutable_anchor.json updated and verified!")
