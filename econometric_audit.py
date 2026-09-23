"""

econometric_audit.py — EWC / Andrews-QS HAC inference, Patton-Timmermann MR bootstrap,

analytic (noncentral-t) and simulated MDE, placebo variance, Holm, SHA-256 ledger.

"""

from __future__ import annotations



import hashlib

import json

import math

import sqlite3

from datetime import datetime, timezone

from pathlib import Path
from typing import Mapping



import numpy as np

import pandas as pd

from scipy import optimize, stats





def _clean(x) -> np.ndarray:

    x = np.asarray(x, float)

    return x[np.isfinite(x)]





def autocov(e: np.ndarray, j: int) -> float:

    return float(e[j:] @ e[: len(e) - j]) / len(e)





def kernel_weight(x: float, kernel: str) -> float:

    x = abs(x)

    if kernel == "bartlett":

        return max(0.0, 1.0 - x)

    if kernel == "qs":

        if x == 0:

            return 1.0

        z = 6.0 * math.pi * x / 5.0

        return 25.0 / (12.0 * math.pi ** 2 * x ** 2) * (math.sin(z) / z - math.cos(z))

    raise ValueError(kernel)





def _ar1(e: np.ndarray) -> float:

    return float(np.clip((e[1:] @ e[:-1]) / (e[:-1] @ e[:-1]), -0.97, 0.97))





def andrews_ar1_bandwidth(e: np.ndarray, kernel: str) -> float:

    T, rho = len(e), _ar1(e)

    if kernel == "bartlett":

        a1 = 4 * rho ** 2 / ((1 - rho) ** 2 * (1 + rho) ** 2)

        return 1.1447 * (a1 * T) ** (1 / 3)

    a2 = 4 * rho ** 2 / (1 - rho) ** 4

    return 1.3221 * (a2 * T) ** (1 / 5)





def nw94_bandwidth(e: np.ndarray, kernel: str) -> float:

    T = len(e)

    if kernel == "bartlett":

        n, q, c, p = int(4 * (T / 100) ** (2 / 9)), 1, 1.1447, 1 / 3

    else:

        n, q, c, p = int(4 * (T / 100) ** (2 / 25)), 2, 1.3221, 1 / 5

    sig = [autocov(e, j) for j in range(n + 1)]

    s0 = sig[0] + 2 * sum(sig[1:])

    sq = 2 * sum((j ** q) * sig[j] for j in range(1, n + 1))

    if s0 <= 0:

        return 1.0

    return c * ((sq / s0) ** 2) ** p * T ** p





def hac_lrv(x, kernel: str = "qs", bandwidth: str | float = "andrews",

            prewhiten: bool = True) -> tuple[float, float]:

    x = _clean(x)

    e = x - x.mean()

    rho = 0.0

    if prewhiten:

        rho = _ar1(e)

        u = e[1:] - rho * e[:-1]

    else:

        u = e

    if bandwidth == "andrews":

        S = andrews_ar1_bandwidth(u, kernel)

    elif bandwidth == "nw94":

        S = nw94_bandwidth(u, kernel)

    else:

        S = float(bandwidth)

    S = max(S, 1e-6)

    Tu = len(u)

    lrv = autocov(u, 0)

    maxlag = Tu - 1 if kernel == "qs" else min(Tu - 1, int(math.floor(S)))

    for j in range(1, maxlag + 1):

        lrv += 2.0 * kernel_weight(j / S, kernel) * autocov(u, j)

    if prewhiten:

        lrv /= (1.0 - rho) ** 2

    return max(lrv, 1e-18), S





def ewc_nu(T: int) -> int:

    return max(2, int(math.floor(0.4 * T ** (2 / 3))))





def _cos_basis(T: int, nu: int) -> np.ndarray:

    t = np.arange(1, T + 1)

    return math.sqrt(2.0 / T) * np.cos(math.pi * np.outer(np.arange(1, nu + 1), t - 0.5) / T)





def ewc_lrv(x, nu: int | None = None) -> tuple[float, int]:

    x = _clean(x)

    T = len(x)

    nu = nu or ewc_nu(T)

    lam = _cos_basis(T, nu) @ (x - x.mean())

    return float(np.mean(lam ** 2)), nu





