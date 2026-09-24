import json
import sqlite3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(".").resolve()))

from econometric_audit import record_amendment, verify_registration, canonical_json, sha256_text, file_sha256
import run_mts_h1

conn = sqlite3.connect("psx.db")
conn.execute("PRAGMA journal_mode=WAL")

spec = run_mts_h1.SPEC
code_files = run_mts_h1.CODE_FILES

diff_text = """
- psx_data_v2.py:
  * Added session_status(con, rows, today) to detect weekend/holiday ghost pages (>90% match with prior session). Logs HOLIDAY in ingest_runs and skips insertion.
  * Hardened base_symbol(sym, known) to only strip EX_SUFFIXES if candidate exists in known PSX universe; preserves genuine symbols (e.g. BLUEX, DCR) and separate rights vouchers (STLR, SGPLR).
  * In fetch_and_store_mts: raises FeedError on report date parse failure (no fallback to today).
  * Removed duplicate unhardened mts_quotes parser; delegates ingestion entirely to hardened mts_engine.ingest_report (single source of truth).
  * Removed unsafe live WAL zip backup create_automated_backup(); disaster recovery managed by backup.py.
  * Added exit code 2 on critical feed failure in __main__.
- run_mts_h1.py:
  * Eliminated mechanical negative cash bias in sector-neutral spread by restricting U_s sleeve to dates when Q5_s has active positions (us = {d: us_all[d] for d in q5s if d in us_all}).
  * Enforced publication lag in shadow mode (dry run) and logged surviving cohort counts.
  * Changed warm-up period exit to code 0 (return cleanly) to eliminate false daily monitoring alerts.
  * Restricted missing_cohort_pct window strictly to evaluated sessions (R.index).
  * Locked eligible universe in SPEC to PSX_LIQUID_PROXY_139 (no mid-run branching).
- daily_job.py:
  * Added check for hypothesis_ledger EVALUATED status; stands down cleanly with exit 0 upon 240-session completion.
- jegadeesh_titman_portfolio.py:
  * Added strict uniqueness assertion on (date, symbol) in load_market_panel.
"""

reason = (
    "V3.8 Pre-Data Institutional Hardening per Opus 5.5 Round 12: "
    "Guarded psx_data_v2 against weekend/holiday ghost sessions via session_status, "
    "safe base_symbol suffix stripping, removed duplicate mts_quotes parser, raised FeedError on MTS parse failures, "
    "fixed sector-neutral spread cash bias, enforced publication lag in shadow mode, fixed warm-up exit code to 0, "
    "bounded missing_cohort_pct to R.index, locked universe to PSX_LIQUID_PROXY_139, and enabled daily_job stand-down on EVALUATED."
)

res = record_amendment(
    conn=conn,
    hypothesis_id=spec["hypothesis_id"],
    amendment_no=8,
    pre_data=True,
    reason=reason,
    diff_text=diff_text.strip(),
    spec=spec,
    code_paths=code_files
)

print(f"Recorded Amendment #8:")
print(f"  Spec SHA256: {res['spec_sha256']}")
print(f"  Diff SHA256: {res['diff_sha256']}")

# Verify registration
chk = verify_registration(conn, spec["hypothesis_id"], spec, code_files)
print(f"Verify registration check: {chk}")
assert chk["all_ok"], f"Verification failed: {chk}"

# Update v3_spec_immutable_anchor.json
# Corporate actions and mts_eligible hashes
ca_df = conn.execute("SELECT base_symbol, ex_date, action_type, ratio, amount FROM corporate_actions ORDER BY base_symbol, ex_date").fetchall()
ca_str = canonical_json(ca_df)
ca_hash = sha256_text(ca_str)

elig_df = conn.execute("SELECT symbol FROM mts_eligible ORDER BY symbol").fetchall()
elig_str = canonical_json(elig_df)
elig_hash = sha256_text(elig_str)

code_hashes = {Path(p).name: file_sha256(p) for p in code_files}

anchor = {
    "hypothesis_id": spec["hypothesis_id"],
    "amendment_no": 8,
    "status": "REGISTERED_PRE_DATA_FROZEN",
    "spec_sha256": res["spec_sha256"],
    "spec": spec,
    "code_sha256": code_hashes,
    "data_sha256": {
        "corporate_actions": ca_hash,
        "mts_eligible": elig_hash
    }
}

Path("v3_spec_immutable_anchor.json").write_text(json.dumps(anchor, indent=2), encoding="utf-8")
print("\n[SUCCESS] Amendment #8 registered and v3_spec_immutable_anchor.json updated and verified!")
