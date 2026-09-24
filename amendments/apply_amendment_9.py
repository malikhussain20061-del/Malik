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
  * session_status: Changed adv/n < 0.8 from fatal FeedError to returning "GAP" status. Prevents permanent self-locking if intermediate trading day was missed due to network/power loss.
  * upsert_quotes: On "GAP" status, logs GAP_DETECTED to ingest_runs and proceeds to ingest today's quotes.
  * Added procedural rule: Intermediate missed sessions must be backfilled from PSX official closing-rates file to preserve strict T+2 publication lag indexing in panel.sessions.
- daily_job.py:
  * Changed stand-down check from row[0] == "EVALUATED" to row[0].startswith("EVALUATED"), properly catching ledger suffixes (e.g. EVALUATED_FAILED_NULL, EVALUATED_CONFIRMED).
- run_mts_h1.py:
  * sector_schedule: Used sorted(x["symbol"].unique()) matching entry_schedule definition, guaranteeing zero duplicate symbol lookups.
  * Protected sector-neutral spread calculation: S_df = pd.concat(S_components, axis=1).reindex(R.index), S = S_df.fillna(0.0).sum(axis=1), preventing missing/unformed sector sleeves from silently dropping valid evaluation sessions.
  * Added sector_neutral_nan_days count to diagnostics ledger.
"""

reason = (
    "V3.9 Pre-Data Institutional Hardening per Opus 5.5 Round 13: "
    "1. Prevented session_status self-lock on missed sessions via GAP return and ingest continuation; "
    "2. Standardized daily_job stand-down check via row[0].startswith('EVALUATED'); "
    "3. Protected sector-neutral spread against NaN session dropping via pd.concat and fillna(0.0); "
    "4. Enforced unique symbols in sector_schedule."
)

res = record_amendment(
    conn=conn,
    hypothesis_id=spec["hypothesis_id"],
    amendment_no=9,
    pre_data=True,
    reason=reason,
    diff_text=diff_text.strip(),
    spec=spec,
    code_paths=code_files
)

print(f"Recorded Amendment #9:")
print(f"  Spec SHA256: {res['spec_sha256']}")
print(f"  Diff SHA256: {res['diff_sha256']}")

# Verify registration
chk = verify_registration(conn, spec["hypothesis_id"], spec, code_files)
print(f"Verify registration check: {chk}")
assert chk["all_ok"], f"Verification failed: {chk}"

# Update v3_spec_immutable_anchor.json
ca_df = conn.execute("SELECT base_symbol, ex_date, action_type, ratio, amount FROM corporate_actions ORDER BY base_symbol, ex_date").fetchall()
ca_str = canonical_json(ca_df)
ca_hash = sha256_text(ca_str)

elig_df = conn.execute("SELECT symbol FROM mts_eligible ORDER BY symbol").fetchall()
elig_str = canonical_json(elig_df)
elig_hash = sha256_text(elig_str)

code_hashes = {Path(p).name: file_sha256(p) for p in code_files}

anchor = {
    "hypothesis_id": spec["hypothesis_id"],
    "amendment_no": 9,
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
print("\n[SUCCESS] Amendment #9 registered and v3_spec_immutable_anchor.json updated and verified!")
