"""Pipeline: pool -> history -> in-sample screen -> out-of-sample check -> benchmarks."""
from __future__ import annotations

import dataclasses
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from .api import InfoAPI
from .config import Config
from .fills import RoundTrip, build_round_trips, parse_fills
from .liquidity import CoinStats, build_coin_stats, tiers_map
from .metrics import DAY_MS, EquityCurve, TraderMetrics, compute_metrics
from .screen import hard_filter_reasons, rank_candidates, score
from .simulate import FollowerResult, FundingBook, buy_and_hold, portfolio_of, simulate_follower

log = logging.getLogger(__name__)


@dataclass
class TraderData:
    address: str
    lb: dict[str, Any]
    equity: EquityCurve
    pnl_curve: list[tuple[int, float]]
    trips: list[RoundTrip]
    fills_truncated: bool
    effective_start: int
    n_fills: int


@dataclass
class RunResult:
    end_ms: int
    is_start: int
    is_end: int
    oos_end: int
    pool_size: int
    loaded: int
    rows: list[dict[str, Any]]
    shortlist: list[dict[str, Any]]
    benchmarks: dict[str, Any]
    coin_stats: dict[str, CoinStats]
    config: dict[str, Any]
    timings: dict[str, float] = field(default_factory=dict)
    drop_counts: dict[str, int] = field(default_factory=dict)


def select_pool(rows: list[dict[str, Any]], cfg: Config, rng: random.Random) -> list[dict[str, Any]]:
    band = []
    for r in rows:
        av = r["account_value"]
        vlm = r["perf"].get("allTime", {}).get("vlm", float("nan"))
        if not (av == av) or av < cfg.equity_min_usd or av > cfg.equity_max_usd:
            continue
        if not (vlm == vlm) or vlm < cfg.min_alltime_volume_usd:
            continue
        if not r["address"].startswith("0x"):
            continue
        band.append(r)
    log.info("leaderboard rows=%d, in equity band with volume=%d", len(rows), len(band))
    if cfg.pool_selection == "month_roi":
        band.sort(key=lambda r: r["perf"].get("month", {}).get("roi", -1e9), reverse=True)
    else:
        rng.shuffle(band)
    return band[: cfg.pool_max_accounts]


def load_trader(api: InfoAPI, row: dict[str, Any], start_ms: int, end_ms: int, cfg: Config) -> TraderData | None:
    addr = row["address"]
    try:
        pf = api.portfolio(addr)
    except Exception as e:  # noqa: BLE001
        log.warning("portfolio failed %s: %s", addr, e)
        return None
    all_time = pf.get("allTime") or pf.get("perpAllTime") or {}
    equity = EquityCurve(all_time.get("account_value", []))
    pnl_curve = all_time.get("pnl", [])
    if equity.first_time is None:
        return None
    age_days = (end_ms - equity.first_time) / DAY_MS
    if age_days < cfg.min_account_age_days:
        return None  # cheap early exit: no fills download for young accounts
    try:
        raw, truncated = api.user_fills(addr, start_ms, end_ms)
    except Exception as e:  # noqa: BLE001
        log.warning("fills failed %s: %s", addr, e)
        return None
    fills = parse_fills(raw, address=addr)
    if not fills:
        return None
    eff_start = max(start_ms, fills[0].time) if truncated else start_ms
    trips, _open = build_round_trips(fills)
    return TraderData(address=addr, lb=row, equity=equity, pnl_curve=pnl_curve, trips=trips,
                      fills_truncated=truncated, effective_start=eff_start, n_fills=len(fills))


