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
    init_schema, load_signal_panel,
    require_full_sector_coverage, sector_map_map as load_sector_map, sector_map_sha256
)

CODE_FILES = [
    "mts_engine.py",
    "jegadeesh_titman_portfolio.py",
    "econometric_audit.py",
    "run_mts_h1.py",
    "psx_data_v2.py",
    "daily_job.py",
    "ca_migration.py",
    "add_corporate_action.py"
]

QUOTES_SQL = "SELECT trade_date AS date, base_symbol AS symbol, close, volume FROM daily_quotes WHERE is_final=1 AND close > 0"

SPEC = {
    "hypothesis_id": "MTS_CROWDING_XS_H10_V3",
    "test_number": 2,
    "alpha": 0.025,
    "freeze_date": "2026-10-01",            # data partition boundary only: first report_date counted, and the
                                            # ex_date < boundary slice whose SHA-256 is ledger-locked
    "evaluation_start": None,               # null => pipeline stays in shadow; set only by a later pre-data amendment
    "evaluation_start_condition": (
        "The 240-session live clock starts on the first session after a verified NCCPL primary MTS source has "
        "delivered 5 consecutive sessions with sequentially advancing internal report dates and zero parse "
        "rejections. No calendar date is pre-committed; postponement carries zero cost and zero peeking."
    ),
    "evaluation_sessions": 240,             # 240 sessions gives MDE ~1.23% per 10-day horizon (80% power)
    "power_statement": "Re-derived on the Amendment #13 repaired panel and the Amendment #17 locked sector_map (amendments/recheck_placebo_mde.py, pre-data). Under sector-matched placebo matching Q5's exact composition by PSX sector code - 0807 x5, 0820 x2, 0804 x2, 0821 x1, 0824 x1, 0809 x1 - drawn from the 138-name universe across 27 sectors, T=240 sessions yields a 10-day Simulated MDE of 1.500% (median of 100 draws, 80% power, B=200) and an Analytic MDE of 1.440%. Median daily placebo sigma is 0.711% (p10 0.668%, p90 0.924%) with median long-run variance 5.536e-05. Power against a 1.0% 10-day spread is 58.6% (median over 25 block-resampled placebos, range 41.9%-74.7%). This supersedes the original registration (sigma 0.626%, simulated 1.400%, analytic 1.226%, power at 1.0% ~52.4%), which was computed on a panel where 8 real tickers were merged into phantom bases and on a sector grouping that split 27 sectors into 39 groups. If null cannot be rejected, conclusion is: 'No crowding effect larger than 1.50% per 10 sessions detected'.",
    "signal": {
        "column": "open_pct",
        "rank": "cross-sectional over financed names (L>0), average ties",
        "control_bucket": "Q0 for non-financed (L=0)",
        "min_financed": 40,
        "vanished_symbols": "excluded and logged to mts_anomalies (not zero-filled)",
        "missing_report": "no cohort"
    },
    "universe": {
        "eligible": "PSX_LIQUID_PROXY (fixed 138 symbols meeting session>=190, vol>=50k, close>=10, plus core MTS). Locked for entire evaluation; no mid-run universe substitution. Was registered as 139 until Amendment #16 removed the phantom row 'HU', which is not a listed PSX ticker.",
        "min_volume": 50000,
        "min_price": 10.0,
        "min_names": 25,
        "order": "filter then rank"
    },
    "timing": {
        "entry_lag_sessions": 2,
        "open_time_pkt": "09:30",
        "rule": "cohort dropped if captured_at >= entry open",
        "formation_key": "The report cover date (mts_snapshots.report_date), which is the date the "
                         "positions became publicly available. Entry counts entry_lag_sessions from "
                         "THAT date. The per-row as-of date is recorded in mts_snapshots.data_as_of "
                         "for staleness only and is never a formation key - ranking or entering off it "
                         "would trade on a session before the report existed.",
        "measured_staleness": "2026-09-14 cover reported positions as of 2026-09-11: 3 calendar days "
                              "but 1 trading session, because 12-13 September was a weekend. Logged "
                              "daily as a PUBLICATION_LAG anomaly. A stale signal is unbiased, only "
                              "weaker; a signal formed on its as-of date would be look-ahead.",
    },
    "valuation": {
        "total_return_via_build_adj_factor": "splits, bonuses, and cash dividends (div_wht=0.15)",
        "corporate_actions_rule": "All corporate actions derived from Exchange LDCP gap detection (prev_close - ldcp) and ratio analysis. Pipeline halts if ex-suffix or LDCP gap occurs without matching event in corporate_actions."
    },
    "data_integrity_hashes": {
        "corporate_actions_sha256": "2b40ec46e7d4e1e4ee0c8e104ea8403a5bfe774cc6794606b7decb9c25505ebd",
        "corporate_actions_count": 399,
        "corporate_actions_sha256_superseded": {
            "hash": "9bd574f028fcb00da3b3aa56b3bee942db2233964d950c27648595091d75c04f",
            "count": 393,
            "amendment": 11,
            "superseded_by": 13,
            "why": "3 HUBC cash dividends were filed under the phantom base 'HU'; Amendment #13 VOIDed "
                   "them and appended the same events under the real ticker. Active event count is unchanged "
                   "at 393; the partition grows to 399 rows because VOID and corrected rows are both appended."
        },
        "mts_eligible_sha256": "5c44519bf807d11feb4d674294b65a205c3f6316fb8ab9e59f696ac78fff4877",
        "mts_eligible_count": 138,
        "sector_map_sha256": "216bb41885c59c7b0c7f53bc59a520c67a5409a83b867974b0cff9726833da35",
        "sector_map_count": 138,
        "sector_map_rule": "One locked point-in-time snapshot (2026-09-24) of PSX's 4-digit sector "
                           "code per symbol, covering all 138 eligible names. Sector-neutral spread and "
                           "the interpretation gate group on this table only; daily_quotes.sector is never "
                           "read for grouping, because it mixes those codes with names from a hand-filed "
                           "local list and split 27 real sectors into 39 groups. A symbol that changes "
                           "sector during the run keeps its snapshot value.",
        "mts_eligible_sha256_superseded": {
            "hash": "d758bde4b123a4e685c9fcb597bd2d9c0dc25225d120035fb3d60d10c5dfa79d",
            "count": 139,
            "amendment": 10,
            "superseded_by": 16,
            "why": "The registered universe carried 139 rows but only 138 real names. 'HU' is a phantom "
                   "inherited from the pre-#13 base_symbol defect: it is absent from PSX market-watch, "
                   "has zero quote rows, and 'HUBC' - the ticker it was derived from - was already a "
                   "separate member. Removing it changes no cohort, only the honesty of the count."
        },
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
        "sector_source": "sector_map table only (locked 2026-09-24 snapshot of PSX 4-digit sector codes); daily_quotes.sector is never used for grouping",
        "definition": "S_t = sum_{s in S} (N_{Q5, s} / |Q5|) * (R_{Q5, s, t} - R_{U, s, t})",
        "eligible_universe_in_sector": "R_{U, s, t} includes all eligible stocks in sector s (including Q5 members)",
        "monopoly_sector_rule": "If sector s contains only Q5 stocks in eligible universe, spread for sector s is set to 0",
        "execution": "Computed via identical JT sleeve engine with sector-filtered schedules",
        "q5_sector_composition": {
            "0807": 5, "0820": 2, "0804": 2, "0821": 1, "0824": 1, "0809": 1
        },
        "q5_composition_note": "Re-expressed from sector names to PSX sector codes in Amendment #17. "
                               "Universe pools behind each slot: 0807=14, 0820=4, 0804=11, 0821=5, "
                               "0824=8, 0809=5. The 0820 slot is the binding one at 2 of 4."
    },
    "interpretation_gate": {
        "rule": "Crowding effect confirmed ONLY IF primary test is statistically significant (p < 0.025) AND sector-neutral spread mean < 0. If primary is significant but sector-neutral spread >= 0, result is classified as Sector Exposure (e.g. macro banking drag).",
        "null_framing": "Failure to reject implies no crowding effect larger than 1.50% per 10 sessions detected."
    },
    "operational_rules": {
        "max_missing_cohort_pct": 0.10,
        "degradation_label": "DEGRADED if missing cohort sessions exceed 10%",
        "shadow_run_window": "2026-09-24 to 2026-09-30 (zero peeking at returns, feed health only)",
        "shadow_window_1_outcome": "FAILED on 2026-09-24: only one MTS report date (2026-09-14) exists in the "
                                   "archive and the configured source has not advanced since. Postponement "
                                   "branch of feed_frequency_decision_rule taken."
    },
    "feed_frequency_decision_rule": "Decision by user on 2026-09-30 based on shadow log: Between Sep 24 and Sep 30, all 5 consecutive sessions must receive fresh NCCPL reports with sequentially advancing internal report dates. If any session is missed or report date remains stagnant (e.g. at 2026-09-14), Oct 1 forward run SHALL BE POSTPONED via pre-data Amendment to adapt to weekly sleeves or delay freeze date. Zero peeking, zero cost for postponement.",
    "feed_frequency_decision_outcome": "POSTPONED (Amendment #12, 2026-09-24, pre-data). The rule's stagnation "
                                       "condition is already met on shadow Day 1: the sole archived report is "
                                       "internal-dated 2026-09-14 and the configured source "
                                       "(scstrade.com MTS Report.pdf) still serves those identical bytes. "
                                       "The Oct 1 automatic switch to evaluate mode is removed; evaluation_start "
                                       "is now a condition, not a date. Weekly-sleeve adaptation was NOT taken, "
                                       "because a weekly sleeve would dilute the carrying-cost mechanism under "
                                       "test; if no daily primary source is ever found, that becomes a new "
                                       "hypothesis (V4), not an amendment to this one.",
    "historical_total_return_status": "All 393 historical corporate actions backfilled via Exchange LDCP gap and ratio analysis. Full 238-day history is now pure Total Return.",
    "revision_policy": "Final pre-data revision V3. Post-freeze modifications restricted to documented bug-fix amendments logged with diff in hypothesis_amendments table.",
    "friction": FrictionModel().__dict__,
}

