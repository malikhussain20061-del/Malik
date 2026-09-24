import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(".").resolve()))

from econometric_audit import record_amendment, verify_registration, file_sha256
import run_mts_h1

conn = sqlite3.connect("psx.db")
conn.execute("PRAGMA journal_mode=WAL")

spec = run_mts_h1.SPEC
code_files = run_mts_h1.CODE_FILES
HIST = spec["data_integrity_hashes"]["corporate_actions_sha256_superseded"]

diff_text = """
- amendments/repair_base_symbol.py (new, tracked):
  * derive_base() strips a legal PSX ex-suffix (XDXB/XDXR/XBXR/XD/XB/XR) only when the
    remainder is itself a ticker the exchange reported. The previous function returned the
    symbol unchanged whenever it appeared in `known`, which stopped real ex-tickers collapsing,
    while the value stored in daily_quotes came from an older stripper that cut trailing
    letters of genuine tickers and invented bases that do not exist on PSX.
  * known = DISTINCT symbol FROM daily_quotes, an exchange-reported proxy for the listed-company
    directory, cross-checked against dps.psx.com.pk market-watch on 2026-09-24. Documented as a
    PROXY: a genuinely listed company that never traded inside our 238-day window is absent from it.
- daily_quotes: 786 rows across 42 symbols re-derived. Phantom bases reduced from 8 to 0
  (HU, PA, AMT, BLU, GR, SR, JSR, BAFL-JU all removed; HUBC, PABC, AMTEX, BLUEX, GRR, SRR, JSRR,
  BAFL-JUNC now map to themselves). LUCKXD->LUCK, SYSXB->SYS, AHLXD->AHL verified still collapsing.
  The ...NC family (WTLNC, ASCNC, HIRATNC, ...) now keeps its own identity instead of merging into
  the parent ticker.
- corporate_actions (append-only respected, no trigger dropped):
  * 3 HUBC cash dividends (Rs 5.00 on 2025-11-10, 2026-03-06, 2026-05-05) were filed under the
    phantom base 'HU'. Each was VOIDed with a trigger-valid VOID (matching ex_date and base_symbol)
    and re-appended under 'HUBC'. Active event count unchanged at 393; partition row count 393 -> 399.
  * Historical partition hash 9bd574f0... (393 rows) -> 2b40ec46... (399 rows). The superseded hash
    is retained in SPEC under corporate_actions_sha256_superseded.
  * verify_corporate_actions_completeness passes before and after the repair.
- mts_eligible: UNCHANGED, hash still d758bde4... (139). Known defect recorded, not repaired:
  the universe contains 'HU', which is not a listed ticker and has no quotes, alongside the real
  'HUBC'. It can never form a cohort so the signal is unaffected, but the universe is effectively
  138 names. Removing it changes a ledger-locked hash and needs its own amendment.
- MTS ingest verified end to end on a scratch copy after the repair: the archived 2026-09-14 report
  ingests 66 rows, HUBC is stored with mts_volume 3,255,486 / open_pct 3.17, the amount total
  reconciles to 17,783,397,456.30, and the two expected anomalies are logged
  (PARSE_ROW_REJECTED for SLGL, RATE_MISSING_COUNT = 4).
"""

reason = (
    "V3.13 Pre-Data base_symbol Repair: the stored daily_quotes.base_symbol column was produced by an "
    "obsolete suffix stripper that mistook trailing letters of real tickers for ex-dividend markers, filing "
    "8 genuine PSX symbols under phantom bases that the exchange does not list. Because mts_eligible and the "
    "MTS report use the real tickers, those names dropped out of the cross-sectional panel and the MTS ingest "
    "aborted outright on 'HUBC' - so this was the remaining blocker behind the dead feed, not a cosmetic cleanup. "
    "Quotes and corporate actions were repaired together on purpose: fixing only quotes would have left HUBC's "
    "3 cash dividends under 'HU' and silently produced unadjusted total returns, which is worse than the status quo. "
    "The corporate_actions partition hash is re-anchored from 9bd574f0 (393 rows) to 2b40ec46 (399 rows) with the "
    "superseded hash retained, using the Amendment #11 VOID protocol; no trigger was dropped or altered."
)

res = record_amendment(
    conn=conn,
    hypothesis_id=spec["hypothesis_id"],
    amendment_no=13,
    pre_data=True,
    reason=reason,
    diff_text=diff_text.strip(),
    spec=spec,
    code_paths=code_files
)

print("Recorded Amendment #13:")
print(f"  Spec SHA256: {res['spec_sha256']}")
print(f"  Diff SHA256: {res['diff_sha256']}")
print(f"  superseded CA hash: {HIST['hash'][:16]}... ({HIST['count']} rows)")

chk = verify_registration(conn, spec["hypothesis_id"], spec, code_files)
print(f"Verify registration: {chk}")
assert chk["all_ok"], f"Verification failed: {chk}"
assert chk["amendment_no"] == 13

# --- Test #1 data-provenance note: recorded only, result NOT re-run ---
T1 = "BREAKOUT_20D_VOLUME_CONFIRMED_H5"
row = conn.execute("SELECT spec_sha256, code_sha256_json, status FROM hypothesis_ledger "
                   "WHERE hypothesis_id=?", (T1,)).fetchone()
t1_note = (
    "Data-provenance note recorded 2026-09-24 under Amendment #13 of MTS_CROWDING_XS_H10_V3. "
    "The quotes panel this result was computed on contained 786 rows across 42 symbols whose "
    "base_symbol was mis-derived by an obsolete suffix stripper, including 8 real PSX tickers filed "
    "under phantom bases (HUBC->HU, PABC->PA, AMTEX->AMT, BLUEX->BLU, GRR->GR, SRR->SR, JSRR->JSR, "
    "BAFL-JUNC->BAFL-JU). Corporate actions for those names were therefore joined on the phantom key. "
    "The stored result (2530 trades, FAILED_TO_REJECT_NULL_SYMMETRIC, empirical p 0.676) is left exactly "
    "as recorded and was NOT re-run: the conclusion is a failure to reject the null, so a corrected panel "
    "could only change the magnitude of a result that was already null, and re-running it post-hoc against "
    "repaired data would be a peek. If this signal is ever revisited it must be re-registered as a new test "
    "number against the repaired panel."
)
cur = conn.execute(
    "INSERT INTO hypothesis_amendments(hypothesis_id, amendment_no, amended_at_utc, pre_data, "
    "reason, diff_sha256, diff_text, spec_sha256, code_sha256_json) VALUES (?,?,?,?,?,?,?,?,?)",
    (T1, 1, datetime.now(timezone.utc).isoformat(),
     0, t1_note, res["diff_sha256"], t1_note, row[0], row[1]))
conn.commit()
print(f"[+] Ledger note written for {T1} (amendment_no=1, status left as {row[2]})")

code_hashes = {Path(p).name: file_sha256(p) for p in code_files}
anchor = {
    "hypothesis_id": spec["hypothesis_id"],
    "amendment_no": 13,
    "status": "REGISTERED_PRE_DATA_POSTPONED",
    "spec_sha256": res["spec_sha256"],
    "spec": spec,
    "code_sha256": code_hashes,
    "data_sha256": {
        "corporate_actions": spec["data_integrity_hashes"]["corporate_actions_sha256"],
        "corporate_actions_superseded": HIST["hash"],
        "mts_eligible": spec["data_integrity_hashes"]["mts_eligible_sha256"]
    }
}
Path("v3_spec_immutable_anchor.json").write_text(json.dumps(anchor, indent=2), encoding="utf-8")
print("\n[SUCCESS] Amendment #13 registered and anchor updated.")
