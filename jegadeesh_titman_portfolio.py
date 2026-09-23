"""

jegadeesh_titman_portfolio.py — K overlapping buy-and-hold sleeves, open-to-open,

symmetric upper-lock zero-fill, involuntary lower-lock roll, full friction, annual CGT.

"""

from __future__ import annotations



import logging

import sqlite3

from dataclasses import dataclass



import numpy as np

import pandas as pd



from mts_engine import is_lower_locked, is_upper_locked



log = logging.getLogger("jt_portfolio")





@dataclass(frozen=True)

class FrictionModel:

    brokerage_rate: float = 0.0015

    brokerage_min_per_share: float = 0.03

    sales_tax_on_brokerage: float = 0.15     # Sindh SST; verify current rate

    levy_rate: float = 0.0005                # SECP/CDC/NCCPL per side



    def one_side(self, raw_price: float) -> float:

        b = max(self.brokerage_rate, self.brokerage_min_per_share / raw_price)

        return b * (1.0 + self.sales_tax_on_brokerage) + self.levy_rate





def build_adj_factor(quotes: pd.DataFrame, events: pd.DataFrame,
                     div_wht: float = 0.15) -> pd.DataFrame:
    """quotes: [date, symbol, close]; events: [symbol, ex_date, kind, value]
    kind: 'split' (value=ratio < 1 or n for n:1), 'bonus' (ratio < 1 or pct), 'cash' (Rs/share).
    adj_factor(t) = product of ratios of all events with ex_date > t.
    div_wht: dividend withholding rate (default 15%)."""
    q = quotes.sort_values(["symbol", "date"]).copy()
    q["date"] = pd.to_datetime(q["date"]).dt.strftime("%Y-%m-%d")
    q["adj_factor"] = 1.0
    if events.empty:
        return q[["date", "symbol", "adj_factor"]]
    for ev in events.itertuples():
        ex_date_str = pd.to_datetime(ev.ex_date).strftime("%Y-%m-%d")
        m = q["symbol"] == ev.symbol
        prev = q[m & (q["date"] < ex_date_str)]
        if prev.empty:
            continue
        kind = str(ev.kind).strip().lower()
        if kind == "split":
            val = float(ev.value)
            # If value < 1.0 (e.g. 0.10 for 10:1 split), ratio is directly value
            # If value >= 1.0 (e.g. 10.0 for 10:1 split), ratio is 1.0 / value
            ratio = val if (0 < val < 1.0) else (1.0 / val if val >= 1.0 else 1.0)
        elif kind == "bonus":
            val = float(ev.value)
            if not (0 < val < 1.0):
                raise ValueError(
                    f"{ev.symbol} {ex_date_str}: bonus ratio {val} must be in range (0, 1.0) "
                    f"(e.g. 0.05 for 5%, 0.20 for 20%). If >= 1.0, specify as split."
                )
            ratio = 1.0 / (1.0 + val)
        elif kind == "cash":
            cash_val = float(ev.value)
            dy = cash_val / prev["close"].iloc[-1]
            if not (0 < dy < 0.40):
                raise ValueError(
                    f"{ev.symbol} {ex_date_str}: implied dividend yield {dy:.1%} out of range (0, 40%); "
                    f"unit error? (Ensure face-value % is converted to Rs/share: value = pct/100 * face_value)"
                )
            ratio = 1.0 - cash_val * (1.0 - div_wht) / prev["close"].iloc[-1]
        else:
            raise ValueError(f"unhandled kind {ev.kind} for {ev.symbol}; handle rights explicitly")
        q.loc[m & (q["date"] < ex_date_str), "adj_factor"] *= ratio
    return q[["date", "symbol", "adj_factor"]]


# Fallback SQL if loading raw DataFrame directly
DEFAULT_SQL = """
SELECT date, symbol, open, volume, adj_factor, upper_limit, lower_limit
FROM quotes_adjusted_view
"""


@dataclass
class MarketPanel:
    sessions: list[str]
    adj_open: pd.DataFrame
    raw_open: pd.DataFrame
    upper: pd.DataFrame
    lower: pd.DataFrame

    @classmethod
    def from_long(cls, df: pd.DataFrame) -> "MarketPanel":
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        if "volume" in df:
            df.loc[df["volume"].fillna(0) <= 0, "open"] = np.nan
        df.loc[df["open"] <= 0, "open"] = np.nan
        df["adj_open"] = df["open"] * df["adj_factor"]
        sessions = sorted(df["date"].unique())
        piv = lambda c: df.pivot_table(index="date", columns="symbol", values=c,
                                       aggfunc="last").reindex(sessions)
        raw = piv("open")
        cols = raw.columns
        return cls(sessions, piv("adj_open").reindex(columns=cols), raw,
                   piv("upper_limit").reindex(columns=cols), piv("lower_limit").reindex(columns=cols))