def run(api: InfoAPI, cfg: Config, end_ms: int, progress: Callable[[str], None] | None = None,
        pool_addresses: list[str] | None = None, cached_only: bool = False) -> RunResult:
    """Full pipeline. `pool_addresses` pins the pool to an explicit list (e.g. the traders of a
    previous run, to re-screen them from cache after a code or config change) instead of
    sampling the leaderboard; leaderboard rows are still used for equity/name when present.
    `cached_only` takes every in-band account whose fills for this run date are already on
    disk and nothing else: turns an interrupted download into a result without new requests."""
    say = progress or (lambda s: log.info(s))
    t0 = time.time()
    rng = random.Random(cfg.random_seed)
    timings: dict[str, float] = {}

    is_start = end_ms - cfg.lookback_days * DAY_MS
    is_end = end_ms - cfg.oos_days * DAY_MS
    oos_end = end_ms

    say("fetching leaderboard")
    lb = api.leaderboard()
    if pool_addresses is not None:
        by_addr = {r["address"]: r for r in lb}
        pool = [by_addr.get(a.lower(), {"address": a.lower(), "account_value": float("nan"), "display_name": None, "perf": {}})
                for a in dict.fromkeys(a.lower() for a in pool_addresses if a.lower().startswith("0x"))]
        say(f"pool pinned to {len(pool)} given addresses ({sum(1 for r in pool if r['address'] in by_addr)} still on the leaderboard)")
    elif cached_only:
        whole_band = dataclasses.replace(cfg, pool_max_accounts=len(lb) + 1)
        band = select_pool(lb, whole_band, rng)
        pool = [r for r in band if api.fills_cached(r["address"], is_start, end_ms)]
        say(f"pool: {len(pool)} of {len(band)} in-band accounts already downloaded for run date {time.strftime('%Y-%m-%d', time.gmtime(end_ms / 1000))}")
        if not pool:
            say("nothing cached for this run date: pass --end <date of the interrupted run> (cache keys include the date)")
    else:
        pool = select_pool(lb, cfg, rng)
        say(f"pool: {len(pool)} accounts (equity {cfg.equity_min_usd:,.0f}-{cfg.equity_max_usd:,.0f} USD)")
    timings["leaderboard"] = time.time() - t0

    traders: list[TraderData] = []
    t1 = time.time()
    for i, row in enumerate(pool, 1):
        td = load_trader(api, row, is_start, end_ms, cfg)
        if td is not None:
            traders.append(td)
        if i % 10 == 0 or i == len(pool):
            say(f"history: {i}/{len(pool)} fetched, {len(traders)} usable")
        if i == 10 and not traders:
            say("WARNING: none of the first 10 accounts had usable history. If this keeps happening the API "
                "response shape may have changed — run `python -m hl_screener inspect <address> -v` on one of them.")
    timings["history"] = time.time() - t1

    say("coin liquidity + volatility")
    coins = {t.coin for td in traders for t in td.trips}
    coin_stats = build_coin_stats(api, cfg, coins, is_start, end_ms)
    tiers = tiers_map(coin_stats)

    # ---- in-sample metrics and a pre-simulation filter pass -----------------
    say("in-sample metrics")
    prelim: list[tuple[TraderData, TraderMetrics, TraderMetrics]] = []
    for td in traders:
        ws = max(is_start, td.effective_start)
        m_is = compute_metrics(td.address, td.trips, td.equity, td.pnl_curve, tiers, ws, is_end, td.fills_truncated, activity_ref_ms=end_ms)
        m_oos = compute_metrics(td.address, td.trips, td.equity, td.pnl_curve, tiers, is_end, oos_end, td.fills_truncated) if cfg.oos_days > 0 else m_is
        prelim.append((td, m_is, m_oos))

    # funding only for coins of traders that survive the non-simulation filters (saves API weight)
    survivors_coins: set[str] = set()
    dummy = FollowerResult(roi=1.0)  # so the follower filter passes in this pre-pass
    for td, m_is, _ in prelim:
        if not hard_filter_reasons(m_is, dummy, cfg):
            survivors_coins.update(t.coin for t in td.trips)
    funding = None
    if cfg.apply_funding:
        say(f"funding history for {len(survivors_coins)} coins")
        recs: dict[str, list[dict[str, Any]]] = {}
        for c in sorted(survivors_coins):
            if not coin_stats.get(c, CoinStats(c, 0, "thin", 0.02, False)).listed:
                continue
            try:
                recs[c] = api.funding_history(c, is_start, end_ms)
            except Exception as e:  # noqa: BLE001
                log.warning("funding failed %s: %s", c, e)
        funding = FundingBook.from_records(recs)

    # ---- simulate everyone, filter, rank --------------------------------------
    say("copy simulation")
    rows: list[dict[str, Any]] = []
    drop_counts: dict[str, int] = {}
    for td, m_is, m_oos in prelim:
        ws = max(is_start, td.effective_start)
        f_is = simulate_follower(td.trips, td.equity, coin_stats, funding, cfg, ws, is_end)
        f_oos = simulate_follower(td.trips, td.equity, coin_stats, funding, cfg, is_end, oos_end) if cfg.oos_days > 0 else f_is
        reasons = hard_filter_reasons(m_is, f_is, cfg)
        for r in reasons:
            drop_counts[r] = drop_counts.get(r, 0) + 1
        rows.append({
            "address": td.address,
            "display_name": td.lb.get("display_name"),
            "account_value": td.lb.get("account_value"),
            "lb_month_roi": td.lb["perf"].get("month", {}).get("roi"),
            "lb_alltime_pnl": td.lb["perf"].get("allTime", {}).get("pnl"),
            "n_fills": td.n_fills,
            "fills_truncated": td.fills_truncated,
            "is": m_is, "oos": m_oos,
            "is_follower": f_is.summary(), "oos_follower": f_oos.summary(),
            "_f_is": f_is, "_f_oos": f_oos,
            "reasons": reasons,
            "score": score(f_is, m_is, cfg),
        })
    shortlist = rank_candidates(rows, cfg)
    say(f"passed all filters: {sum(1 for r in rows if not r['reasons'])}; shortlist: {len(shortlist)}")

    # ---- benchmarks (all evaluated out-of-sample) ----------------------------
    bench: dict[str, Any] = {}
    bench["shortlist"] = portfolio_of({r["address"]: r["_f_oos"] for r in shortlist}, cfg)
    passed = [r for r in rows if not r["reasons"]]
    bench["all_passed"] = portfolio_of({r["address"]: r["_f_oos"] for r in passed}, cfg)
    naive_pool = [r for r in rows if r["is"].n_trades >= 10 and r["is"].roi_trades == r["is"].roi_trades]
    naive_pool.sort(key=lambda r: r["is"].roi_trades, reverse=True)
    naive = naive_pool[: cfg.naive_top_n]
    bench["naive_top_n"] = portfolio_of({r["address"]: r["_f_oos"] for r in naive}, cfg)
    bench["naive_top_n_addresses"] = [r["address"] for r in naive]
    try:
        btc = api.candles("BTC", "1d", is_end - DAY_MS, oos_end)
        bench["btc_hold"] = buy_and_hold(btc, is_end, oos_end)
        btc_is = api.candles("BTC", "1d", is_start - DAY_MS, is_end)
        bench["btc_hold_is"] = buy_and_hold(btc_is, is_start, is_end)
    except Exception as e:  # noqa: BLE001
        log.warning("BTC benchmark failed: %s", e)
        bench["btc_hold"] = {"roi": float("nan"), "max_dd": float("nan")}
        bench["btc_hold_is"] = {"roi": float("nan"), "max_dd": float("nan")}

    # persistence: does the in-sample score say anything about out-of-sample follower ROI?
    xs = [r["score"] for r in passed if r["oos_follower"]["roi"] == r["oos_follower"]["roi"]]
    ys = [r["oos_follower"]["roi"] for r in passed if r["oos_follower"]["roi"] == r["oos_follower"]["roi"]]
    bench["persistence"] = {
        "n": len(xs),
        "spearman": _spearman(xs, ys) if len(xs) >= 5 else float("nan"),
        "shortlist_positive_oos_share": bench["shortlist"].get("leaders_positive_share", float("nan")),
    }
    bench["verdict"] = verdict(bench, cfg)

    timings["total"] = time.time() - t0
    return RunResult(end_ms=end_ms, is_start=is_start, is_end=is_end, oos_end=oos_end, pool_size=len(pool),
                     loaded=len(traders), rows=rows, shortlist=shortlist, benchmarks=bench,
                     coin_stats=coin_stats, config=cfg.to_dict(), timings=timings, drop_counts=drop_counts)


