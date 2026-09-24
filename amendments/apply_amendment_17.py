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
dh = spec["data_integrity_hashes"]

diff_text = """
A. NEW LOCKED TABLE: sector_map (138 rows, sha256 216bb418...)
- amendments/build_sector_map.py (new): symbol -> PSX 4-digit sector_code, one locked
  point-in-time snapshot dated 2026-09-24, plus an unverified local sector_name label that
  grouping never reads.
- Canonical key is the code, established by evidence not assumption: the codes stored in
  daily_quotes agreed with the live dps.psx.com.pk market-watch table on 429 of 429
  comparable symbols, zero disagreements, zero multi-code symbols. The names in that column
  came from a hand-maintained local file and contradict the codes (code 0823 appears against
  both PHARMACEUTICALS and REFINERY), so names are not a second spelling of the same fact.
- Coverage enforced: 138 of 138 eligible names mapped, hard error otherwise. Two names
  (ASC, HASCOL) had an empty sector in every local row and required the DPS snapshot; the
  first DPS parser missed them because a defaulted ticker carries an extra
  <div class="tag">NC</div> before the cell boundary, which also explains the ...NC tickers
  seen in the base_symbol defect. Robust parsing yields 488 symbols.
- PIT rule recorded in SPEC: a symbol that changes sector during the 240-session run keeps
  its snapshot value, so the universe definition cannot drift while results are observed.

B. THE DEFECT THIS FIXES (run_mts_h1.py sector-neutral spread)
- The gate built its grouping with
    SELECT DISTINCT base_symbol AS symbol, sector FROM daily_quotes  ->  dict(zip(...))
  so whichever row happened to sort last won, mixing codes and names for the same sector.
  Across the eligible universe that presents 39 groups where PSX has 27 sectors: 12 real
  sectors were split in two, and the within-sector mean(Q5 - universe) that decides
  "Crowding effect" vs "Sector Exposure" was computed on that grouping.
- run_mts_h1.py now calls mts_engine.sector_map_map() and require_full_sector_coverage();
  daily_quotes.sector is no longer read anywhere in the evaluation path.
- mts_engine.py gained sector_map in SCHEMA plus sector_map_map, require_full_sector_coverage
  and sector_map_sha256, all raising SectorMapError rather than returning a partial map.

C. RUNTIME GUARD AND DRILLS
- run_mts_h1.py data-integrity step 3b compares sector_map's hash and row count against SPEC
  and aborts with SystemExit on any change, then asserts full universe coverage.
- guard_drill.py gained two negative drills: sector_map_tampered (reassign one symbol to code
  9999) and sector_map_incomplete (delete one row). Both must and do raise SystemExit.
  The suite is now 1 positive + 6 negative.

D. POWER RE-DERIVED ON THE CORRECTED GROUPING (100 draws, not 20)
- amendments/recheck_placebo_mde.py rewritten to group on sector_map, refuse any draw whose
  sector pool is short, and report the dispersion instead of a single number.
- Q5 composition re-expressed in codes: 0807 x5, 0820 x2, 0804 x2, 0821 x1, 0824 x1, 0809 x1.
  Pools behind each slot: 0807=14, 0820=4, 0804=11, 0821=5, 0824=8, 0809=5; 0820 is binding.
- sigma 0.711% (p10 0.668%, p90 0.924%), median LRV 5.536e-05, simulated 10d MDE at T=240
  1.500%, analytic 1.440%, power against a 1.0% 10-day spread 58.6% (median of 25, range
  41.9-74.7%). power_statement and interpretation_gate.null_framing updated to 1.50%, and the
  "pending" marker #16 left on the fat-tail clause is now resolved.
- A print bug in the #16 script reported p90 using the p10 percentile; corrected, medians and
  MDE figures are unaffected.
"""

reason = (
    "V3.17 Pre-Data Sector Grouping Repair and Final Power Derivation: the interpretation gate "
    "read sector from daily_quotes.sector, a column that mixes PSX's 4-digit codes with names from "
    "a hand-filed local list, and collapsed it with dict(zip(...)) so the last row per symbol won. "
    "That presented 39 groups where there are 27 sectors, splitting 12 real sectors in two - and that "
    "grouping is exactly what decides whether a significant primary result is reported as a crowding "
    "effect or as sector exposure. Replaced with a locked point-in-time sector_map keyed on the "
    "exchange's own code, chosen on evidence: codes matched live PSX market-watch on 429/429 symbols "
    "with no conflicts, while the names contradict the codes. All 138 universe names map, enforced by "
    "a hard error and by two new negative guard drills. Power was then re-derived once, on the "
    "corrected grouping, with 100 draws: simulated 10d MDE 1.400 -> 1.500%, analytic 1.226 -> 1.440%, "
    "sigma 0.626 -> 0.711%, power at a 1.0% spread 52.4 -> 58.6%. This is the last pre-data "
    "methodology change; the remaining blocker is a daily primary MTS source, not code."
)

res = record_amendment(
    conn=conn, hypothesis_id=spec["hypothesis_id"], amendment_no=17, pre_data=True,
    reason=reason, diff_text=diff_text.strip(), spec=spec, code_paths=code_files
)
print("Recorded Amendment #17:")
print(f"  Spec SHA256: {res['spec_sha256']}")
print(f"  Diff SHA256: {res['diff_sha256']}")
print(f"  sector_map : {dh['sector_map_sha256'][:12]}... ({dh['sector_map_count']} rows)")

chk = verify_registration(conn, spec["hypothesis_id"], spec, code_files)
print(f"Verify registration: {chk}")
assert chk["all_ok"] and chk["amendment_no"] == 17, f"Verification failed: {chk}"

anchor = json.loads(Path("v3_spec_immutable_anchor.json").read_text(encoding="utf-8"))
anchor.update({
    "amendment_no": 17,
    "spec_sha256": res["spec_sha256"],
    "spec": spec,
    "code_sha256": {Path(p).name: file_sha256(p) for p in code_files},
})
anchor["data_sha256"]["sector_map"] = dh["sector_map_sha256"]
Path("v3_spec_immutable_anchor.json").write_text(json.dumps(anchor, indent=2), encoding="utf-8")
print("\n[SUCCESS] Amendment #17 registered and anchor updated.")