BUCKETS = ["Q0", "Q1", "Q2", "Q3", "Q4", "Q5", "U"]


def table_sha256(conn: sqlite3.Connection, table: str, where: str = "", params: tuple = ()) -> tuple[str, int]:
    ncol = len(conn.execute(f"PRAGMA table_info({table})").fetchall())
    order = ",".join(str(i) for i in range(1, ncol + 1))
    rows = conn.execute(f"SELECT * FROM {table} {where} ORDER BY {order}", params).fetchall()
    return hashlib.sha256(json.dumps(rows, default=str).encode("utf-8")).hexdigest(), len(rows)


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
    # 1. Historical corporate actions partition (< freeze_date): frozen and tamper-proof
    ca_hash, ca_cnt = table_sha256(conn, "corporate_actions", "WHERE ex_date < ?", (SPEC["freeze_date"],))
    exp_ca_hash = SPEC["data_integrity_hashes"]["corporate_actions_sha256"]
    exp_ca_cnt = SPEC["data_integrity_hashes"]["corporate_actions_count"]
    if ca_hash != exp_ca_hash or ca_cnt != exp_ca_cnt:
        raise SystemExit(
            f"Data integrity violation: corporate_actions historical partition (< {SPEC['freeze_date']}) tampered! "
            f"Count={ca_cnt} (exp {exp_ca_cnt}), SHA={ca_hash} (exp {exp_ca_hash})"
        )

    # 2. Forward corporate actions (>= freeze_date): must have valid source and ingest_ts
    fwd_invalid = conn.execute(
        "SELECT COUNT(*) FROM corporate_actions WHERE ex_date >= ? AND (source IS NULL OR source = '' OR ingest_ts IS NULL)",
        (SPEC["freeze_date"],)
    ).fetchone()[0]
    if fwd_invalid > 0:
        raise SystemExit(f"Data integrity violation: {fwd_invalid} forward corporate actions lack source or ingest_ts provenance!")

    # 3. MTS eligible universe: permanently fixed for entire forward evaluation
    el_hash, el_cnt = table_sha256(conn, "mts_eligible")
    exp_el_hash = SPEC["data_integrity_hashes"]["mts_eligible_sha256"]
    exp_el_cnt = SPEC["data_integrity_hashes"]["mts_eligible_count"]
    if el_hash != exp_el_hash or el_cnt != exp_el_cnt:
        raise SystemExit(
            f"Data integrity violation: mts_eligible universe tampered! "
            f"Count={el_cnt} (exp {exp_el_cnt}), SHA={el_hash} (exp {exp_el_hash})"
        )

    # 3b. Sector map: one locked point-in-time assignment, hashed like the universe itself
    sm_hash, sm_cnt = sector_map_sha256(conn)
    exp_sm_hash = SPEC["data_integrity_hashes"]["sector_map_sha256"]
    exp_sm_cnt = SPEC["data_integrity_hashes"]["sector_map_count"]
    if sm_hash != exp_sm_hash or sm_cnt != exp_sm_cnt:
        raise SystemExit(
            f"Data integrity violation: sector_map tampered! "
            f"Count={sm_cnt} (exp {exp_sm_cnt}), SHA={sm_hash} (exp {exp_sm_hash})"
        )
    require_full_sector_coverage(conn, {r[0] for r in conn.execute(
        "SELECT symbol FROM mts_eligible")})

    # 4. Mandatory append-only triggers integrity check
    trg = dict(conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name='corporate_actions'"
    ).fetchall())
    expected_triggers = {"ca_no_update", "ca_no_delete", "ca_no_replace"}
    if not expected_triggers <= trg.keys() or any("RAISE(ABORT" not in trg[k] for k in expected_triggers):
        raise SystemExit("Data integrity violation: corporate_actions append-only triggers missing or altered!")

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
    # Locked point-in-time sector assignment from sector_map, never daily_quotes.sector:
    # that column mixes PSX 4-digit codes with hand-filed names and splits real sectors.
    sec_map = load_sector_map(conn)
    require_full_sector_coverage(conn, set(cohorts["symbol"].unique()))

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