def verdict(bench: dict[str, Any], cfg: Config) -> dict[str, Any]:
    s = bench["shortlist"]
    n = bench["naive_top_n"]
    b = bench["btc_hold"]
    checks = {
        "shortlist_has_leaders": s.get("n_leaders", 0) >= cfg.min_shortlist_leaders,
        "beats_naive_top_n": _gt(s.get("roi"), n.get("roi")),
        "beats_btc_hold": _gt(s.get("roi"), b.get("roi")),
        "drawdown_ok": (s.get("max_dd", float("nan")) == s.get("max_dd")) and s.get("max_dd", 1.0) <= cfg.max_drawdown,
        "positive_oos": _gt(s.get("roi"), 0.0),
    }
    go = all(checks.values())
    return {"checks": checks, "proceed_to_paper_test": go}


def _gt(a: Any, b: Any) -> bool:
    try:
        return (a == a) and (b == b) and float(a) > float(b)
    except (TypeError, ValueError):
        return False


def _spearman(x: list[float], y: list[float]) -> float:
    rx = _ranks(x)
    ry = _ranks(y)
    if np.std(rx) == 0 or np.std(ry) == 0:
        return float("nan")
    return float(np.corrcoef(rx, ry)[0, 1])


def _ranks(v: list[float]) -> np.ndarray:
    a = np.array(v, dtype=float)
    order = a.argsort()
    r = np.empty(len(a))
    r[order] = np.arange(len(a))
    return r
