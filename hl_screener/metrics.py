"""Per-trader metrics over a time window, computed from round trips + equity curve."""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .fills import RoundTrip

DAY_MS = 86_400_000


class EquityCurve:
    """Step-interpolated account value from the portfolio endpoint (allTime)."""

    def __init__(self, points: list[tuple[int, float]]):
        pts = sorted((int(t), float(v)) for t, v in points if v == v)  # drop NaN
        self.t = [p[0] for p in pts]
        self.v = [p[1] for p in pts]

    def __len__(self) -> int:
        return len(self.t)

    @property
    def first_time(self) -> int | None:
        return self.t[0] if self.t else None

    def at(self, t: int) -> float:
        if not self.t:
            return float("nan")
        i = bisect.bisect_right(self.t, t) - 1
        if i < 0:
            return self.v[0]
        return self.v[i]

    def mean_between(self, t0: int, t1: int) -> float:
        if not self.t:
            return float("nan")
        i0 = max(bisect.bisect_left(self.t, t0), 0)
        i1 = bisect.bisect_right(self.t, t1)
        seg = self.v[i0:i1]
        if not seg:
            return self.at(t0)
        return float(np.mean(seg))

    def max_drawdown_between(self, t0: int, t1: int) -> float:
        """Max drawdown of the value curve between t0 and t1, as a fraction of the running peak."""
        if not self.t:
            return float("nan")
        i0 = max(bisect.bisect_left(self.t, t0), 0)
        i1 = bisect.bisect_right(self.t, t1)
        seg = self.v[i0:i1]
        if len(seg) < 2:
            return 0.0
        peak = seg[0]
        mdd = 0.0
        for x in seg:
            peak = max(peak, x)
            if peak > 0:
                mdd = max(mdd, (peak - x) / peak)
        return mdd


