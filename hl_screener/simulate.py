"""Follower-realized return: what a copier would have made, not what the leader made.

Per round trip the follower:
  * sizes notional = min(leader_leverage, follower_max_leverage) * follower_equity
  * enters at leader entry VWAP moved AGAINST it by penalty_bps
  * exits at leader exit VWAP moved AGAINST it by penalty_bps
  * pays taker (+ builder) fee on both legs
  * pays/receives funding for every hourly funding event during the hold
Penalty model (all in bps, adverse):
  half_spread[tier]
  + latency_z * sigma_1h * sqrt(latency_s / 3600)        (price runs before we fill)
  + impact_coeff * sigma_day * sqrt(notional / day_vlm)   (square-root impact law)
This is a model, not a measurement. The forward paper test replaces it with
real l2Book snapshots; the point here is to rank traders by copyability.
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .config import Config
from .fills import RoundTrip
from .liquidity import CoinStats
from .metrics import EquityCurve

DAY_MS = 86_400_000


@dataclass
class FundingBook:
    """coin -> sorted (time_ms, hourly_rate)."""
    data: dict[str, tuple[list[int], list[float]]] = field(default_factory=dict)

    @classmethod
    def from_records(cls, by_coin: dict[str, list[dict[str, Any]]]) -> "FundingBook":
        fb = cls()
        for coin, recs in by_coin.items():
            pts = sorted((int(r["time"]), float(r["fundingRate"])) for r in recs)
            fb.data[coin] = ([p[0] for p in pts], [p[1] for p in pts])
        return fb

    def sum_rates(self, coin: str, t0: int, t1: int) -> float:
        if coin not in self.data:
            return 0.0
        ts, rs = self.data[coin]
        i0 = bisect.bisect_right(ts, t0)
        i1 = bisect.bisect_right(ts, t1)
        return float(sum(rs[i0:i1]))


def penalty_bps(cs: CoinStats, notional: float, cfg: Config) -> float:
    hs = cfg.half_spread_bps.get(cs.tier, cfg.half_spread_bps["thin"])
    lat = cfg.latency_z * cs.sigma_1h * math.sqrt(cfg.latency_s / 3600.0) * 1e4
    if cs.day_vlm > 0:
        imp = cfg.impact_coeff * cs.sigma_day * math.sqrt(notional / cs.day_vlm) * 1e4
    else:
        imp = 50.0  # unlisted / no volume data: assume it is very thin
    return hs + lat + imp


@dataclass
class SimTrade:
    coin: str
    open_time: int
    close_time: int
    direction: int
    leader_return: float      # price return captured by the leader (fraction)
    leader_net_pnl: float
    follower_notional: float
    penalty_bps_entry: float
    penalty_bps_exit: float
    gross_pnl: float
    fees: float
    funding: float
    net_pnl: float


@dataclass
class FollowerResult:
    n_trades: int = 0
    equity_base: float = 0.0
    net_pnl: float = 0.0
    gross_pnl: float = 0.0
    fees: float = 0.0
    funding: float = 0.0
    roi: float = float("nan")
    max_dd: float = float("nan")
    leader_roi: float = float("nan")       # leader's round-trip ROI on leader equity, same trades
    copy_gap: float = float("nan")         # leader_roi - roi
    avg_penalty_bps: float = float("nan")
    trades: list[SimTrade] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d.pop("trades")
        return d


def simulate_follower(
    trips: list[RoundTrip],
    leader_equity: EquityCurve,
    coin_stats: dict[str, CoinStats],
    funding: FundingBook | None,
    cfg: Config,
    window_start: int,
    window_end: int,
) -> FollowerResult:
    res = FollowerResult(equity_base=cfg.follower_equity_usd)
    sel = [t for t in trips if t.complete and window_start <= t.close_time <= window_end]
    sel.sort(key=lambda t: t.close_time)
    if not sel:
        return res

    sim: list[SimTrade] = []
    leader_pnl = 0.0
    for t in sel:
        cs = coin_stats.get(t.coin) or CoinStats(coin=t.coin, day_vlm=0.0, tier="thin", sigma_1h=0.025, listed=False)
        le = leader_equity.at(t.open_time)
        if not (le == le) or le <= 0 or t.max_notional <= 0:
            continue
        lev = min(t.max_notional / le, cfg.follower_max_leverage)
        notional = lev * cfg.follower_equity_usd
        pen_in = penalty_bps(cs, notional, cfg)
        pen_out = pen_in  # symmetric assumption; exits are often worse, tune latency_z if you want
        entry = t.entry_vwap * (1 + t.direction * pen_in / 1e4)
        exit_ = t.exit_vwap * (1 - t.direction * pen_out / 1e4)
        gross = t.direction * (exit_ - entry) / entry * notional
        fees = (cfg.taker_fee_bps + cfg.builder_fee_bps) / 1e4 * notional * 2
        fund = 0.0
        if cfg.apply_funding and funding is not None:
            # long pays positive funding; sum of hourly rates over the hold
            fund = t.direction * funding.sum_rates(t.coin, t.open_time, t.close_time) * notional
        net = gross - fees - fund
        sim.append(SimTrade(
            coin=t.coin, open_time=t.open_time, close_time=t.close_time, direction=t.direction,
            leader_return=t.price_return, leader_net_pnl=t.net_pnl, follower_notional=notional,
            penalty_bps_entry=pen_in, penalty_bps_exit=pen_out, gross_pnl=gross, fees=fees,
            funding=fund, net_pnl=net,
        ))
        leader_pnl += t.net_pnl

    if not sim:
        return res
    pnls = np.array([s.net_pnl for s in sim])
    res.trades = sim
    res.n_trades = len(sim)
    res.net_pnl = float(pnls.sum())
    res.gross_pnl = float(sum(s.gross_pnl for s in sim))
    res.fees = float(sum(s.fees for s in sim))
    res.funding = float(sum(s.funding for s in sim))
    res.roi = res.net_pnl / cfg.follower_equity_usd
    cum = np.cumsum(pnls)
    peak = np.maximum.accumulate(np.concatenate([[0.0], cum]))[1:]
    res.max_dd = float(np.max(peak - cum) / cfg.follower_equity_usd)
    le0 = leader_equity.at(window_start)
    if le0 == le0 and le0 > 0:
        res.leader_roi = leader_pnl / le0
        res.copy_gap = res.leader_roi - res.roi
    res.avg_penalty_bps = float(np.mean([s.penalty_bps_entry for s in sim]))
    return res


def portfolio_of(results: dict[str, FollowerResult], cfg: Config) -> dict[str, Any]:
    """Equal-weight basket: each leader gets follower_equity_usd; DD on the merged pnl stream."""
    n = len(results)
    if n == 0:
        return {"n_leaders": 0, "roi": float("nan"), "max_dd": float("nan"), "net_pnl": 0.0, "n_trades": 0}
    base = n * cfg.follower_equity_usd
    stream = sorted(
        ((s.close_time, s.net_pnl) for r in results.values() for s in r.trades), key=lambda x: x[0]
    )
    if not stream:
        return {"n_leaders": n, "roi": 0.0, "max_dd": 0.0, "net_pnl": 0.0, "n_trades": 0}
    pnls = np.array([p for _, p in stream])
    cum = np.cumsum(pnls)
    peak = np.maximum.accumulate(np.concatenate([[0.0], cum]))[1:]
    return {
        "n_leaders": n,
        "roi": float(cum[-1] / base),
        "max_dd": float(np.max(peak - cum) / base),
        "net_pnl": float(cum[-1]),
        "n_trades": int(len(pnls)),
        "leaders_positive_share": float(np.mean([r.roi > 0 for r in results.values() if r.roi == r.roi])) if any(r.roi == r.roi for r in results.values()) else float("nan"),
    }


def buy_and_hold(candles_daily: list[dict[str, Any]], t0: int, t1: int) -> dict[str, Any]:
    pts = sorted((int(c["t"]), float(c["c"])) for c in candles_daily if t0 - DAY_MS <= int(c["t"]) <= t1)
    if len(pts) < 2:
        return {"roi": float("nan"), "max_dd": float("nan")}
    closes = np.array([p[1] for p in pts])
    roi = float(closes[-1] / closes[0] - 1.0)
    peak = np.maximum.accumulate(closes)
    mdd = float(np.max((peak - closes) / peak))
    return {"roi": roi, "max_dd": mdd, "start_px": float(closes[0]), "end_px": float(closes[-1])}
