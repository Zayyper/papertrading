"""Hard filters and ranking. Every filter returns a reason string so the report
can show *why* a trader was dropped — that is how you tune thresholds honestly."""
from __future__ import annotations

import math
from typing import Any

from .config import Config
from .metrics import TraderMetrics
from .simulate import FollowerResult


def hard_filter_reasons(m: TraderMetrics, f: FollowerResult, cfg: Config) -> list[str]:
    r: list[str] = []
    if m.account_age_days != m.account_age_days or m.account_age_days < cfg.min_account_age_days:
        r.append(f"age<{cfg.min_account_age_days}d")
    if m.n_trades < cfg.min_trades:
        r.append(f"trades<{cfg.min_trades}")
    if m.active_days < cfg.min_active_days:
        r.append(f"active_days<{cfg.min_active_days}")
    if m.days_since_last_trade != m.days_since_last_trade or m.days_since_last_trade > cfg.max_days_since_last_trade:
        r.append(f"inactive>{cfg.max_days_since_last_trade}d")
    if m.median_hold_min != m.median_hold_min or m.median_hold_min < cfg.min_hold_minutes:
        r.append(f"hold<{cfg.min_hold_minutes:.0f}min")
    if m.thin_share == m.thin_share and m.thin_share > cfg.max_thin_notional_share:
        r.append(f"thin_share>{cfg.max_thin_notional_share:.0%}")
    if m.median_leverage == m.median_leverage and m.median_leverage > cfg.max_median_leverage:
        r.append(f"lev>{cfg.max_median_leverage:.0f}x")
    if m.top2_share == m.top2_share and m.top2_share > cfg.max_top2_share:
        r.append(f"top2>{cfg.max_top2_share:.0%}")
    if not (m.profit_factor == m.profit_factor) or m.profit_factor < cfg.min_profit_factor:
        r.append(f"PF<{cfg.min_profit_factor}")
    dd = _nanmax(m.max_dd_trades, m.max_dd_portfolio)
    if dd != dd or dd > cfg.max_drawdown:
        r.append(f"DD>{cfg.max_drawdown:.0%}")
    if m.positive_bucket_share != m.positive_bucket_share or m.positive_bucket_share < cfg.min_positive_buckets_share:
        r.append(f"consistency<{cfg.min_positive_buckets_share:.0%}")
    if m.liquidations > cfg.max_liquidations:
        r.append(f"liquidations>{cfg.max_liquidations}")
    if f.roi != f.roi or f.roi <= cfg.min_follower_roi_is:
        r.append("follower_roi<=0")
    return r


def score(f: FollowerResult, m: TraderMetrics, cfg: Config) -> float:
    """Copy-adjusted Calmar-like ratio. Only follower-realized numbers enter."""
    if f.roi != f.roi:
        return -math.inf
    dd = f.max_dd if f.max_dd == f.max_dd else cfg.dd_floor
    return f.roi / max(dd, cfg.dd_floor)


def rank_candidates(rows: list[dict[str, Any]], cfg: Config) -> list[dict[str, Any]]:
    keep = [r for r in rows if not r["reasons"]]
    keep.sort(key=lambda r: (r["score"], r["is_follower"]["roi"]), reverse=True)
    return keep[: cfg.shortlist_size]


def _nanmax(*xs: float) -> float:
    vals = [x for x in xs if x == x]
    return max(vals) if vals else float("nan")
