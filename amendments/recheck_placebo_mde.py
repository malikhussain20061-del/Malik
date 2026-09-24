"""Re-derive the sector-matched placebo, MDE and power on the locked sector_map.

Amendment #17 supersedes the first re-run (which used #16's name-first workaround over
daily_quotes.sector). Everything here groups on sector_map.sector_code only, uses 100
draws instead of 20 for stability, and re-derives the fat-tail power clause that was
left marked "pending" in #16.
"""

import sqlite3
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from econometric_audit import analytic_mde, hac_lrv, simulated_mde, simulated_power
from jegadeesh_titman_portfolio import load_market_panel
from mts_engine import require_full_sector_coverage, sector_map_map

DRAWS = 100
T_TARGET = 240
ALPHA = 0.025
POWER_TARGET = 0.80
HORIZON = 10
# Q5's registered sector mix, re-expressed in PSX 4-digit codes (Amendment #17).
Q5_COMPOSITION = {"0807": 5, "0820": 2, "0804": 2, "0821": 1, "0824": 1, "0809": 1}


def placebo_draws(rets, sector, composition, draws=DRAWS, seed=11):
    rng = np.random.default_rng(seed)
    uni = rets.mean(axis=1)
    lrvs, sds, spreads = [], [], []
    for _ in range(draws):
        cols = []
        for sec, n in composition.items():
            pool = [c for c in rets.columns if sector.get(c) == sec]
            if len(pool) < n:
                raise RuntimeError(f"sector {sec} pool {len(pool)} < required {n}")
            cols += list(rng.choice(pool, n, replace=False))
        s = (rets[cols].mean(axis=1) - uni).dropna()
        lrvs.append(hac_lrv(s, "qs", "andrews", True)[0])
        sds.append(float(s.std(ddof=1)))
        spreads.append(s)
    if len(spreads) < draws:
        raise RuntimeError(f"only {len(spreads)}/{draws} draws survived")
    return lrvs, sds, spreads


def main():
    conn = sqlite3.connect("psx.db")
    smap = sector_map_map(conn)
    universe = {r[0] for r in conn.execute("SELECT symbol FROM mts_eligible")}
    require_full_sector_coverage(conn, universe)

    panel = load_market_panel(conn)
    rets = panel.adj_open.pct_change(fill_method=None).iloc[1:]
    cols = [c for c in rets.columns if c in universe and rets[c].notna().mean() > 0.8]
    rets_el = rets[cols]
    print("=== SECTOR-MATCHED PLACEBO on locked sector_map ===")
    print(f"universe {len(universe)} | in panel with >80% coverage {len(cols)} | "
          f"distinct sectors {len(set(smap.values()))}")
    for sec, n in sorted(Q5_COMPOSITION.items()):
        print(f"  {sec}: draw {n} from pool {sum(1 for c in cols if smap.get(c) == sec)}")

    lrvs, sds, spreads = placebo_draws(rets_el, smap, Q5_COMPOSITION)
    med_lrv, med_sd = float(np.median(lrvs)), float(np.median(sds))
    print(f"\nmedian daily SD {med_sd:.6f} ({med_sd * 100:.3f}%)  "
          f"[p10 {np.percentile(sds, 10) * 100:.3f}%, p90 {np.percentile(sds, 90) * 100:.3f}%] "
          f"over {DRAWS} draws")
    print(f"median LRV     {med_lrv:.8f}")

    a240 = analytic_mde(med_lrv, T_TARGET)
    print(f"\nanalytic T={T_TARGET}: daily {a240['mde_daily'] * 100:.4f}% | "
          f"10d {a240['mde_per_horizon'] * 100:.3f}% | ann {a240['mde_annualized'] * 100:.2f}%")
    for T in (120, 180, 360):
        a = analytic_mde(med_lrv, T)
        print(f"analytic T={T:3d}: 10d {a['mde_per_horizon'] * 100:.3f}%")

    sim = [simulated_mde(s, T_TARGET, power=POWER_TARGET, alpha=ALPHA, B=200)["mde_daily"]
           for s in spreads]
    sim = [d for d in sim if d is not None]
    med_sim = float(np.median(sim))
    print(f"\nsimulated MDE ({len(sim)} draws, T={T_TARGET}, 80% power): "
          f"daily {med_sim * 100:.4f}% | 10d {med_sim * 1000:.3f}%")

    # The clause #16 left pending: power at a 1.0% 10-day spread under the placebo distribution.
    delta_daily = 0.010 / HORIZON
    powers = [simulated_power(s, delta_daily, T_TARGET, ALPHA, B=2000) for s in spreads[:25]]
    print(f"\npower at a 1.0% {HORIZON}-day spread (delta_daily={delta_daily:.5f}): "
          f"median {np.median(powers) * 100:.1f}%  "
          f"[min {min(powers) * 100:.1f}%, max {max(powers) * 100:.1f}%] over {len(powers)} placebos")
    conn.close()


if __name__ == "__main__":
    main()
