"""
python run_mts_h1.py mde | register | evaluate
"""
import logging
import sqlite3
import sys
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
    "run_mts_h1.py"
]

QUOTES_SQL = "SELECT trade_date AS date, base_symbol AS symbol, close, volume FROM daily_quotes WHERE is_final=1 AND close > 0"

SPEC = {
    "hypothesis_id": "MTS_CROWDING_XS_H10_V3",
    "test_number": 2,
    "alpha": 0.025,
    "freeze_date": "2026-10-01",            # first report_date counted (after pre-registration window)
    "evaluation_sessions": 240,             # 240 sessions gives MDE ~1.23% per 10-day horizon (85% power)
    "power_statement": "Under sector-matched placebo matching Q5's exact composition (5 Banks, 2 E&P, 2 Cement, 1 OMC, 1 Power, 1 Fertilizer; sigma ~0.626%), T=240 sessions yields 10-day MDE of 1.226% (analytic, nu=15) and 1.400% (20-draw simulated) at 85% power. Power at 1.0% 10-day spread is ~65%.",
    "signal": {
        "column": "open_pct",
        "rank": "cross-sectional over financed names (L>0), average ties",
        "control_bucket": "Q0 for non-financed (L=0)",
        "min_financed": 40,
        "vanished_symbols": "excluded and logged to mts_anomalies (not zero-filled)",
        "missing_report": "no cohort"
    },
    "universe": {
        "eligible": "Official NCCPL MTS eligible securities list (periodically updated)",
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
        "total_return_via_build_adj_factor": "splits, bonuses, and cash dividends (div_wht=0.15)"
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
    "interpretation_gate": {
        "rule": "Crowding effect confirmed ONLY IF primary test is statistically significant AND sector-neutral spread < 0. Otherwise classified as Sector Exposure.",
        "null_framing": "Failure to reject implies no crowding effect larger than 1.40% per 10 sessions detected."
    },
    "operational_rules": {
        "max_missing_cohort_pct": 0.10,
        "degradation_label": "DEGRADED if missing cohort sessions exceed 10%"
    },
    "revision_policy": "Final pre-data revision V3. Post-freeze modifications restricted to documented bug-fix amendments logged with diff in hypothesis_amendments table.",
    "friction": FrictionModel().__dict__,
}

BUCKETS = ["Q0", "Q1", "Q2", "Q3", "Q4", "Q5", "U"]


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

    chk = verify_registration(conn, SPEC["hypothesis_id"], SPEC, CODE_FILES)
    if not chk["all_ok"]:
        raise SystemExit(f"Registration mismatch: {chk}")

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
        raise SystemExit("Warm-up incomplete.")

    R = pd.DataFrame({b: res[b].returns for b in BUCKETS}).loc[max(starts):]
    n = len(R)

    if n < SPEC["evaluation_sessions"]:
        # Discipline: report feed health only, NEVER returns, before decision date.
        print(f"{n}/{SPEC['evaluation_sessions']} sessions. Cohort days: "
              f"{cohorts['entry_date'].nunique()}. Mean invested Q5: {res['Q5'].invested.mean():.2%}")
        return

    R = R.iloc[: SPEC["evaluation_sessions"]]
    result = {
        "primary": mean_test(R["Q5"] - R["U"], "less"),
        "mr": mr_test(R[["Q1", "Q2", "Q3", "Q4", "Q5"]].to_numpy()),
        "diagnostics": {
            b: {
                "mean_invested": float(res[b].invested.mean()),
                "max_trapped": int(res[b].n_trapped.max())
            } for b in BUCKETS
        }
    }
    record_result(conn, SPEC["hypothesis_id"], result)
    print(result)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "evaluate")