class CorporateActionMissingError(RuntimeError):
    pass


def verify_corporate_actions_completeness(conn: sqlite3.Connection, freeze_date: str = "2026-10-01", tick: float = 0.011) -> None:
    """
    Verifies that every price gap exceeding 1 tick (0.011) on or after freeze_date
    has an accounted corporate action in corporate_actions.
    Also halts immediately if any rights issues ('XR' tickers) are detected.
    """
    # 1. Explicit check for rights counter tickers (XR)
    rights = pd.read_sql_query(
        "SELECT DISTINCT base_symbol FROM daily_quotes WHERE trade_date >= ? AND symbol LIKE '%XR'",
        conn, params=(freeze_date,)
    )
    if not rights.empty:
        raise CorporateActionMissingError(
            f"Trading halted: rights counter ('XR') detected on/after {freeze_date} for: {rights['base_symbol'].tolist()}"
        )

    # 2. Check for unaccounted gaps in spot equity quotes
    ca = pd.read_sql_query("SELECT base_symbol, ex_date FROM corporate_actions", conn)
    have = set(zip(ca["base_symbol"], ca["ex_date"]))
    q = pd.read_sql_query("""
        WITH q AS (
            SELECT base_symbol, symbol, trade_date, ldcp,
                   LAG(close) OVER (PARTITION BY base_symbol ORDER BY trade_date) AS prev_close
            FROM daily_quotes
            WHERE is_final = 1 AND base_symbol NOT LIKE '%-%'
              AND NOT (symbol != base_symbol AND (symbol LIKE '%R' OR symbol LIKE '%R1'))
        )
        SELECT * FROM q WHERE trade_date >= ? AND prev_close IS NOT NULL
    """, conn, params=(freeze_date,))
    if q.empty:
        return
    gap = q["prev_close"] - q["ldcp"]
    bad = q[(gap.abs() > tick) & ~q.apply(lambda r: (r.base_symbol, r.trade_date) in have, axis=1)]
    if not bad.empty:
        raise CorporateActionMissingError(
            f"unaccounted gaps: {bad[['base_symbol','trade_date']].values.tolist()}"
        )


def load_market_panel(conn: sqlite3.Connection, div_wht: float = 0.15) -> MarketPanel:
    """
    Constructs MarketPanel with Total Return adj_open using build_adj_factor.
    Eliminates mechanical negative dividend yield bias against high-dividend names (Q5 banks).
    Limits: 10% flat circuit bands (+/-10% or +/-Rs 1.00 min step).
    """
    verify_corporate_actions_completeness(conn)
    quotes = pd.read_sql_query(
        "SELECT trade_date AS date, base_symbol AS symbol, open, close, volume, ldcp "
        "FROM daily_quotes WHERE is_final=1 AND open > 0", conn
    )
    dup = quotes.duplicated(subset=["date", "symbol"], keep=False)
    if dup.any():
        bad = quotes[dup][["date", "symbol"]].head(10).to_dict(orient="records")
        raise AssertionError(f"Duplicate (date, symbol) detected in quotes panel: {bad}")

    quotes["upper_limit"] = quotes["ldcp"] + np.maximum(0.10 * quotes["ldcp"], 1.00)
    quotes["lower_limit"] = np.maximum(quotes["ldcp"] - np.maximum(0.10 * quotes["ldcp"], 1.00), 0.01)

    events_raw = pd.read_sql_query(
        "SELECT base_symbol AS symbol, ex_date, action_type, amount, ratio FROM corporate_actions", conn
    )
    events_list = []
    for _, r in events_raw.iterrows():
        act = str(r["action_type"]).strip().lower()
        if act == "split":
            events_list.append({"symbol": r["symbol"], "ex_date": r["ex_date"], "kind": "split", "value": float(r["ratio"])})
        elif act == "bonus":
            events_list.append({"symbol": r["symbol"], "ex_date": r["ex_date"], "kind": "bonus", "value": float(r["ratio"])})
        elif act == "cash":
            events_list.append({"symbol": r["symbol"], "ex_date": r["ex_date"], "kind": "cash", "value": float(r["amount"])})
    events = pd.DataFrame(events_list) if events_list else pd.DataFrame(columns=["symbol", "ex_date", "kind", "value"])

    adj = build_adj_factor(quotes[["date", "symbol", "close"]], events, div_wht=div_wht)
    quotes["adj_factor"] = adj["adj_factor"]
    return MarketPanel.from_long(quotes)