def _p(t: float, alternative: str, dist) -> float:

    if alternative == "less":

        return float(dist.cdf(t))

    if alternative == "greater":

        return float(dist.sf(t))

    return float(2 * dist.sf(abs(t)))





def mean_test(x, alternative: str = "less") -> dict:

    x = _clean(x)

    T, m = len(x), float(x.mean())

    lrv, nu = ewc_lrv(x)

    t1 = m / math.sqrt(lrv / T)

    lrv2, S = hac_lrv(x, "qs", "andrews", True)

    t2 = m / math.sqrt(lrv2 / T)

    return {"T": T, "mean_daily": m, "naive_se": float(x.std(ddof=1) / math.sqrt(T)),

            "primary_EWC": {"lrv": lrv, "nu": nu, "t": t1, "p": _p(t1, alternative, stats.t(nu))},

            "secondary_QS_Andrews_PW": {"lrv": lrv2, "bandwidth": S, "t": t2,

                                        "p": _p(t2, alternative, stats.norm)}}





def stationary_bootstrap_indices(T_src: int, n_out: int, mean_block: float,

                                 rng: np.random.Generator) -> np.ndarray:

    p = 1.0 / mean_block

    new = rng.random(n_out) < p

    starts = rng.integers(T_src, size=n_out)

    idx = np.empty(n_out, dtype=np.int64)

    idx[0] = starts[0]

    for t in range(1, n_out):

        idx[t] = starts[t] if new[t] else (idx[t - 1] + 1) % T_src

    return idx





def mr_test(R, decreasing: bool = True, B: int = 10_000, mean_block: float = 10.0,

            studentize: bool = False, seed: int = 20260923) -> dict:

    """R: T x N daily returns of Q1..QN (columns in order). Resamples DATES (rows jointly)."""

    R = np.asarray(R, float)

    R = R[np.all(np.isfinite(R), axis=1)]

    T, N = R.shape

    D = (R[:, :-1] - R[:, 1:]) if decreasing else (R[:, 1:] - R[:, :-1])

    dhat = D.mean(axis=0)

    rng = np.random.default_rng(seed)

    boot = np.empty((B, N - 1))

    for b in range(B):

        boot[b] = D[stationary_bootstrap_indices(T, T, mean_block, rng)].mean(axis=0)

    cen = boot - dhat

    if studentize:

        sd = boot.std(axis=0, ddof=1)

        J, Jb = float(np.min(dhat / sd)), np.min(cen / sd, axis=1)

    else:

        J, Jb = float(dhat.min()), cen.min(axis=1)

    return {"T": T, "delta_hat": dhat.tolist(), "J": J,

            "p_value": float(np.mean(Jb >= J)), "B": B, "mean_block": mean_block,

            "studentized": studentize}





def analytic_mde(lrv_daily: float, T: int, alpha: float = 0.025, power: float = 0.80,

                 horizon: int = 10, sessions_per_year: int = 248) -> dict:

    nu = ewc_nu(T)

    crit = stats.t.ppf(1 - alpha, nu)

    approx_mult = crit + stats.t.ppf(power, nu)

    f = lambda d: (1.0 - float(stats.nct.cdf(crit, nu, d))) - power
    try:
        exact_mult = float(optimize.brentq(f, 1e-6, 15.0))
    except Exception:
        exact_mult = float(approx_mult)

    se = math.sqrt(lrv_daily / T)

    d = exact_mult * se

    return {"T": T, "nu": nu, "se_daily": se, "mult_approx": approx_mult,

            "mult_exact_nct": exact_mult, "mde_daily": d, "mde_per_horizon": d * horizon,

            "mde_annualized": d * sessions_per_year}





