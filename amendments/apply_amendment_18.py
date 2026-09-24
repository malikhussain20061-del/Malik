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
VERIFICATION FIRST - the suspected look-ahead is not present:
- mts_snapshots.report_date is parsed from the report COVER date ("September 14, 2026" on page 0),
  not from the per-row "Report Date" column. Verified on the golden 2026-09-14 report:
  cover = 2026-09-14, all 67 rows dated 11-Sep-26, parsed report_date = 2026-09-14.
- build_cohorts sets formation_date = report_date, and enforce_publication_lag counts
  entry_lag_sessions from formation_date. So entry is counted from the availability date.
- The per-row as-of date was not parsed at all before this amendment, so it could not have been
  used as a key - but equally, staleness was completely unmeasured.

WHAT CHANGED (observability, no behavioural change):
- mts_engine.parse_mts_pdf: captures the per-row as-of date as diag['data_as_of'] (modal value,
  warns if a single report mixes dates).
- mts_snapshots gains a nullable data_as_of column; init_schema ALTERs existing databases.
- mts_engine.ingest_report logs a PUBLICATION_LAG anomaly per report date with the as-of date,
  the cover date, the staleness in TRADING sessions, and the capture date.
  First measurement on the golden report: as of 2026-09-11, published 2026-09-14 = 1 session
  stale, because 12-13 September was a weekend. The "3 day" lag is 3 calendar days, 1 session.
- test_mts_parser.py gained TEST 8, which fails if report_date ever becomes the as-of date or if
  the as-of date stops being captured. 8/8 pass.
- SPEC timing block now states the formation key explicitly and records the measured staleness.
- Fixed a counting bug found while testing this: the lag query used COUNT(*) over daily_quotes,
  which counts quote rows not sessions and reported 644 sessions stale; it is COUNT(DISTINCT
  trade_date).

NO data, hash, universe or signal change. corporate_actions, mts_eligible and sector_map hashes
are byte-identical to Amendment #17.
"""

reason = (
    "V3.18 Pre-Data Staleness Observability: the concern that entry might be counted from the "
    "position as-of date rather than the publication date was checked and found NOT to hold - "
    "report_date is the cover date and the as-of date was never parsed. But because it was never "
    "parsed, staleness was unmeasurable, and a source change (the pending NCCPL export) could "
    "introduce exactly that drift with no alarm. This records the as-of date, logs the lag every "
    "report, and pins the correct behaviour with a failing-if-broken test. Measured staleness on "
    "the only report we hold is 1 trading session, not 3 days. No behaviour, data or hash changed."
)

res = record_amendment(
    conn=conn, hypothesis_id=spec["hypothesis_id"], amendment_no=18, pre_data=True,
    reason=reason, diff_text=diff_text.strip(), spec=spec, code_paths=code_files
)
print("Recorded Amendment #18:")
print(f"  Spec SHA256: {res['spec_sha256']}")
print(f"  Diff SHA256: {res['diff_sha256']}")

chk = verify_registration(conn, spec["hypothesis_id"], spec, code_files)
print(f"Verify registration: {chk}")
assert chk["all_ok"] and chk["amendment_no"] == 18, f"Verification failed: {chk}"

anchor = json.loads(Path("v3_spec_immutable_anchor.json").read_text(encoding="utf-8"))
anchor.update({
    "amendment_no": 18,
    "spec_sha256": res["spec_sha256"],
    "spec": spec,
    "code_sha256": {Path(p).name: file_sha256(p) for p in code_files},
})
Path("v3_spec_immutable_anchor.json").write_text(json.dumps(anchor, indent=2), encoding="utf-8")
print("\n[SUCCESS] Amendment #18 registered and anchor updated.")