@dataclass

class _Pos:

    symbol: str

    col: int

    value: float

    last_px: float

    cost_basis: float

    entry_idx: int

    exit_due: int

    trapped: int = 0





@dataclass

class RunResult:

    nav: pd.Series

    returns: pd.Series

    invested: pd.Series

    n_positions: pd.Series

    n_trapped: pd.Series

    trades: pd.DataFrame

    fills: pd.DataFrame

    valid_from: str | None



    def stat_returns(self) -> pd.Series:

        return self.returns.loc[self.valid_from:] if self.valid_from else self.returns.iloc[0:0]





class JTPortfolio:

    def __init__(self, panel: MarketPanel, friction: FrictionModel = FrictionModel(),

                 K: int = 10, lock_tol: float = 0.005, rf_daily: float = 0.0):

        self.p, self.f, self.K, self.tol, self.rf = panel, friction, K, lock_tol, rf_daily



    def run(self, schedule: dict[str, list[str]]) -> RunResult:

        P, K, S = self.p, self.K, len(self.p.sessions)

        cols = {s: j for j, s in enumerate(P.adj_open.columns)}

        A = P.adj_open.to_numpy(float)

        O = P.raw_open.to_numpy(float)

        U = P.upper.to_numpy(float)

        Lo = P.lower.to_numpy(float)

        cash = np.full(K, 1.0 / K)

        sleeves: list[list[_Pos]] = [[] for _ in range(K)]

        nav, inv, npos, ntrap = (np.zeros(S) for _ in range(4))

        trades, fills = [], []

        first_entry = None



        for t in range(S):

            date = P.sessions[t]

            # 1. mark-to-market open(t-1) -> open(t)

            if t > 0:

                cash *= (1.0 + self.rf)

                for sl in sleeves:

                    for p in sl:

                        px = A[t, p.col]

                        if np.isfinite(px) and px > 0:

                            p.value *= px / p.last_px

                            p.last_px = px

            # 2. scheduled / trapped exits

            for k in range(K):

                keep = []

                for p in sleeves[k]:

                    if p.exit_due > t:

                        keep.append(p)

                        continue

                    o, lo = O[t, p.col], Lo[t, p.col]

                    tradable = (np.isfinite(o) and o > 0 and np.isfinite(lo)

                                and not is_lower_locked(o, lo, self.tol))

                    if tradable:

                        proceeds = p.value * (1.0 - self.f.one_side(o))

                        cash[k] += proceeds

                        trades.append({"symbol": p.symbol, "entry_date": P.sessions[p.entry_idx],

                                       "exit_date": date, "sessions_held": t - p.entry_idx,

                                       "trapped_sessions": p.trapped,

                                       "net_return": proceeds / p.cost_basis - 1.0})

                    else:

                        p.trapped += 1

                        keep.append(p)

                sleeves[k] = keep

            # 3. entry into sleeve t mod K

            names = schedule.get(date)

            k = t % K

            if names:

                ok, n_upper, n_missing = [], 0, 0

                for s in dict.fromkeys(names):

                    j = cols.get(s)

                    if j is None:

                        n_missing += 1

                        continue

                    o, a, up = O[t, j], A[t, j], U[t, j]

                    if not (np.isfinite(o) and o > 0 and np.isfinite(a) and a > 0 and np.isfinite(up)):

                        n_missing += 1

                        continue

                    if is_upper_locked(o, up, self.tol):

                        n_upper += 1

                        continue

                    ok.append((s, j, o, a))

                if ok and cash[k] > 0:

                    w = cash[k] / len(ok)

                    for s, j, o, a in ok:

                        c = self.f.one_side(o)

                        sleeves[k].append(_Pos(s, j, w / (1.0 + c), a, w, t, t + K))

                    cash[k] = 0.0

                    if first_entry is None:

                        first_entry = t

                fills.append({"date": date, "sleeve": k, "signals": len(set(names)),

                              "filled": len(ok), "upper_locked": n_upper, "untradeable": n_missing})

            # 4. record

            pv = sum(p.value for sl in sleeves for p in sl)

            nav[t] = cash.sum() + pv

            inv[t] = pv / nav[t]

            npos[t] = sum(len(sl) for sl in sleeves)

            ntrap[t] = sum(1 for sl in sleeves for p in sl if p.trapped > 0)



        idx = pd.Index(P.sessions, name="date")

        nav_s = pd.Series(nav, idx)

        vf = None

        if first_entry is not None and first_entry + K - 1 < S:

            vf = P.sessions[first_entry + K - 1]

        return RunResult(nav_s, nav_s.pct_change().fillna(0.0), pd.Series(inv, idx),

                         pd.Series(npos, idx), pd.Series(ntrap, idx),

                         pd.DataFrame(trades), pd.DataFrame(fills), vf)





