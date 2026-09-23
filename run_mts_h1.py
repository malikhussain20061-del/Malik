"""
python run_mts_h1.py mde | register | evaluate
"""
import hashlib
import json
import logging
import sqlite3
import sys
import numpy as np
import pandas as pd

from econometric_audit import (
    init_ledger, mean_test, mr_test, placebo_lrv, analytic_mde,
    simulated_mde, record_result, register_hypothesis,
    verify_registration
)
from jegadeesh_titman_portfolio import FrictionModel, JTPortfolio, load_market_panel
from mts_engine import (
    SignalConfig, build_cohorts, enforce_publication_lag, entry_schedule,
    init_schema, load_signal_panel
)

CODE_FILES = [
    "mts_engine.py",
    "jegadeesh_titman_portfolio.py",
    "econometric_audit.py",
    "run_mts_h1.py",
    "psx_data_v2.py",
    "daily_job.py"
]

QUOTES_SQL = "SELECT trade_date AS date, base_symbol AS symbol, close, volume FROM daily_quotes WHERE is_final=1 AND close > 0"

SPEC = {
    "hypothesis_id": "MTS_CROWDING_XS_H10_V3",
    "test_number": 2,
    "alpha": 0.025,
    "freeze_date": "2026-10-01",            # first report_date counted (after pre-registration window)
    "evaluation_sessions": 240,             # 240 sessions gives MDE ~1.23% per 10-day horizon (80% power)
    "power_statement": "Under sector-matched placebo matching Q5's exact composition (5 Banks, 2 E&P, 2 Cement, 1 OMC, 1 Power, 1 Fertilizer; sigma ~0.626%), T=240 sessions yields 10-day Simulated MDE of 1.400% (20-draw median) at 80% power (Analytic MDE 1.226%). Power at 1.0% 10-day spread is ~52.4% under empirical fat tails. If null cannot be rejected, conclusion is: 'No crowding effect larger than 1.40% per 10 sessions detected'.",
    "signal": {
        "column": "open_pct",
        "rank": "cross-sectional over financed names (L>0), average ties",
        "control_bucket": "Q0 for non-financed (L=0)",
        "min_financed": 40,
        "vanished_symbols": "excluded and logged to mts_anomalies (not zero-filled)",
        "missing_report": "no cohort"
    },
    "universe": {
        "eligible": "PSX_LIQUID_PROXY_139 (fixed 139 symbols meeting session>=190, vol>=50k, close>=10, plus core MTS). Locked for entire evaluation; no mid-run universe substitution.",
        "min_volume": 50000,
        "min_price": 10.0,
        "min_names": 25,
        "order": "filter then rank"
    },
    "timing": {
        "entry_lag_sessions": 2,
        "open_time_pkt": "09:30",
        "rule": "cohort dropped if captured_at >= entry open"
    },
    "valuation": {
        "total_return_via_build_adj_factor": "splits, bonuses, and cash dividends (div_wht=0.15)",
        "corporate_actions_rule": "All corporate actions derived from Exchange LDCP gap detection (prev_close - ldcp) and ratio analysis. Pipeline halts if ex-suffix or LDCP gap occurs without matching event in corporate_actions."
    },
    "data_integrity_hashes": {
        "corporate_actions_sha256": "f9f5d688627131ca1132643579aea2bb2b9e25ccf75d311cb86d71b41b0c4c9f",
        "corporate_actions_count": 393,
        "mts_eligible_sha256": "d758bde4b123a4e685c9fcb597bd2d9c0dc25225d120035fb3d60d10c5dfa79d",
        "mts_eligible_count": 139
    },
    "portfolio": {
        "K": 10,
        "H": 10,
        "weights": "EW over fillable",
        "valuation": "open-to-open Total Return",
        "upper_lock": "zero-fill tol 0.5%",
        "lower_lock": "roll, tol 0.5%",
        "trapped_cash": "idle in sleeve until next rebalance"
    },
    "primary": {
        "stat": "mean daily (Q5 - U)",
        "inference": "EWC nu=floor(0.4T^(2/3)), t_nu",
        "alternative": "less"
    },
    "secondary": [
        "QS+Andrews AR(1)+prewhitening",
        "PT MR test decreasing across financed buckets Q1..Q5, B=10000, block=10",
        "Sector-Neutral Crowding Spread: within-sector mean(Q5 - sector_universe)"
    ],
    "sector_neutral_spread_formula": {
        "definition": "S_t = sum_{s in S} (N_{Q5, s} / |Q5|) * (R_{Q5, s, t} - R_{U, s, t})",
        "eligible_universe_in_sector": "R_{U, s, t} includes all eligible stocks in sector s (including Q5 members)",
        "monopoly_sector_rule": "If sector s contains only Q5 stocks in eligible universe, spread for sector s is set to 0",
        "execution": "Computed via identical JT sleeve engine with sector-filtered schedules"
    },
    "interpretation_gate": {
        "rule": "Crowding effect confirmed ONLY IF primary test is statistically significant (p < 0.025) AND sector-neutral spread mean < 0. If primary is significant but sector-neutral spread >= 0, result is classified as Sector Exposure (e.g. macro banking drag).",
        "null_framing": "Failure to reject implies no crowding effect larger than 1.40% per 10 sessions detected."
    },
    "operational_rules": {
        "max_missing_cohort_pct": 0.10,
        "degradation_label": "DEGRADED if missing cohort sessions exceed 10%",
        "shadow_run_window": "2026-09-24 to 2026-09-30 (zero peeking at returns, feed health only)"
    },
    "feed_frequency_decision_rule": "Decision by user on 2026-09-30 based on shadow log: Between Sep 24 and Sep 30, all 5 consecutive sessions must receive fresh NCCPL reports with sequentially advancing internal report dates. If any session is missed or report date remains stagnant (e.g. at 2026-09-14), Oct 1 forward run SHALL BE POSTPONED via pre-data Amendment to adapt to weekly sleeves or delay freeze date. Zero peeking, zero cost for postponement.",
    "historical_total_return_status": "All 383 historical corporate actions backfilled via Exchange LDCP gap and ratio analysis. Full 238-day history is now pure Total Return.",
    "revision_policy": "Final pre-data revision V3. Post-freeze modifications restricted to documented bug-fix amendments logged with diff in hypothesis_amendments table.",
    "friction": FrictionModel().__dict__,
}

