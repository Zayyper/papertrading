"""Configuration for the Hyperliquid niche-trader screener.

All parameters live in one dataclass so a run is fully described by one
config.toml plus the run date. Every number here is a decision, not a fact:
tune them, but write down why.
"""
from __future__ import annotations

import dataclasses
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Config:
    # ---- data / IO -------------------------------------------------------
    data_dir: str = "data"
    out_dir: str = "out"
    api_url: str = "https://api.hyperliquid.xyz/info"
    leaderboard_url: str = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
    request_weight_per_minute: int = 1000      # HL hard limit is 1200/min/IP; stay under
    http_timeout_s: float = 30.0
    random_seed: int = 42

    # ---- pool definition (who we even look at) ---------------------------
    equity_min_usd: float = 1_000.0
    equity_max_usd: float = 50_000.0
    min_alltime_volume_usd: float = 250_000.0  # proves the account actually trades
    pool_max_accounts: int = 300               # cap on fills downloads (weight-heavy)
    pool_selection: str = "random"             # "random" (clean) or "month_roi" (biased, quick look)

    # ---- windows (days back from run date) -------------------------------
    lookback_days: int = 180                   # total history considered
    oos_days: int = 60                         # last N days held out for the walk-forward check
    min_account_age_days: int = 90

    # ---- copy mechanics assumptions ---------------------------------------
    latency_s: float = 3.0                     # detection-to-fill, WebSocket + market order from EU
    hold_multiple_of_latency: float = 60.0     # median hold must be >= this x latency
    latency_z: float = 1.0                     # adverse move = z * sigma_1h * sqrt(latency/3600)
    impact_coeff: float = 1.0                  # sqrt-impact: sigma_day * sqrt(Q / V_day) * coeff
    half_spread_bps: dict[str, float] = field(
        default_factory=lambda: {"major": 1.0, "mid": 3.0, "thin": 10.0}
    )
    taker_fee_bps: float = 4.5                 # HL base-tier perp taker fee (0.045%)
    builder_fee_bps: float = 0.0               # 0 when trading direct; set >0 to model a platform cut
    follower_equity_usd: float = 1_000.0
    follower_max_leverage: float = 10.0        # hard cap on follower notional / equity
    apply_funding: bool = True

    # ---- liquidity tiers (dayNtlVlm thresholds, USD) ----------------------
    tier_major_min_vlm: float = 200_000_000.0
    tier_mid_min_vlm: float = 10_000_000.0     # below this = "thin"
    max_thin_notional_share: float = 0.20

    # ---- hard filters (applied on in-sample metrics) ----------------------
    min_trades: int = 50
    min_active_days: int = 20
    max_days_since_last_trade: int = 14
    max_median_leverage: float = 25.0
    max_top2_share: float = 0.40              # share of gross profit from the 2 best trades
    min_profit_factor: float = 1.3
    max_drawdown: float = 0.30                # on in-sample equity curve, fraction
    min_positive_buckets_share: float = 0.5   # share of 2-week buckets with pnl > 0
    max_liquidations: int = 0
    min_follower_roi_is: float = 0.0          # follower-realized return in-sample must be > 0

    # ---- ranking / shortlist ------------------------------------------------
    shortlist_size: int = 15
    min_shortlist_leaders: int = 3            # verdict needs at least this many survivors
    naive_top_n: int = 5                      # "leaderboard copier" benchmark
    dd_floor: float = 0.05                    # floor on DD in calmar-like ratio

    # ------------------------------------------------------------------------
    @property
    def min_hold_minutes(self) -> float:
        return self.latency_s * self.hold_multiple_of_latency / 60.0

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def load(cls, path: str | Path | None) -> "Config":
        cfg = cls()
        if path is None:
            return cfg
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"config not found: {p}")
        with open(p, "rb") as fh:
            raw = tomllib.load(fh)
        known = {f.name for f in dataclasses.fields(cls)}
        for section in raw.values() if all(isinstance(v, dict) for v in raw.values()) else [raw]:
            for k, v in section.items():
                if k not in known:
                    raise KeyError(f"unknown config key: {k}")
                setattr(cfg, k, v)
        return cfg
