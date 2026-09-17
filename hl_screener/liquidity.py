"""Per-coin liquidity tier and realized volatility, used by filters and the slippage model."""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Iterable

import numpy as np

from .api import InfoAPI
from .config import Config

log = logging.getLogger(__name__)

DEFAULT_SIGMA_1H = {"major": 0.006, "mid": 0.012, "thin": 0.025}


@dataclass
class CoinStats:
    coin: str
    day_vlm: float
    tier: str
    sigma_1h: float
    listed: bool = True

    @property
    def sigma_day(self) -> float:
        return self.sigma_1h * math.sqrt(24.0)


def tier_for(day_vlm: float, cfg: Config) -> str:
    if day_vlm != day_vlm:  # NaN
        return "thin"
    if day_vlm >= cfg.tier_major_min_vlm:
        return "major"
    if day_vlm >= cfg.tier_mid_min_vlm:
        return "mid"
    return "thin"


def build_coin_stats(api: InfoAPI, cfg: Config, coins: Iterable[str], start_ms: int, end_ms: int) -> dict[str, CoinStats]:
    meta, ctxs = api.meta_and_ctxs()
    universe = meta.get("universe", [])
    vlm_by_coin: dict[str, float] = {}
    for asset, ctx in zip(universe, ctxs):
        try:
            vlm_by_coin[asset["name"]] = float(ctx.get("dayNtlVlm", "nan"))
        except (TypeError, ValueError):
            vlm_by_coin[asset["name"]] = float("nan")

    out: dict[str, CoinStats] = {}
    for coin in sorted(set(coins)):
        listed = coin in vlm_by_coin
        vlm = vlm_by_coin.get(coin, float("nan"))
        tier = tier_for(vlm, cfg)
        sigma = float("nan")
        if listed:
            try:
                candles = api.candles(coin, "1h", start_ms, end_ms)
                closes = np.array([float(c["c"]) for c in candles if float(c.get("c", 0)) > 0])
                if len(closes) >= 48:
                    r = np.diff(np.log(closes))
                    sigma = float(np.std(r))
            except Exception as e:  # noqa: BLE001 - a missing candle series must not kill the run
                log.warning("candles failed for %s: %s", coin, e)
        if not (sigma == sigma) or sigma <= 0:
            sigma = DEFAULT_SIGMA_1H[tier]
        out[coin] = CoinStats(coin=coin, day_vlm=vlm if vlm == vlm else 0.0, tier=tier, sigma_1h=sigma, listed=listed)
    return out


def tiers_map(stats: dict[str, CoinStats]) -> dict[str, str]:
    return {c: s.tier for c, s in stats.items()}
