"""
fire_drill_evaluate.py — End-to-End Decision Day Fire Drill per Opus 5.5
Tests complete evaluation path on psx.db copy with synthetic noise signal.
Catches all potential runtime crashes before live forward decision day.
"""
import shutil
import sqlite3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(".").resolve()))
import numpy as np
import pandas as pd

# Step 1: Copy psx.db to scratch/fire_drill.db
src_db = Path("psx.db")
drill_db = Path("scratch/fire_drill.db")
shutil.copyfile(src_db, drill_db)

conn = sqlite3.connect(drill_db)
conn.execute("PRAGMA journal_mode=WAL")

# Step 2: Get 70 historical dates from daily_quotes
dates = sorted(pd.read_sql_query(
    "SELECT DISTINCT trade_date FROM daily_quotes WHERE is_final=1 ORDER BY trade_date", conn
)["trade_date"].tolist())[50:120]  # 70 sessions

freeze_date = dates[0]
print(f"Fire drill window: {len(dates)} dates from {freeze_date} to {dates[-1]}")

# Get eligible symbols
elig_syms = sorted(set(pd.read_sql_query("SELECT symbol FROM mts_eligible", conn)["symbol"].tolist()))
print(f"Eligible universe: {len(elig_syms)} symbols")

# Step 3: Insert synthetic noise signal into mts_snapshots for fire drill
rng = np.random.default_rng(42)
synth_rows = []
for d in dates:
    # 65 financed names with random signal, rest 0
    shuffled = rng.permutation(elig_syms)
    financed = shuffled[:65]
    for s in elig_syms:
        sig = float(rng.uniform(0.1, 5.0)) if s in financed else 0.0
        vol = float(rng.uniform(100000, 5000000)) if s in financed else 0.0
        amt = vol * 50.0
        synth_rows.append((d, s, s, vol, amt, 0.0, 0.0, 20.0, sig, 1e8, f"{d}T18:00:00+05:00", "DRILL_SHA"))

conn.execute("DELETE FROM mts_snapshots")
conn.executemany("""
    INSERT INTO mts_snapshots(
        report_date, symbol, raw_symbol, mts_volume, mts_amount, new_mts_volume,
        new_mts_amount, weighted_rate, open_pct, implied_denominator, captured_at, report_sha256
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
""", synth_rows)
conn.commit()

# Step 4: Import core pipeline modules and run end-to-end
from jegadeesh_titman_portfolio import FrictionModel, JTPortfolio, load_market_panel
from mts_engine import SignalConfig, build_cohorts, enforce_publication_lag, entry_schedule, load_signal_panel
from econometric_audit import mean_test, mr_test, record_result
import run_mts_h1

DRILL_SPEC = dict(run_mts_h1.SPEC)
DRILL_SPEC["freeze_date"] = freeze_date
DRILL_SPEC["evaluation_sessions"] = 50  # test with 50 sessions

panel = load_market_panel(conn)
cfg = SignalConfig(
    min_volume=DRILL_SPEC["universe"]["min_volume"],
    min_price=DRILL_SPEC["universe"]["min_price"],
    min_names=DRILL_SPEC["universe"]["min_names"],
    min_financed=DRILL_SPEC["signal"]["min_financed"]
)

signal = load_signal_panel(conn, DRILL_SPEC["signal"]["column"])
signal = signal[signal["report_date"] >= DRILL_SPEC["freeze_date"]]
quotes = pd.read_sql_query(run_mts_h1.QUOTES_SQL, conn)
quotes["date"] = pd.to_datetime(quotes["date"]).dt.strftime("%Y-%m-%d")

cohorts = enforce_publication_lag(
    build_cohorts(signal, quotes, cfg),
    panel.sessions,
    DRILL_SPEC["timing"]["entry_lag_sessions"],
    DRILL_SPEC["timing"]["open_time_pkt"]
)

print(f"Formed cohorts: {cohorts['entry_date'].nunique()} entry dates")