@dataclass
class TraderMetrics:
    address: str
    window_start: int
    window_end: int
    n_trades: int = 0
    n_incomplete: int = 0
    active_days: int = 0
    days_since_last_trade: float = float("nan")
    account_age_days: float = float("nan")
    equity_start: float = float("nan")
    equity_mean: float = float("nan")
    net_pnl: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    profit_factor: float = float("nan")
    win_rate: float = float("nan")
    roi_trades: float = float("nan")          # net pnl from round trips / equity at window start
    roi_portfolio: float = float("nan")       # pnlHistory delta / equity at window start
    max_dd_trades: float = float("nan")       # cumulative round-trip pnl drawdown / equity_start
    max_dd_portfolio: float = float("nan")    # from the cumulative-PnL curve (transfer-neutral) / equity_start
    top2_share: float = float("nan")
    median_leverage: float = float("nan")
    max_leverage: float = float("nan")
    sizing_cv: float = float("nan")
    median_hold_min: float = float("nan")
    p25_hold_min: float = float("nan")
    thin_share: float = float("nan")
    major_share: float = float("nan")
    n_coins: int = 0
    top_coin_share: float = float("nan")
    liquidations: int = 0
    n_buckets: int = 0
    positive_bucket_share: float = float("nan")
    avg_notional: float = float("nan")
    fills_truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def compute_metrics(
    address: str,
    trips: list[RoundTrip],
    equity: EquityCurve,
    pnl_curve: list[tuple[int, float]],
    tiers: dict[str, str],
    window_start: int,
    window_end: int,
    fills_truncated: bool = False,
    bucket_days: int = 14,
    activity_ref_ms: int | None = None,
) -> TraderMetrics:
    m = TraderMetrics(address=address, window_start=window_start, window_end=window_end, fills_truncated=fills_truncated)
    in_win = [t for t in trips if window_start <= t.close_time <= window_end]
    complete = [t for t in in_win if t.complete]
    m.n_incomplete = len(in_win) - len(complete)
    m.n_trades = len(complete)

    age_ref = activity_ref_ms if activity_ref_ms is not None else window_end
    if equity.first_time is not None:
        m.account_age_days = (age_ref - equity.first_time) / DAY_MS
    m.equity_start = equity.at(window_start)
    m.equity_mean = equity.mean_between(window_start, window_end)
    # Drawdown from the PnL curve, not the account-value curve: a profit sweep (withdrawal)
    # is not a drawdown, and small accounts sweep often.
    m.max_dd_portfolio = _pnl_curve_dd(pnl_curve, window_start, window_end, m.equity_start)
    m.roi_portfolio = _portfolio_roi(pnl_curve, window_start, window_end, m.equity_start)

    if not complete:
        return m

    pnls = np.array([t.net_pnl for t in complete])
    m.net_pnl = float(pnls.sum())
    m.gross_profit = float(pnls[pnls > 0].sum())
    m.gross_loss = float(-pnls[pnls < 0].sum())
    m.profit_factor = (m.gross_profit / m.gross_loss) if m.gross_loss > 0 else (math.inf if m.gross_profit > 0 else float("nan"))
    m.win_rate = float((pnls > 0).mean())
    if m.gross_profit > 0:
        top2 = np.sort(pnls[pnls > 0])[-2:]
        m.top2_share = float(top2.sum() / m.gross_profit)

    eq0 = m.equity_start if m.equity_start == m.equity_start and m.equity_start > 0 else m.equity_mean
    if eq0 == eq0 and eq0 > 0:
        m.roi_trades = m.net_pnl / eq0
        cum = np.cumsum(pnls)
        peak = np.maximum.accumulate(np.concatenate([[0.0], cum]))[1:]
        m.max_dd_trades = float(np.max(peak - cum) / eq0) if len(cum) else 0.0

    # leverage / sizing relative to equity at open
    lev = []
    for t in complete:
        e = equity.at(t.open_time)
        if e == e and e > 0 and t.max_notional > 0:
            lev.append(t.max_notional / e)
    if lev:
        lev_a = np.array(lev)
        m.median_leverage = float(np.median(lev_a))
        m.max_leverage = float(lev_a.max())
        m.sizing_cv = float(lev_a.std() / lev_a.mean()) if lev_a.mean() > 0 else float("nan")

    holds = np.array([t.hold_minutes for t in complete])
    m.median_hold_min = float(np.median(holds))
    m.p25_hold_min = float(np.percentile(holds, 25))

    notionals = np.array([t.max_notional for t in complete])
    tot = notionals.sum()
    m.avg_notional = float(notionals.mean())
    if tot > 0:
        m.thin_share = float(sum(n for n, t in zip(notionals, complete) if tiers.get(t.coin, "thin") == "thin") / tot)
        m.major_share = float(sum(n for n, t in zip(notionals, complete) if tiers.get(t.coin) == "major") / tot)
        by_coin: dict[str, float] = {}
        for n, t in zip(notionals, complete):
            by_coin[t.coin] = by_coin.get(t.coin, 0.0) + n
        m.n_coins = len(by_coin)
        m.top_coin_share = float(max(by_coin.values()) / tot)

    m.liquidations = sum(1 for t in complete if t.liquidated)
    days = {t.close_time // DAY_MS for t in complete}
    m.active_days = len(days)
    # "still active" is judged against the run date when given (we want traders trading *now*),
    # otherwise against the window end
    ref = activity_ref_ms if activity_ref_ms is not None else window_end
    last_any = max((t.close_time for t in trips if t.close_time <= ref), default=None)
    m.days_since_last_trade = (ref - last_any) / DAY_MS if last_any is not None else float("nan")

    # consistency in fixed buckets
    bucket_ms = bucket_days * DAY_MS
    buckets: dict[int, float] = {}
    for t in complete:
        b = (t.close_time - window_start) // bucket_ms
        buckets[b] = buckets.get(b, 0.0) + t.net_pnl
    n_b = max(1, int(math.ceil((window_end - window_start) / bucket_ms)))
    m.n_buckets = n_b
    m.positive_bucket_share = sum(1 for b in range(n_b) if buckets.get(b, 0.0) > 0) / n_b
    return m


def _portfolio_roi(pnl_curve: list[tuple[int, float]], t0: int, t1: int, eq0: float) -> float:
    if not pnl_curve or not (eq0 == eq0) or eq0 <= 0:
        return float("nan")
    pts = sorted((int(t), float(v)) for t, v in pnl_curve if v == v)
    ts = [p[0] for p in pts]
    i0 = bisect.bisect_right(ts, t0) - 1
    i1 = bisect.bisect_right(ts, t1) - 1
    if i0 < 0:
        i0 = 0
    if i1 < 0:
        return float("nan")
    return (pts[i1][1] - pts[i0][1]) / eq0


def _pnl_curve_dd(pnl_curve: list[tuple[int, float]], t0: int, t1: int, eq0: float) -> float:
    if not pnl_curve or not (eq0 == eq0) or eq0 <= 0:
        return float("nan")
    seg = [float(v) for t, v in sorted(pnl_curve) if t0 <= int(t) <= t1 and v == v]
    if len(seg) < 2:
        return 0.0
    peak = seg[0]
    mdd = 0.0
    for x in seg:
        peak = max(peak, x)
        mdd = max(mdd, peak - x)
    return mdd / eq0