def simulated_power(spread, delta_daily: float, T_target: int, alpha: float = 0.025,

                    B: int = 2000, mean_block: float = 10.0, seed: int = 7) -> float:

    """H1 'less': inject mean -delta into demeaned, block-resampled real/placebo spread."""

    x = _clean(spread)

    x = x - x.mean()

    rng = np.random.default_rng(seed)

    nu = ewc_nu(T_target)

    C = _cos_basis(T_target, nu)

    crit = stats.t.ppf(alpha, nu)

    rej = 0

    for _ in range(B):

        y = x[stationary_bootstrap_indices(len(x), T_target, mean_block, rng)] - delta_daily

        lam = C @ (y - y.mean())

        t = y.mean() / math.sqrt(np.mean(lam ** 2) / T_target)

        rej += t < crit

    return rej / B





def simulated_mde(spread, T_target: int, power: float = 0.80, alpha: float = 0.025,

                  grid: np.ndarray | None = None, **kw) -> dict:

    grid = grid if grid is not None else np.linspace(0.0002, 0.005, 25)

    curve = [(float(d), simulated_power(spread, d, T_target, alpha, **kw)) for d in grid]

    hit = next((d for d, pw in curve if pw >= power), None)

    return {"T": T_target, "mde_daily": hit, "power_curve": curve}





def placebo_lrv(returns: pd.DataFrame, n_names: int, draws: int = 500,

                seed: int = 11) -> dict:

    """Random fixed n-name EW portfolio minus EW universe. Uses NO MTS information."""

    rng = np.random.default_rng(seed)

    uni = returns.mean(axis=1)

    lrvs, sds, example = [], [], None

    for i in range(draws):

        cols = rng.choice(returns.columns.to_numpy(), n_names, replace=False)

        s = (returns[cols].mean(axis=1) - uni).dropna()

        lrvs.append(hac_lrv(s, "qs", "andrews", True)[0])

        sds.append(float(s.std(ddof=1)))

        if i == 0:

            example = s

    return {"median_lrv": float(np.median(lrvs)), "median_sd": float(np.median(sds)),
            "example_spread": example}


def placebo_sector_matched(returns: pd.DataFrame, sector: Mapping[str, str], composition: Mapping[str, int],
                           draws: int = 200, seed: int = 11) -> dict:
    """
    Draws placebo portfolios matching Q5's exact sector composition.
    Uses sector counts only (zero lookahead / peeking into returns).
    """
    rng = np.random.default_rng(seed)
    uni = returns.mean(axis=1)
    lrvs, sds, spreads = [], [], []
    for _ in range(draws):
        cols = []
        for sec, n in composition.items():
            pool = [c for c in returns.columns if sector.get(c) == sec]
            if pool:
                cols += list(rng.choice(pool, min(n, len(pool)), replace=False))
        if len(cols) < 5:
            continue
        s = (returns[cols].mean(axis=1) - uni).dropna()
        lrvs.append(hac_lrv(s, "qs", "andrews", True)[0])
        sds.append(float(s.std(ddof=1)))
        spreads.append(s)
    return {
        "median_lrv": float(np.median(lrvs)),
        "median_sd": float(np.median(sds)),
        "spreads": spreads,
        "example_spread": spreads[0] if spreads else None
    }





def holm_adjust(pvals) -> np.ndarray:

    p = np.asarray(pvals, float)

    m = len(p)

    adj, running = np.empty(m), 0.0

    for rank, i in enumerate(np.argsort(p)):

        running = max(running, min(1.0, (m - rank) * p[i]))

        adj[i] = running

    return adj





LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS hypothesis_ledger(
  hypothesis_id TEXT PRIMARY KEY,
  test_number INTEGER NOT NULL UNIQUE,
  spec_json TEXT NOT NULL,
  spec_sha256 TEXT NOT NULL,
  code_sha256_json TEXT NOT NULL,
  registered_at_utc TEXT NOT NULL,
  alpha REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'REGISTERED',
  result_json TEXT
);
CREATE TABLE IF NOT EXISTS hypothesis_amendments(
  hypothesis_id TEXT NOT NULL,
  amendment_no INTEGER NOT NULL,
  amended_at_utc TEXT NOT NULL,
  pre_data INTEGER NOT NULL,
  reason TEXT NOT NULL,
  diff_sha256 TEXT NOT NULL,
  diff_text TEXT NOT NULL,
  spec_sha256 TEXT NOT NULL,
  code_sha256_json TEXT NOT NULL,
  PRIMARY KEY(hypothesis_id, amendment_no)
);
"""





def init_ledger(conn: sqlite3.Connection) -> None:

    conn.executescript(LEDGER_SCHEMA)

    conn.commit()





def canonical_json(obj) -> str:

    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)





def sha256_text(s: str) -> str:

    return hashlib.sha256(s.encode("utf-8")).hexdigest()





def file_sha256(path: str | Path) -> str:

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()





def register_hypothesis(conn, hypothesis_id: str, test_number: int, spec: dict,

                        alpha: float, code_paths: list[str]) -> str:

    if conn.execute("SELECT 1 FROM hypothesis_ledger WHERE hypothesis_id=?",

                    (hypothesis_id,)).fetchone():

        raise RuntimeError(f"{hypothesis_id} already registered; registrations are immutable.")

    sj = canonical_json(spec)

    code = {Path(p).name: file_sha256(p) for p in code_paths}

    h = sha256_text(sj)

    conn.execute("INSERT INTO hypothesis_ledger(hypothesis_id,test_number,spec_json,spec_sha256,"

                 "code_sha256_json,registered_at_utc,alpha) VALUES (?,?,?,?,?,?,?)",

                 (hypothesis_id, test_number, sj, h, canonical_json(code),

                  datetime.now(timezone.utc).isoformat(), alpha))

    conn.commit()

    return h





def record_amendment(conn, hypothesis_id: str, amendment_no: int, pre_data: bool,
                     reason: str, diff_text: str, spec: dict, code_paths: list[str]) -> dict:
    sj = canonical_json(spec)
    code = {Path(p).name: file_sha256(p) for p in code_paths}
    spec_h = sha256_text(sj)
    diff_h = sha256_text(diff_text)
    conn.execute(
        "INSERT INTO hypothesis_amendments(hypothesis_id, amendment_no, amended_at_utc, "
        "pre_data, reason, diff_sha256, diff_text, spec_sha256, code_sha256_json) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (hypothesis_id, amendment_no, datetime.now(timezone.utc).isoformat(),
         1 if pre_data else 0, reason, diff_h, diff_text, spec_h, canonical_json(code))
    )
    conn.commit()
    return {"spec_sha256": spec_h, "diff_sha256": diff_h, "code_sha256": code}


def verify_registration(conn, hypothesis_id: str, spec: dict, code_paths: list[str]) -> dict:
    row = conn.execute("SELECT spec_sha256, code_sha256_json, status FROM hypothesis_ledger "
                       "WHERE hypothesis_id=?", (hypothesis_id,)).fetchone()
    if row is None:
        return {"all_ok": False, "reason": "not registered"}

    # Check for latest amendment if any
    amd = conn.execute(
        "SELECT spec_sha256, code_sha256_json, amendment_no FROM hypothesis_amendments "
        "WHERE hypothesis_id=? ORDER BY amendment_no DESC LIMIT 1", (hypothesis_id,)
    ).fetchone()

    expected_spec_sha = amd[0] if amd else row[0]
    expected_code_json = amd[1] if amd else row[1]

    code_now = {Path(p).name: file_sha256(p) for p in code_paths}
    spec_ok = sha256_text(canonical_json(spec)) == expected_spec_sha
    code_ok = code_now == json.loads(expected_code_json)
    return {
        "all_ok": spec_ok and code_ok,
        "spec_ok": spec_ok,
        "code_ok": code_ok,
        "status": row[2],
        "amendment_no": amd[2] if amd else 0
    }





def record_result(conn, hypothesis_id: str, result: dict) -> None:

    st = conn.execute("SELECT status FROM hypothesis_ledger WHERE hypothesis_id=?",

                      (hypothesis_id,)).fetchone()

    if st is None or st[0] != "REGISTERED":

        raise RuntimeError(f"Cannot record: status={st}")

    conn.execute("UPDATE hypothesis_ledger SET status='EVALUATED', result_json=? "

                 "WHERE hypothesis_id=?", (canonical_json(result), hypothesis_id))

    conn.commit()