def apply_annual_cgt(nav: pd.Series, rate: float = 0.15, carryforward_years: int = 3) -> pd.DataFrame:

    """Mark-to-market approximation of CGT on NET tax-year gains (Pakistan tax year Jul–Jun),

    loss carry-forward. Realized-basis CGT differs in timing; verify rules with a tax advisor."""

    nav = nav.dropna().astype(float)

    dates = pd.to_datetime(nav.index)

    fy = np.where(dates.month >= 7, dates.year + 1, dates.year)

    out = nav.copy()

    scale, prev_end = 1.0, float(nav.iloc[0])

    losses: list[tuple[int, float]] = []

    rows = []

    for y in sorted(set(fy)):

        mask = fy == y

        seg = nav[mask] * scale

        end = float(seg.iloc[-1])

        gain = end - prev_end

        losses = [(ly, a) for ly, a in losses if y - ly <= carryforward_years]

        tax, taxable = 0.0, 0.0

        if gain > 0:

            taxable, rem = gain, []

            for ly, a in losses:

                use = min(a, taxable)

                taxable -= use

                if a - use > 1e-15:

                    rem.append((ly, a - use))

            losses, tax = rem, rate * taxable

        elif gain < 0:

            losses.append((y, -gain))

        seg = seg.copy()

        seg.iloc[-1] = end - tax

        out[mask] = seg.to_numpy()

        if tax > 0:

            scale *= (end - tax) / end

        prev_end = end - tax

        rows.append({"tax_year": int(y), "gain": gain, "taxable": taxable, "tax": tax})

    log.info("CGT schedule:\n%s", pd.DataFrame(rows))

    return pd.DataFrame({"nav_pre_tax": nav, "nav_after_tax": out})





def _self_test() -> None:

    sessions = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-01-05", periods=60)]

    syms = ["AAA", "BBB", "CCC"]

    flat = pd.DataFrame(100.0, index=sessions, columns=syms)

    zero = FrictionModel(0.0, 0.0, 0.0, 0.0)

    # (a) NAV conservation: flat prices, zero cost

    pan = MarketPanel(sessions, flat.copy(), flat.copy(), flat * 1.10, flat * 0.90)

    r = JTPortfolio(pan, zero).run({d: syms for d in sessions})

    assert np.allclose(r.nav.to_numpy(), 1.0), "NAV not conserved"

    # (b) lower-lock trap: BBB opens at lower limit sessions 12..15

    op = flat.copy()

    op.iloc[12:16, 1] = 90.0

    pan2 = MarketPanel(sessions, op.copy(), op.copy(), flat * 1.10, flat * 0.90)

    r2 = JTPortfolio(pan2, zero).run({sessions[2]: syms})

    held = r2.trades.set_index("symbol")["sessions_held"]

    assert held["BBB"] == 14 and held["AAA"] == 10, held

    # (c) upper-lock zero-fill

    op3 = flat.copy()

    op3.iloc[2, 2] = 109.8

    pan3 = MarketPanel(sessions, op3.copy(), op3.copy(), flat * 1.10, flat * 0.90)

    r3 = JTPortfolio(pan3, zero).run({sessions[2]: syms})

    assert r3.fills.iloc[0]["upper_locked"] == 1 and "CCC" not in set(r3.trades["symbol"])

    # (d) CGT: loss year then gain year

    idx = ["2025-12-31", "2026-06-30", "2026-12-31", "2027-06-30"]

    t = apply_annual_cgt(pd.Series([1.0, 0.9, 1.0, 1.2], idx))

    assert abs(t["nav_after_tax"].iloc[-1] - (1.2 - 0.15 * (0.3 - 0.1))) < 1e-12

    print("jegadeesh_titman_portfolio self-test: OK")





if __name__ == "__main__":

    _self_test()