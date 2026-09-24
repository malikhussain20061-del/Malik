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
- daily_job.py (run_step):
  * The raised failure carried only the subprocess STDERR. The feed errors are printed to
    STDOUT by psx_data_v2, so the HTTP status that explains the failure never reached the
    alert or the status file. Both streams are now included (truncated to 4000 chars).
- daily_job.py (classify_capture_failure, new):
  * Splits the old single COMPLETED_WITH_CAPTURE_FAILURE into two statuses, because the two
    causes need opposite responses:
      FEED_BLOCKED        - 403/404/Forbidden/Not Found/unreachable/timeout. The exchange is
                            refusing us. Do NOT retry; settle access with PSX and backfill the
                            day from a manually downloaded closing-rate file.
      FEED_PARSE_FAILURE  - the feed answered but nothing ingested. A parser or schema problem.
  * Validated against the real 2026-09-24 18:30 failure text (FEED_BLOCKED) and against
    synthetic parser cases (FEED_PARSE_FAILURE).
- Behaviour NOT changed, because it was already correct and is recorded here so nobody
  "fixes" it later: there is no retry-on-failure loop anywhere in the ingest path. The two
  while loops in psx_data_v2.py (lines 397 and 522) walk back over calendar days looking for a
  NON-EMPTY FIPI/LIPI payload; an HTTP 403/404 raises and is caught by the except branch, so a
  blocked feed is attempted exactly once per run. Confirmed by reading both call sites.
- No SPEC change, no data change. spec_sha256 is byte-identical to Amendment #18; only the
  daily_job.py entry in code_sha256 moves. corporate_actions (2b40ec46), mts_eligible
  (5c44519b) and sector_map (216bb418) hashes are untouched.
"""

reason = (
    "V3.19 Pre-Data Operational Fix: a blocked exchange feed and a broken parser were reported "
    "under one status name, and the HTTP status that distinguishes them was thrown away because "
    "run_step only carried stderr while psx_data_v2 prints feed errors to stdout. On 2026-09-24 "
    "18:30 market-watch returned 404 and the status file said only CAPTURE_FAILURE, which reads "
    "like a code bug when it is an access question. Now FEED_BLOCKED and FEED_PARSE_FAILURE are "
    "distinct, and FEED_BLOCKED's alert text says plainly not to retry. The retry-loop concern "
    "was checked first and found not to exist; that finding is recorded so the absence is not "
    "later mistaken for an omission. Nothing in the registered spec, signal, universe or data "
    "changed."
)

res = record_amendment(
    conn=conn, hypothesis_id=spec["hypothesis_id"], amendment_no=19, pre_data=True,
    reason=reason, diff_text=diff_text.strip(), spec=spec, code_paths=code_files
)
print("Recorded Amendment #19:")
print(f"  Spec SHA256: {res['spec_sha256']}")
print(f"  Diff SHA256: {res['diff_sha256']}")

chk = verify_registration(conn, spec["hypothesis_id"], spec, code_files)
print(f"Verify registration: {chk}")
assert chk["all_ok"] and chk["amendment_no"] == 19, f"Verification failed: {chk}"
assert res["spec_sha256"] == "a5dad83d3e13aa52e7b59c8e6e66bbb920f070f0de17c9d5cacfcc351ac3f9c1", \
    "SPEC must not have moved in this amendment"

anchor = json.loads(Path("v3_spec_immutable_anchor.json").read_text(encoding="utf-8"))
anchor.update({
    "amendment_no": 19,
    "spec_sha256": res["spec_sha256"],
    "spec": spec,
    "code_sha256": {Path(p).name: file_sha256(p) for p in code_files},
})
Path("v3_spec_immutable_anchor.json").write_text(json.dumps(anchor, indent=2), encoding="utf-8")
print("\n[SUCCESS] Amendment #19 registered; SPEC hash confirmed unchanged.")
