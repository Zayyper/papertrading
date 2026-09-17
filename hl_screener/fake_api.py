"""Synthetic Hyperliquid data source for tests and the `demo` command (no network).

It fabricates a small universe of traders with known personalities so the
pipeline's filters can be checked against ground truth: a good swing trader
should survive, a scalper, a lucky one-hit account, a thin-coin specialist and
a liquidated degen should not.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Any

DAY = 86_400_000
HOUR = 3_600_000

COINS = {
    # coin: (base price, day volume, hourly sigma)
    "BTC": (60_000.0, 2.0e9, 0.004),
    "ETH": (3_000.0, 8.0e8, 0.006),
    "SOL": (150.0, 5.0e7, 0.010),
    "XYZ": (1.0, 2.0e6, 0.030),
}


@dataclass
class Persona:
    name: str
    equity: float
    age_days: int
    trades_per_day: float
    hold_min: float
    edge: float               # mean price return captured per trade
    noise: float              # std of price return per trade
    leverage: float
    coins: tuple[str, ...]
    last_trade_days_before_end: float = 0.5
    liquidate_one: bool = False
    one_big_winner: float = 0.0   # if >0, inject one trade with this price return


PERSONAS = [
    Persona("good_swing", 12_000, 320, 1.0, 360, 0.0040, 0.010, 2.0, ("BTC", "ETH")),
    Persona("good_swing2", 8_000, 200, 1.5, 240, 0.0035, 0.009, 2.5, ("BTC", "ETH", "SOL")),
    Persona("scalper", 15_000, 400, 25.0, 1.0, 0.0012, 0.0015, 8.0, ("BTC",)),
    Persona("lucky", 5_000, 150, 0.7, 180, 0.0005, 0.006, 3.0, ("ETH",), one_big_winner=1.2),
    Persona("thin_coin", 9_000, 250, 1.2, 300, 0.004, 0.02, 2.0, ("XYZ",)),
    Persona("young", 7_000, 30, 2.0, 200, 0.004, 0.01, 3.0, ("BTC",)),
    Persona("whale", 500_000, 500, 1.0, 300, 0.003, 0.01, 2.0, ("BTC",)),
    Persona("degen", 4_000, 180, 3.0, 90, 0.001, 0.02, 40.0, ("SOL",), liquidate_one=True),
    Persona("inactive", 10_000, 300, 1.0, 300, 0.004, 0.01, 3.0, ("BTC",), last_trade_days_before_end=40),
] + [Persona(f"noise{i}", 3_000 + 500 * i, 120 + 20 * i, 1.0 + 0.2 * i, 120 + 30 * i, 0.0, 0.012, 3.0, ("BTC", "ETH", "SOL")) for i in range(10)]


def addr_for(name: str) -> str:
    h = abs(hash(name)) % (16 ** 38)
    return "0x" + f"{h:038x}"[:38].rjust(40, "0")


class FakeAPI:
    def __init__(self, end_ms: int, seed: int = 7, personas: list[Persona] | None = None):
        self.end = end_ms
        self.rng = random.Random(seed)
        self.personas = personas or PERSONAS
        self.by_addr: dict[str, dict[str, Any]] = {}
        self.calls: dict[str, int] = {}
        for p in self.personas:
            self.by_addr[addr_for(p.name)] = self._make_trader(p)

    # ---- generation ----------------------------------------------------------
    def _make_trader(self, p: Persona) -> dict[str, Any]:
        rng = random.Random(f"{p.name}-{self.rng.random()}")
        start = self.end - p.age_days * DAY
        last = self.end - int(p.last_trade_days_before_end * DAY)
        n = int(p.trades_per_day * (last - start) / DAY)
        fills: list[dict[str, Any]] = []
        tid = 1
        cum_pnl = 0.0
        pnl_curve = [(start, 0.0)]
        # place the special trades in the first third of history so they land in-sample
        big_idx = n // 3 if p.one_big_winner > 0 and n > 0 else -1
        liq_idx = n // 3 + 1 if p.liquidate_one and n > 0 else -1
        # sequential, non-overlapping trades (startPosition must be consistent per coin)
        mean_gap = max((last - start) / max(n, 1) - p.hold_min * 60_000, 60_000)
        times: list[float] = []
        t_cur = float(start)
        for _ in range(n):
            t_cur += rng.expovariate(1.0 / mean_gap)
            if t_cur + p.hold_min * 60_000 * 1.4 > last:
                break
            times.append(t_cur)
            t_cur += p.hold_min * 60_000 * 1.4
        for i, t_open in enumerate(times):
            coin = rng.choice(p.coins)
            base, _vlm, _sig = COINS[coin]
            entry = base * (1 + rng.gauss(0, 0.05))
            d = rng.choice((1, -1))
            ret = rng.gauss(p.edge, p.noise)
            if i == big_idx:
                ret = p.one_big_winner
            if i == liq_idx:
                ret = -1.0 / p.leverage * 0.95
            exit_ = entry * (1 + d * ret)
            size = p.leverage * p.equity / entry
            t_close = int(t_open + p.hold_min * 60_000 * rng.uniform(0.6, 1.4))
            fee = size * entry * 4.5e-4
            fills.append({"coin": coin, "px": f"{entry:.6f}", "sz": f"{size:.6f}", "side": "B" if d > 0 else "A",
                          "time": int(t_open), "startPosition": "0.0", "dir": "Open Long" if d > 0 else "Open Short",
                          "closedPnl": "0.0", "fee": f"{fee:.6f}", "tid": tid, "hash": f"0x{tid:x}"})
            tid += 1
            pnl = d * (exit_ - entry) * size
            f2 = {"coin": coin, "px": f"{exit_:.6f}", "sz": f"{size:.6f}", "side": "A" if d > 0 else "B",
                  "time": t_close, "startPosition": f"{d * size:.6f}", "dir": "Close Long" if d > 0 else "Close Short",
                  "closedPnl": f"{pnl:.6f}", "fee": f"{fee:.6f}", "tid": tid, "hash": f"0x{tid:x}"}
            if i == liq_idx:
                f2["liquidation"] = {"liquidatedUser": addr_for(p.name), "markPx": f"{exit_:.4f}", "method": "market"}
            fills.append(f2)
            tid += 1
            cum_pnl += pnl - 2 * fee
            pnl_curve.append((t_close, cum_pnl))
        fills.sort(key=lambda f: f["time"])
        vlm = sum(float(f["px"]) * float(f["sz"]) for f in fills)
        return {
            "persona": p,
            "fills": fills,
            # small accounts typically sweep profits out; keep equity flat so the leaderboard band is stable
            "equity_curve": [(start, p.equity), (self.end, p.equity)],
            "pnl_curve": pnl_curve,
            "vlm": vlm,
        }

    # ---- InfoAPI protocol -----------------------------------------------------
    def _count(self, k: str) -> None:
        self.calls[k] = self.calls.get(k, 0) + 1

    def leaderboard(self) -> list[dict[str, Any]]:
        self._count("leaderboard")
        rows = []
        for addr, d in self.by_addr.items():
            p: Persona = d["persona"]
            rows.append({
                "address": addr, "account_value": d["equity_curve"][-1][1], "display_name": p.name,
                "perf": {
                    "allTime": {"pnl": d["pnl_curve"][-1][1], "roi": d["pnl_curve"][-1][1] / p.equity, "vlm": d["vlm"]},
                    "month": {"pnl": 0.0, "roi": 0.0, "vlm": d["vlm"] / 6},
                },
            })
        return rows

    def portfolio(self, user: str) -> dict[str, Any]:
        self._count("portfolio")
        d = self.by_addr[user]
        return {"allTime": {"account_value": d["equity_curve"], "pnl": d["pnl_curve"], "vlm": d["vlm"]}}

    def fills_cached(self, user: str, start_ms: int, end_ms: int) -> bool:
        return user in self.by_addr  # synthetic data is always "on disk"

    def user_fills(self, user: str, start_ms: int, end_ms: int) -> tuple[list[dict[str, Any]], bool]:
        self._count("user_fills")
        fills = [f for f in self.by_addr[user]["fills"] if start_ms <= f["time"] <= end_ms]
        if len(fills) > 10_000:
            return fills[-10_000:], True
        return fills, False

    def meta_and_ctxs(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        self._count("meta")
        meta = {"universe": [{"name": c, "szDecimals": 3, "maxLeverage": 50} for c in COINS]}
        ctxs = [{"dayNtlVlm": str(v[1]), "funding": "0.00001", "openInterest": "1", "markPx": str(v[0])} for v in COINS.values()]
        return meta, ctxs

    def candles(self, coin: str, interval: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        self._count("candles")
        base, _vlm, sig = COINS[coin]
        step = HOUR if interval == "1h" else DAY
        sig_step = sig if interval == "1h" else sig * math.sqrt(24)
        rng = random.Random(f"{coin}-{interval}-{start_ms}")
        n = min(5000, int((end_ms - start_ms) // step))
        t0 = end_ms - n * step
        px = base
        out = []
        for i in range(n):
            px *= math.exp(rng.gauss(0.0, sig_step))
            out.append({"t": t0 + i * step, "T": t0 + (i + 1) * step - 1, "s": coin, "i": interval,
                        "o": str(px), "c": str(px), "h": str(px), "l": str(px), "v": "1", "n": 1})
        return out

    def funding_history(self, coin: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        self._count("funding")
        t = (start_ms // HOUR + 1) * HOUR
        out = []
        while t <= end_ms:
            out.append({"coin": coin, "fundingRate": "0.00001", "premium": "0", "time": t})
            t += HOUR
        return out
