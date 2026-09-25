"""DRAFT — Amendment #20: pre-data rule for when a hand-downloaded session counts as CLEAN.

NOT RUN. Nothing here touches psx.db or the ledger until the user orders registration, and the
order given on 2026-09-25 was to register it together with the 30-Sep POSTPONED entry.

Why this file exists today rather than on 30 Sep: the rule decides whether 22/24/25-Sep sessions
count toward the 5-clean-sessions start condition. If it is written after those sessions are
already in the database, the check has been fitted to the result - which is the one thing the
pre-registration exists to prevent. The user's instruction was "abhi likh kar lock karna hai,
baad mein nahi".

Why it moves spec_sha256: `evaluation_start_condition` gains an explicit definition of the word
"clean", so this is a SPEC change, not just a note. That is the opposite of the 30-Sep POSTPONED
record, which must NOT change any hash. They stay two separate ledger entries even if registered
the same day.
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(".").resolve()))

from econometric_audit import record_amendment, verify_registration
import run_mts_h1

RULE = {
    "manual_download_clean_rule": {
        "text": (
            "A trading session counts as CLEAN toward evaluation_start_condition only if ALL "
            "four hold: (1) final quotes rows exist for that trade_date; (2) if those rows were "
            "supplied by hand rather than captured, each row carries quality_flags beginning "
            "'MANUAL_DOWNLOAD:sha256=' with the SHA-256 of the downloaded file, and no row on "
            "that date carries a ':SCRATCH' marker; (3) the close-vs-ldcp band check passed for "
            "that date and the pass is recorded in quality_flags as ':LDCP_OK'; (4) an MTS "
            "snapshot exists whose report_date is new relative to the previously registered "
            "report_date. A repeated report_date does not make a session clean. Any condition "
            "unmet means the counter does not advance and does not reset - it only advances on "
            "consecutive qualifying sessions."
        ),
        "applies_to": "evaluation_start_condition only; no signal, universe, band or threshold changes",
        "recorded_in": ["quality_flags per row", "mts_snapshots.report_date", "raw_archive gz + sha256"],
    }
}

REASON = (
    "V3.20 Pre-Data Definition: PSX market-watch has been blocked since 2026-09-24, so quote "
    "sessions are being supplied from files downloaded by hand by the researcher. The locked "
    "start condition counts '5 consecutive clean sessions' without defining whether a "
    "hand-supplied session is clean. Deciding that after the sessions exist would be fitting the "
    "rule to the result. This amendment fixes the definition in advance and makes each part of "
    "it checkable from the database alone: the source hash is on the row, the band-check verdict "
    "is on the row, and MTS freshness is the report_date. It tightens the check; it loosens "
    "nothing. No threshold, no universe, no signal, no holding period, no evaluation window "
    "changes, and the start date is still not a calendar date."
)

DIFF = """
- run_mts_h1.py SPEC:
  * evaluation_start_condition: wording now points at manual_download_clean_rule for the word
    "clean" (the 5-consecutive-sessions requirement itself is unchanged).
  * NEW key manual_download_clean_rule with the four conditions above.
- data_integrity_hashes: unchanged. corporate_actions 2b40ec46, mts_eligible 5c44519b and
  sector_map 216bb418 are untouched by this amendment.
- Code outside SPEC:
  * backfill_manual.py (not a CODE_FILE): quality_flags now records the band-check verdict
    (':LDCP_OK' when the close-vs-ldcp test passes) so condition (3) is auditable later instead
    of being something a human remembers.
  * news_job.py (not a CODE_FILE): the daily digest gains the clean-session counter (x/5) so the
    number that will decide the start is visible during the shadow run instead of being computed
    at the gate.
- This file deliberately does not run any capture, and registers nothing by itself.
"""


def main():
    conn = sqlite3.connect("psx.db")
    conn.execute("PRAGMA journal_mode=WAL")
    spec = run_mts_h1.SPEC

    print("DRAFT STATE - nothing has been written. Intended change:")
    print(f"  hypothesis_id            : {spec['hypothesis_id']}")
    print(f"  current spec_sha256      : (unchanged until registered)")
    print(f"  evaluation_start_condition:\n    {spec.get('evaluation_start_condition')}")
    print(f"\n  rule text ({len(RULE['manual_download_clean_rule']['text'])} chars):")
    for line in RULE["manual_download_clean_rule"]["text"].split(". "):
        print(f"    - {line.strip()}.")

    if "--register" not in sys.argv:
        print("\n[REFUSED] Dry run only. Register with:  python amendments/apply_amendment_20.py --register")
        print("          That moves spec_sha256, so it needs the user's word in that round.")
        return

    res = record_amendment(
        conn=conn, hypothesis_id=spec["hypothesis_id"], amendment_no=20, pre_data=True,
        reason=REASON, diff_text=DIFF.strip(), spec=spec, code_paths=run_mts_h1.CODE_FILES,
    )
    print(f"Recorded Amendment #20: spec={res['spec_sha256']} diff={res['diff_sha256']}")
    chk = verify_registration(conn, spec["hypothesis_id"], spec, run_mts_h1.CODE_FILES)
    print(f"Verify registration: {chk}")
    assert chk["all_ok"] and chk["amendment_no"] == 20, f"Verification failed: {chk}"


if __name__ == "__main__":
    main()