eng = JTPortfolio(panel, FrictionModel(), K=DRILL_SPEC["portfolio"]["K"])
res = {b: eng.run(entry_schedule(cohorts, b)) for b in run_mts_h1.BUCKETS}
starts = [r.valid_from for r in res.values()]
print(f"Warmup starts: max valid_from = {max(starts)}")

R = pd.DataFrame({b: res[b].returns for b in run_mts_h1.BUCKETS}).loc[max(starts):]
print(f"Available evaluated sessions: {len(R)}")

R = R.iloc[: DRILL_SPEC["evaluation_sessions"]]

# Sector-neutral spread calculation
# Amendment #17: group on the locked sector_map, never daily_quotes.sector.
from mts_engine import sector_map_map, require_full_sector_coverage
sec_map = sector_map_map(conn)
require_full_sector_coverage(conn, set(elig_syms))
print(f"Sector groups available: {len(set(sec_map.values()))} (PSX codes, not mixed name/code)")

def sector_schedule(cohorts_df: pd.DataFrame, bucket: str, sector: str, sec_map_dict: dict) -> dict[str, list[str]]:
    g = cohorts_df[cohorts_df["symbol"].map(sec_map_dict) == sector]
    if bucket != "U":
        g = g[g["bucket"] == bucket]
    return {d: sorted(x["symbol"].unique()) for d, x in g.groupby("entry_date")}

w = cohorts.loc[cohorts["bucket"] == "Q5", "symbol"].map(sec_map).dropna().value_counts(normalize=True)
print(f"Sector weights for Q5: {w.to_dict()}")

n_nan = 0
if not w.empty:
    S_components = []
    for s, ws in w.items():
        q5s = sector_schedule(cohorts, "Q5", s, sec_map)
        us_all = sector_schedule(cohorts, "U", s, sec_map)
        us = {d: us_all[d] for d in q5s if d in us_all}
        ret_q5 = eng.run(q5s).returns
        ret_u = eng.run(us).returns
        S_components.append(float(ws) * (ret_q5 - ret_u))
    S_df = pd.concat(S_components, axis=1).reindex(R.index)
    n_nan = int(S_df.isna().any(axis=1).sum())
    S = S_df.fillna(0.0).sum(axis=1)
    sec_neutral_mean = float(S.mean())
else:
    sec_neutral_mean = 0.0

primary_res = mean_test(R["Q5"] - R["U"], "less")
p_primary = primary_res["primary_EWC"]["p"]

if p_primary < DRILL_SPEC["alpha"]:
    classification = "CROWDING_EFFECT_CONFIRMED" if sec_neutral_mean < 0 else "SECTOR_EXPOSURE_ONLY"
else:
    classification = "NULL_NOT_REJECTED"

cohorts_in_window = cohorts[cohorts["entry_date"].isin(R.index)]
actual_cohort_days = cohorts_in_window["entry_date"].nunique()
missing_cohort_pct = max(0.0, 1.0 - (actual_cohort_days / len(R.index)))
degradation_status = "DEGRADED" if missing_cohort_pct > DRILL_SPEC["operational_rules"]["max_missing_cohort_pct"] else "NORMAL"

final_result = {
    "primary": primary_res,
    "mr": mr_test(R[["Q1", "Q2", "Q3", "Q4", "Q5"]].to_numpy()),
    "sector_neutral_spread_mean": sec_neutral_mean,
    "classification": classification,
    "degradation_status": degradation_status,
    "missing_cohort_pct": missing_cohort_pct,
    "diagnostics": {
        "sector_neutral_nan_days": n_nan,
        **{
            b: {
                "mean_invested": float(res[b].invested.mean()),
                "max_trapped": int(res[b].n_trapped.max())
            } for b in run_mts_h1.BUCKETS
        }
    }
}

record_result(conn, DRILL_SPEC["hypothesis_id"], final_result)
print("\n=== FIRE DRILL SUCCESSFUL! ===")
print("Classification:", final_result["classification"])
print("Primary p-value:", p_primary)
print("Sector-neutral spread mean:", sec_neutral_mean)
print("Degradation status:", degradation_status)
print("Result recorded cleanly in ledger without exceptions.")