BUCKETS = ["Q0", "Q1", "Q2", "Q3", "Q4", "Q5", "U"]


def table_sha256(conn: sqlite3.Connection, table: str) -> tuple[str, int]:
    rows = conn.execute(f"SELECT * FROM {table} ORDER BY 1,2,3").fetchall()
    h = hashlib.sha256(json.dumps(rows, default=str).encode("utf-8")).hexdigest()
    return h, len(rows)


def main(mode: str, db: str = "psx.db") -> None:
    logging.basicConfig(level=logging.INFO)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    init_schema(conn)
    init_ledger(conn)
    panel = load_market_panel(conn)

    if mode == "mde":
        eligible_syms = set(r[0] for r in conn.execute("SELECT symbol FROM mts_eligible").fetchall())
        rets = panel.adj_open.pct_change(fill_method=None).iloc[1:]
        eligible_cols = [c for c in rets.columns if c in eligible_syms and rets[c].notna().mean() > 0.8]
        rets_el = rets[eligible_cols]
        pl = placebo_lrv(rets_el, n_names=12, draws=100)
        print("MTS-Eligible Universe Placebo median daily sd:", pl["median_sd"])
        for T in (120, 180, 240, 360):
            print(f"Analytic MDE for T={T}:", analytic_mde(pl["median_lrv"], T))
        print("Simulated MDE (T=240, B=500):", simulated_mde(pl["example_spread"], 240, B=500))
        return

    if mode == "register":
        print("spec sha256:", register_hypothesis(
            conn, SPEC["hypothesis_id"], SPEC["test_number"],
            SPEC, SPEC["alpha"], CODE_FILES
        ))
        return

    if mode == "shadow":
        # Operational dry run (Sep 24 - Sep 30) for feed health and zero peeking
        print("=== SHADOW RUN / FEED HEALTH CHECK ===")
        quotes = pd.read_sql_query(QUOTES_SQL, conn)
        quotes["date"] = pd.to_datetime(quotes["date"]).dt.strftime("%Y-%m-%d")
        cfg = SignalConfig(
            min_volume=SPEC["universe"]["min_volume"],
            min_price=SPEC["universe"]["min_price"],
            min_names=SPEC["universe"]["min_names"],
            min_financed=SPEC["signal"]["min_financed"]
        )
        signal = load_signal_panel(conn, SPEC["signal"]["column"])
        raw_cohorts = build_cohorts(signal, quotes, cfg)
        cohorts = enforce_publication_lag(
            raw_cohorts,
            panel.sessions,
            SPEC["timing"]["entry_lag_sessions"],
            SPEC["timing"]["open_time_pkt"]
        )
        print(f"Total formation dates in signal: {signal['report_date'].nunique()}")
        print(f"Raw cohorts formed: {raw_cohorts['formation_date'].nunique() if not raw_cohorts.empty else 0} dates, {len(raw_cohorts)} symbol-cohort rows")
        print(f"Cohorts surviving publication lag: {cohorts['formation_date'].nunique() if not cohorts.empty else 0} dates, {len(cohorts)} symbol-cohort rows")
        if not cohorts.empty:
            latest_fd = cohorts["formation_date"].max()
            c_latest = cohorts[cohorts["formation_date"] == latest_fd]
            print(f"Latest surviving cohort [{latest_fd}] bucket counts: {c_latest['bucket'].value_counts().to_dict()}")
        anom = pd.read_sql_query("SELECT * FROM mts_anomalies ORDER BY report_date DESC LIMIT 5", conn)
        print("Recent anomalies logged in mts_anomalies:")
        print(anom)
        return

    chk = verify_registration(conn, SPEC["hypothesis_id"], SPEC, CODE_FILES)
    if not chk["all_ok"]:
        raise SystemExit(f"Registration mismatch: {chk}")

    # Runtime data integrity verification: guard against post-freeze data tampering
    for tbl, key_hash, key_cnt in [
        ("corporate_actions", "corporate_actions_sha256", "corporate_actions_count"),
        ("mts_eligible", "mts_eligible_sha256", "mts_eligible_count")
    ]:
        cur_hash, cur_cnt = table_sha256(conn, tbl)
        exp_hash = SPEC["data_integrity_hashes"][key_hash]
        exp_cnt = SPEC["data_integrity_hashes"][key_cnt]
        if cur_hash != exp_hash or cur_cnt != exp_cnt:
            raise SystemExit(
                f"Data integrity violation: {tbl} tampered or altered! "
                f"Count={cur_cnt} (exp {exp_cnt}), SHA={cur_hash} (exp {exp_hash})"
            )

    cfg = SignalConfig(
        min_volume=SPEC["universe"]["min_volume"],
        min_price=SPEC["universe"]["min_price"],
        min_names=SPEC["universe"]["min_names"],
        min_financed=SPEC["signal"]["min_financed"]
    )
    signal = load_signal_panel(conn, SPEC["signal"]["column"])
    signal = signal[signal["report_date"] >= SPEC["freeze_date"]]
    quotes = pd.read_sql_query(QUOTES_SQL, conn)
    quotes["date"] = pd.to_datetime(quotes["date"]).dt.strftime("%Y-%m-%d")

    cohorts = enforce_publication_lag(
        build_cohorts(signal, quotes, cfg),
        panel.sessions,
        SPEC["timing"]["entry_lag_sessions"],
        SPEC["timing"]["open_time_pkt"]
    )

    eng = JTPortfolio(panel, FrictionModel(), K=SPEC["portfolio"]["K"])
    res = {b: eng.run(entry_schedule(cohorts, b)) for b in BUCKETS}
    starts = [r.valid_from for r in res.values()]

    if any(s is None for s in starts):
        logging.info("Warm-up incomplete: sleeves still accumulating initial positions. Standby.")
        print("Warm-up incomplete: waiting for initial K sessions.")
        return

    R = pd.DataFrame({b: res[b].returns for b in BUCKETS}).loc[max(starts):]
    n = len(R)

    if n < SPEC["evaluation_sessions"]:
        # Discipline: report feed health only, NEVER returns, before decision date.
        print(f"{n}/{SPEC['evaluation_sessions']} sessions. Cohort days: "
              f"{cohorts['entry_date'].nunique()}. Mean invested Q5: {res['Q5'].invested.mean():.2%}")
        return

    R = R.iloc[: SPEC["evaluation_sessions"]]

    # Sector-neutral spread calculation across cohorts via identical JT sleeve engine per SPEC
    sec_df = pd.read_sql_query(
        "SELECT DISTINCT base_symbol AS symbol, sector FROM daily_quotes WHERE sector IS NOT NULL AND sector != ''",
        conn
    )
    sec_map = dict(zip(sec_df["symbol"], sec_df["sector"]))

    def sector_schedule(cohorts_df: pd.DataFrame, bucket: str, sector: str, sec_map_dict: dict) -> dict[str, list[str]]:
        g = cohorts_df[cohorts_df["symbol"].map(sec_map_dict) == sector]
        if bucket != "U":
            g = g[g["bucket"] == bucket]
        return {d: sorted(x["symbol"].unique()) for d, x in g.groupby("entry_date")}

    w = cohorts.loc[cohorts["bucket"] == "Q5", "symbol"].map(sec_map).dropna().value_counts(normalize=True)
    n_nan = 0
    if not w.empty:
        S_components = []
        for s, ws in w.items():
            q5s = sector_schedule(cohorts, "Q5", s, sec_map)
            us_all = sector_schedule(cohorts, "U", s, sec_map)
            # Eliminate mechanical negative cash bias: restrict U sleeve to days when Q5 has active positions
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

    # Interpretation Gate evaluation
    if p_primary < SPEC["alpha"]:
        if sec_neutral_mean < 0:
            classification = "CROWDING_EFFECT_CONFIRMED"
        else:
            classification = "SECTOR_EXPOSURE_ONLY"
    else:
        classification = "NULL_NOT_REJECTED"

    # Missing cohort % & Degradation check (strictly over evaluated session window)
    cohorts_in_window = cohorts[cohorts["entry_date"].isin(R.index)]
    actual_cohort_days = cohorts_in_window["entry_date"].nunique()
    missing_cohort_pct = max(0.0, 1.0 - (actual_cohort_days / len(R.index)))
    degradation_status = "DEGRADED" if missing_cohort_pct > SPEC["operational_rules"]["max_missing_cohort_pct"] else "NORMAL"

    result = {
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
                } for b in BUCKETS
            }
        }
    }
    record_result(conn, SPEC["hypothesis_id"], result)
    print(result)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "evaluate")