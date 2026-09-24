"""Sector-matched placebo MDE, re-run on the Amendment #13/#14 repaired panel.

Two defects in the original scratch/test_sector_matched_mde.py made it return all-NaN
rather than a number:
  1. q5_composition keys are PSX sector NAMES while daily_quotes.sector holds a mix of
     names and 4-digit codes written by two different ingest paths, so no pool ever matched
     and every draw was skipped.
  2. The skip was silent - the function still returned a dict of NaN medians.
Here the sector map is built name-first, and an empty draw set is a hard error.
"""

import re
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from econometric_audit import analytic_mde, hac_lrv, simulated_mde
from jegadeesh_titman_portfolio import load_market_panel

CODE_RE = re.compile(r"^\d{4}$")
Q5_COMPOSITION = {
    "COMMERCIAL BANKS": 5,
    "OIL & GAS EXPLORATION COMPANIES": 2,
    "CEMENT": 2,
    "OIL & GAS MARKETING COMPANIES": 1,
    "POWER GENERATION & DISTRIBUTION": 1,
    "FERTILIZER": 1,
}


def sector_map(conn) -> dict[str, str]:
    """Name-form sector wins over a 4-digit code for the same base_symbol."""
    out: dict[str, str] = {}
    for base, sec in conn.execute(
            "SELECT base_symbol, sector FROM daily_quotes "
            "WHERE sector IS NOT NULL AND sector <> '' GROUP BY 1, 2"):
        if not base or not sec:
            continue
        if CODE_RE.match(sec):
            out.setdefault(base, sec)
        else:
            out[base] = sec
    return out


def placebo_sector_matched(rets, sector, composition, draws=200, seed=11):
    rng = np.random.default_rng(seed)
    uni = rets.mean(axis=1)
    lrvs, sds, spreads = [], [], []
    for _ in range(draws):
        cols = []
        for sec, n in composition.items():
            pool = [c for c in rets.columns if sector.get(c) == sec]
            if pool:
                cols += list(rng.choice(pool, min(n, len(pool)), replace=False))
        if len(cols) < 5:
            continue
        s = (rets[cols].mean(axis=1) - uni).dropna()
        lrvs.append(hac_lrv(s, "qs", "andrews", True)[0])
        sds.append(float(s.std(ddof=1)))
        spreads.append(s)
    if not lrvs:
        raise RuntimeError(
            "Every placebo draw was skipped: the sector map never matches q5_composition. "
            f"sample sectors: {sorted({v for v in sector.values() if v})[:8]}")
    return {"median_lrv": float(np.median(lrvs)), "median_sd": float(np.median(sds)),
            "n_draws": len(lrvs), "spreads": spreads}


def main():
    conn = sqlite3.connect("psx.db")
    panel = load_market_panel(conn)
    eligible = {r[0] for r in conn.execute("SELECT symbol FROM mts_eligible")}
    smap = sector_map(conn)

    rets = panel.adj_open.pct_change(fill_method=None).iloc[1:]
    cols = [c for c in rets.columns if c in eligible and rets[c].notna().mean() > 0.8]
    rets_el = rets[cols]
    print(f"=== SECTOR-MATCHED PLACEBO (repaired panel) ===")
    print(f"eligible in universe: {len(eligible)} | with >80% coverage: {len(cols)}")
    print(f"sector names resolved for {sum(1 for c in cols if smap.get(c))}/{len(cols)} panel names")
    for sec, n in Q5_COMPOSITION.items():
        pool = [c for c in cols if smap.get(c) == sec]
        print(f"  pool {sec:36} need {n} have {len(pool)}")

    res = placebo_sector_matched(rets_el, smap, Q5_COMPOSITION)
    sd = res["median_sd"]
    print(f"\nmedian daily SD: {sd:.6f} ({sd * 100:.3f}%)   draws used: {res['n_draws']}")
    print(f"median LRV:      {res['median_lrv']:.6e}")
    for T in (120, 180, 240, 360):
        a = analytic_mde(res["median_lrv"], T)
        print(f"T={T} (nu={a['nu']:4.1f}): MDE daily {a['mde_daily'] * 100:.3f}% | "
              f"10d {a['mde_per_horizon'] * 100:.3f}% | ann {a['mde_annualized'] * 100:.2f}%")
    sim = [simulated_mde(s, 240, B=200)["mde_daily"] for s in res["spreads"][:20]]
    med_sim = float(np.median(sim))
    print(f"20-draw median SIMULATED MDE (T=240): daily {med_sim * 100:.4f}% | 10d {med_sim * 1000:.3f}%")
    print(f"\nRegistered in SPEC: sigma ~0.626%, simulated MDE(10d) 1.400%, analytic MDE 1.226%")
    conn.close()


if __name__ == "__main__":
    main()
