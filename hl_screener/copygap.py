"""Copy test, part 2: what copying a wallet would really have made, and whether that beats picking at random.

copy_gap() replays one wallet's orders twice with the same sizes: once at the wallet's own prices and fees (the
leader leg), once as a copier would have filled them, `latency` later, walking the order book the collector
captured at that moment, paying the taker fee and the funding of its own, shifted, holding periods. The gap
between the two legs is split into what the price did meanwhile (latency), what the book cost past its mid
(slippage), fees and funding, so that every dollar of it can be checked by hand.

The rest is the testing discipline: every configuration looked at is logged (trials), the development window
stops where the holdout begins, and the holdout is judged once, by the rules frozen in copytest.toml.
"""
from __future__ import annotations

import bisect
import dataclasses
import hashlib
import json
import math
import random
import statistics
import time
from dataclasses import dataclass
from typing import Any

from .paper import plan_follow, walk_book
from .tape import GROUPS, day_ms, get_meta, is_perp, now_ms, plan_of, tolerance_ms

EPS = 1e-12
HOUR_MS = 3_600_000
MIN_ORDER_USD = 10.0           # Hyperliquid rejects smaller orders: a copier cannot follow below it


@dataclass(frozen=True)
class CopyCfg:
    latency_ms: int = 3000
    equity_usd: float = 1000.0
    max_leverage: float = 10.0
    taker_fee_bps: float = 4.5
    builder_fee_bps: float = 0.0
    funding: bool = True
    slippage_bps: float | None = None      # None: walk the captured book; a number: fill at its mid +/- this

    @classmethod
    def from_plan(cls, copy: dict[str, Any], latency_s: float, **over: Any) -> "CopyCfg":
        slip = copy.get("slippage", "book")
        cfg = cls(latency_ms=int(latency_s * 1000), equity_usd=copy["equity_usd"], max_leverage=copy["max_leverage"],
                  taker_fee_bps=copy["taker_fee_bps"], builder_fee_bps=copy["builder_fee_bps"], funding=copy["funding"],
                  slippage_bps=None if slip == "book" else float(slip))
        return dataclasses.replace(cfg, **{k: v for k, v in over.items() if v is not None})


# ---------------------------------------------------------------------------
# market data: an in-memory view (tests build it by hand) and the Postgres one
# ---------------------------------------------------------------------------
class Market:
    def __init__(self, books: dict[str, list[tuple[int, list, list]]] | None = None,
                 mids: dict[str, list[tuple[int, float]]] | None = None, funding: dict[str, list[tuple[int, float]]] | None = None):
        self.books = {c: sorted(v) for c, v in (books or {}).items()}
        self.mids = {c: sorted(v) for c, v in (mids or {}).items()}
        self.fund = {c: sorted(v) for c, v in (funding or {}).items()}

    def book(self, coin: str, due: int, tol: int) -> tuple[int, list, list] | None:
        """The captured book nearest the due time, within the tolerance, with both sides present."""
        rows = self.books.get(coin) or []
        i = bisect.bisect_left(rows, (due - tol,))
        near = [r for r in rows[i:] if r[0] <= due + tol]
        best = min(near, key=lambda r: abs(r[0] - due), default=None)
        return best if best and best[1] and best[2] else None

    def mark(self, coin: str, t: int) -> float | None:
        """The last mid at or before t (else the first after): the collector stores one every 5 minutes."""
        pts = self._mids(coin)
        if not pts:
            return None
        i = bisect.bisect_right(pts, (t, math.inf)) - 1
        return pts[max(i, 0)][1]

    def funding_events(self, coin: str, t0: int, t1: int) -> list[tuple[int, float]]:
        pts = self._funding(coin)
        return pts[bisect.bisect_right(pts, (t0, math.inf)):bisect.bisect_right(pts, (t1, math.inf))]

    def _mids(self, coin: str) -> list[tuple[int, float]]:
        return self.mids.get(coin) or []

    def _funding(self, coin: str) -> list[tuple[int, float]]:
        return self.fund.get(coin) or []


class PgMarket(Market):
    """Books are looked up one at a time (a window holds far more than memory should); mids and funding are
    loaded per coin for the window on first use."""

    def __init__(self, conn, t0: int, t1: int):
        super().__init__()
        self.conn, self.t0, self.t1 = conn, t0 - 86_400_000, t1 + 2 * HOUR_MS

    def book(self, coin: str, due: int, tol: int) -> tuple[int, list, list] | None:
        row = self.conn.execute("SELECT taken_ms, bids, asks FROM books WHERE coin = %s AND taken_ms BETWEEN %s AND %s "
                                "ORDER BY abs(taken_ms - %s) LIMIT 1", (coin, due - tol, due + tol, due)).fetchone()
        return (int(row[0]), row[1], row[2]) if row and row[1] and row[2] else None

    def _mids(self, coin: str) -> list[tuple[int, float]]:
        if coin not in self.mids:
            self.mids[coin] = [(int(t), p) for t, p in self.conn.execute(
                "SELECT time_ms, px FROM mids WHERE coin = %s AND time_ms BETWEEN %s AND %s ORDER BY time_ms", (coin, self.t0, self.t1))]
        return self.mids[coin]

    def _funding(self, coin: str) -> list[tuple[int, float]]:
        if coin not in self.fund:
            self.fund[coin] = [(int(t), r) for t, r in self.conn.execute(
                "SELECT time_ms, rate FROM funding WHERE coin = %s AND time_ms BETWEEN %s AND %s ORDER BY time_ms", (coin, self.t0, self.t1))]
        return self.fund[coin]


# ---------------------------------------------------------------------------
# copy_gap
# ---------------------------------------------------------------------------
def leader_orders(fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One order often fills in pieces at the same millisecond: a copier sends one order for all of them."""
    out: list[dict[str, Any]] = []
    for f in sorted(fills, key=lambda f: (f["time_ms"], f["tid"])):
        if not is_perp(f["coin"]):
            continue
        o = out[-1] if out else None
        if o and (o["time_ms"], o["coin"], o["side"]) == (f["time_ms"], f["coin"], f["side"]):
            o["sz"] += f["sz"]
            o["notional"] += f["px"] * f["sz"]
            o["fee"] += f["fee"]
            o["fills"] += 1
        else:
            out.append({"time_ms": f["time_ms"], "coin": f["coin"], "side": f["side"], "start_pos": f["start_pos"],
                        "sz": f["sz"], "notional": f["px"] * f["sz"], "fee": f["fee"], "fills": 1})
    for o in out:
        o["px"] = o["notional"] / o["sz"] if o["sz"] else 0.0
    return out


def equity_at(points: list[tuple[int, float]], t: int) -> float | None:
    if not points:
        return None
    i = bisect.bisect_right(points, (t, math.inf)) - 1
    return points[max(i, 0)][1]


class _Leg:
    def __init__(self) -> None:
        self.cash = self.fees = 0.0
        self.pos: dict[str, float] = {}
        self.trades: list[tuple[int, str, float, float, float]] = []   # (time, coin, signed size, px, fee)

    def trade(self, t: int, coin: str, size: float, px: float, fee: float) -> None:
        self.cash -= size * px
        self.fees += fee
        self.pos[coin] = self.pos.get(coin, 0.0) + size
        self.trades.append((t, coin, size, px, fee))

    def funding(self, market: Market, t_end: int) -> list[tuple[int, str, float]]:
        """Every hourly payment on the position held just before it: longs pay a positive rate."""
        out = []
        for coin in {c for _, c, *_ in self.trades}:
            mine = [(t, size, px) for t, c, size, px, _ in self.trades if c == coin]
            times, held = [t for t, _, _ in mine], []
            for _, size, _ in mine:
                held.append((held[-1] if held else 0.0) + size)
            for T, rate in market.funding_events(coin, times[0], t_end):
                i = bisect.bisect_left(times, T) - 1
                if i >= 0 and abs(held[i]) > EPS:
                    out.append((T, coin, -held[i] * (market.mark(coin, T) or mine[i][2]) * rate))
        return sorted(out)


def copy_gap(fills: list[dict[str, Any]], equity: list[tuple[int, float]], market: Market, cfg: CopyCfg,
             t0: int, t1: int, ticks: list[int] | None = None) -> dict[str, Any]:
    """What a copier `cfg.latency_ms` behind this wallet made over [t0, t1), next to the wallet's own execution of
    the same sizes; the difference split into latency + slippage + fees + funding (they add up to `gap.usd`)."""
    orders = [o for o in leader_orders(fills) if t0 <= o["time_ms"] < t1]
    tol, fee_rate = tolerance_ms(cfg.latency_ms), (cfg.taker_fee_bps + cfg.builder_fee_bps) / 1e4
    lead, copy = _Leg(), _Leg()
    rows: list[dict[str, Any]] = []
    n = {"copied": 0, "missing_book": 0, "no_equity": 0, "too_small": 0}
    latency = slippage = 0.0
    for o in orders:
        le = equity_at(equity, o["time_ms"])
        if not le or le <= 0:
            n["no_equity"] += 1
            continue
        delta = o["sz"] if o["side"] == "B" else -o["sz"]
        planned = plan_follow(o["start_pos"], delta, lead.pos.get(o["coin"], 0.0), cfg.equity_usd / le,
                              cfg.max_leverage * cfg.equity_usd / o["px"])
        acts = [(a, s) for a, s in planned if abs(s) * o["px"] >= MIN_ORDER_USD]
        n["too_small"] += len(planned) - len(acts)
        if not acts:
            continue
        book = market.book(o["coin"], o["time_ms"] + cfg.latency_ms, tol)
        if book is None:
            n["missing_book"] += 1
            continue
        taken, bids, asks = book
        mid = (bids[0][0] + asks[0][0]) / 2
        n["copied"] += 1
        lead_rate = o["fee"] / o["notional"] if o["notional"] else 0.0
        for action, size in acts:
            side = "B" if size > 0 else "A"
            if cfg.slippage_bps is None:
                levels = [[{"px": p, "sz": s} for p, s in bids], [{"px": p, "sz": s} for p, s in asks]]
                px, used, unfilled = walk_book({"levels": levels}, side, abs(size))
            else:
                px, used, unfilled = mid * (1 + math.copysign(cfg.slippage_bps, size) / 1e4), 0, 0.0
            lead.trade(o["time_ms"], o["coin"], size, o["px"], abs(size) * o["px"] * lead_rate)
            copy.trade(taken, o["coin"], size, px, abs(size) * px * fee_rate)
            latency += size * (mid - o["px"])
            slippage += size * (px - mid)
            rows.append({"time_ms": o["time_ms"], "coin": o["coin"], "action": action, "size": size, "leader_px": o["px"],
                         "leader_fee": abs(size) * o["px"] * lead_rate, "due_ms": o["time_ms"] + cfg.latency_ms, "book_ms": taken,
                         "mid": mid, "copy_px": px, "levels": used, "unfilled": unfilled, "copy_fee": abs(size) * px * fee_rate,
                         "latency_usd": size * (mid - o["px"]), "slippage_usd": size * (px - mid)})
    t_end = t1 + cfg.latency_ms
    fund_l = lead.funding(market, t_end) if cfg.funding else []
    fund_c = copy.funding(market, t_end) if cfg.funding else []
    last_px = {c: px for _, c, _, px, _ in copy.trades}
    marks = {c: market.mark(c, t_end) or last_px[c] for c, s in lead.pos.items() if abs(s) > EPS}

    def pnl(leg: _Leg, fund: list) -> float:
        return leg.cash - leg.fees + sum(a for *_, a in fund) + sum(s * marks[c] for c, s in leg.pos.items() if abs(s) > EPS)

    pl, pc, E = pnl(lead, fund_l), pnl(copy, fund_c), cfg.equity_usd
    fl, fc = sum(a for *_, a in fund_l), sum(a for *_, a in fund_c)
    tried = n["copied"] + n["missing_book"]
    out = {"latency_ms": cfg.latency_ms, "orders": len(orders), **n, "coverage": n["copied"] / tried if tried else None,
           "actions": len(rows), "leader": {"pnl": pl, "fees": lead.fees, "funding": fl, "ret": pl / E},
           "copier": {"pnl": pc, "fees": copy.fees, "funding": fc, "ret": pc / E},
           "gap": {"usd": pl - pc, "ret": (pl - pc) / E, "latency": latency, "slippage": slippage,
                   "fees": copy.fees - lead.fees, "funding": fl - fc},
           "open_notional": sum(abs(s) * marks[c] for c, s in lead.pos.items() if abs(s) > EPS), "rows": rows}
    if ticks:
        out["curve"] = _curve(copy, fund_c, market, ticks, E)
    return out


def _curve(leg: _Leg, fund: list[tuple[int, str, float]], market: Market, ticks: list[int], base: float) -> list[float]:
    """The copier's equity at each tick, open positions at the collector's mids."""
    cash = paid = got = 0.0
    pos: dict[str, float] = {}
    last: dict[str, float] = {}
    i = j = 0
    out = []
    for T in ticks:
        while i < len(leg.trades) and leg.trades[i][0] <= T:
            _, c, size, px, fee = leg.trades[i]
            cash, paid = cash - size * px, paid + fee
            pos[c], last[c] = pos.get(c, 0.0) + size, px
            i += 1
        while j < len(fund) and fund[j][0] <= T:
            got += fund[j][2]
            j += 1
        out.append(base + cash - paid + got + sum(s * (market.mark(c, T) or last[c]) for c, s in pos.items() if abs(s) > EPS))
    return out


# ---------------------------------------------------------------------------
# benchmarks and the random-wallet test (pure)
# ---------------------------------------------------------------------------
def random_test(target: float | None, pool: list[float], k: int, draws: int = 10_000, seed: int = 0) -> dict[str, Any]:
    """How often a portfolio of k wallets drawn at random from the pool did at least as well as the target.
    p = (1 + hits) / (1 + draws), so it is never 0: 10,000 draws cannot prove better than 1 in 10,001."""
    if target is None or k <= 0 or len(pool) < k:
        return {"p": None, "percentile": None, "draws": 0, "k": k, "pool": len(pool)}
    rng = random.Random(seed)
    hits = sum(statistics.fmean(rng.sample(pool, k)) >= target for _ in range(draws))
    return {"p": (1 + hits) / (1 + draws), "percentile": 1 - hits / draws, "draws": draws, "k": k, "pool": len(pool)}


def max_drawdown(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    peak, dd = values[0], 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            dd = max(dd, (peak - v) / peak)
    return dd


def vol_matched(port: list[float], btc: list[float]) -> dict[str, Any]:
    """BTC held at the leverage that gives it the copy portfolio's own hourly volatility, over the same hours."""
    rets = lambda v: [b / a - 1 for a, b in zip(v, v[1:]) if a > 0]  # noqa: E731
    rp, rb = rets(port), rets(btc)
    if len(rp) < 3 or len(rb) < 3 or statistics.pstdev(rb) == 0:
        return {"k": None, "ret": None}
    k = statistics.pstdev(rp) / statistics.pstdev(rb)
    return {"k": k, "ret": k * (btc[-1] / btc[0] - 1)}


def verdict(num: dict[str, Any], rules: dict[str, Any]) -> dict[str, Any]:
    gt = lambda a, b: a is not None and b is not None and a > b  # noqa: E731
    s = num.get("shortlist")
    checks = {"made_money": gt(s, rules["min_return"]),
              "beat_btc_hold": gt(s, num.get("btc_hold")) if rules.get("beat_btc_hold") else True,
              "beat_risk_matched": gt(s, num.get("risk_matched")) if rules.get("beat_risk_matched") else True,
              "beat_top_n": gt(s, num.get("top")) if rules.get("beat_top_n") else True,
              "random_test": num.get("random_p") is not None and num["random_p"] <= rules["max_random_p"],
              "drawdown": num.get("max_drawdown") is not None and num["max_drawdown"] <= rules["max_drawdown"],
              "enough_trades": (num.get("actions") or 0) >= rules["min_actions"],
              "coverage": num.get("coverage") is not None and num["coverage"] >= rules["min_coverage"]}
    return {"pass": all(checks.values()), "checks": checks}


# ---------------------------------------------------------------------------
# a window, every group, every delay
# ---------------------------------------------------------------------------
def _load(conn, t0: int, t1: int) -> tuple[dict[str, list[str]], dict[str, list], dict[str, list]]:
    groups = {g: [a for (a,) in conn.execute("SELECT address FROM tracked WHERE grp = %s ORDER BY address", (g,))] for g in GROUPS}
    fills: dict[str, list] = {}
    for a, tid, t, coin, side, px, sz, start, fee in conn.execute(
            "SELECT address, tid, time_ms, coin, side, px, sz, start_pos, fee FROM fills WHERE time_ms >= %s AND time_ms < %s", (t0, t1)):
        fills.setdefault(a, []).append({"tid": tid, "time_ms": t, "coin": coin, "side": side, "px": px, "sz": sz, "start_pos": start, "fee": fee})
    equity: dict[str, list] = {}
    for a, t, v in conn.execute("SELECT address, time_ms, value FROM equity WHERE time_ms < %s ORDER BY time_ms", (t1,)):
        equity.setdefault(a, []).append((int(t), v))
    return groups, fills, equity


def group_report(conn, plan: dict[str, Any], t0: int, t1: int, delays_ms: list[int], **over: Any) -> dict[str, Any]:
    groups, fills, equity = _load(conn, t0, t1)
    market = PgMarket(conn, t0, t1)
    judge = int(plan["holdout"]["delay_s"] * 1000)
    ticks = list(range(t0 - t0 % HOUR_MS + HOUR_MS, t1 + 1, HOUR_MS))
    base = CopyCfg.from_plan(plan["copy"], judge / 1000, **over)
    everyone = sorted({a for v in groups.values() for a in v})
    out: dict[str, Any] = {"window": [t0, t1], "config": {**dataclasses.asdict(base), "delays_ms": delays_ms, "plan_id": plan["id"]},
                           "groups": {g: len(v) for g, v in groups.items()}, "delays": {}}
    for d in delays_ms:
        cfg = dataclasses.replace(base, latency_ms=d)
        res = {a: copy_gap(fills.get(a, []), equity.get(a, []), market, cfg, t0, t1, ticks if d == judge else None) for a in everyone}
        mean = lambda g, k: statistics.fmean(res[a][k]["ret"] for a in groups[g]) if groups[g] else None  # noqa: E731
        pool = [res[a]["copier"]["ret"] for a in groups["random"]]
        tried = sum(res[a]["copied"] + res[a]["missing_book"] for a in groups["shortlist"])
        row = {"shortlist": mean("shortlist", "copier"), "shortlist_itself": mean("shortlist", "leader"), "top": mean("top", "copier"),
               "random_mean": statistics.fmean(pool) if pool else None, "random_median": statistics.median(pool) if pool else None,
               "random_test": random_test(mean("shortlist", "copier"), pool, len(groups["shortlist"])),
               "actions": sum(res[a]["actions"] for a in groups["shortlist"]),
               "coverage": sum(res[a]["copied"] for a in groups["shortlist"]) / tried if tried else None,
               "wallets": {a: {k: v for k, v in r.items() if k not in ("rows", "curve")} for a, r in res.items()}}
        if d == judge:
            b0, b1 = market.mark("BTC", t0), market.mark("BTC", t1)
            row["btc_hold"] = b1 / b0 - 1 if b0 and b1 else None
        if d == judge and ticks:
            port = [sum(vals) for vals in zip(*(res[a]["curve"] for a in groups["shortlist"]))] if groups["shortlist"] else []
            btc = [market.mark("BTC", T) for T in ticks]
            vm = vol_matched(port, btc) if port and all(btc) else {"k": None, "ret": None}
            row["risk_matched"], row["risk_k"] = vm["ret"], vm["k"]
            row["max_drawdown"] = max_drawdown(port)
            row["curve"] = [[T, v] for T, v in zip(ticks, port)]
        out["delays"][str(d)] = row
    return out


def _pct(x: float | None) -> str:
    return f"{x:+.2%}" if x is not None else "n/a"


def summary_line(rep: dict[str, Any], judge_ms: int) -> str:
    t0, t1 = rep["window"]
    fmt = lambda t: time.strftime("%m-%d %H:%M", time.gmtime(t / 1000))  # noqa: E731
    d = rep["delays"]
    j = d.get(str(judge_ms)) or {}
    curve = ", ".join(f"{int(k) // 1000}s {_pct(v['shortlist'])}" for k, v in d.items())
    p = (j.get("random_test") or {}).get("p")
    return (f"{fmt(t0)} to {fmt(t1)} UTC ({(t1 - t0) / HOUR_MS:.1f} h): shortlist copy {curve}; the wallets themselves "
            f"{_pct(j.get('shortlist_itself'))} | top-5 {_pct(j.get('top'))} | random median {_pct(j.get('random_median'))}, "
            f"p {'n/a' if p is None else f'{p:.3f}'} | BTC {_pct(j.get('btc_hold'))}, risk-matched {_pct(j.get('risk_matched'))} | "
            f"drawdown {_pct(j.get('max_drawdown'))} | {j.get('actions', 0)} copied trades, coverage {_pct(j.get('coverage'))}")


# ---------------------------------------------------------------------------
# the discipline: every look logged, the holdout judged once
# ---------------------------------------------------------------------------
def log_trial(conn, source: str, config: dict[str, Any], t0: int, t1: int, result: dict[str, Any]) -> int:
    from psycopg.types.json import Jsonb
    h = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    return conn.execute("INSERT INTO trials(at_ms, source, config, config_hash, window_start, window_end, result) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id", (now_ms(), source, Jsonb(config), h, t0, t1, Jsonb(result))).fetchone()[0]


def trials(conn, limit: int = 100) -> dict[str, Any]:
    rows = conn.execute("SELECT id, at_ms, source, config_hash, window_start, window_end, result, config FROM trials ORDER BY id DESC LIMIT %s", (limit,)).fetchall()
    n, distinct = conn.execute("SELECT COUNT(*), COUNT(DISTINCT config_hash) FROM trials WHERE source <> 'holdout'").fetchone()
    keys = ("id", "at_ms", "source", "config_hash", "window_start", "window_end", "result", "config")
    return {"n": n, "distinct_configs": distinct, "rows": [dict(zip(keys, r)) for r in rows]}


def _headline(rep: dict[str, Any], judge_ms: int) -> dict[str, Any]:
    j = rep["delays"].get(str(judge_ms)) or next(iter(rep["delays"].values()), {})
    return {"curve": {k: v["shortlist"] for k, v in rep["delays"].items()}, "itself": j.get("shortlist_itself"), "top": j.get("top"),
            "random_median": j.get("random_median"), "random_p": (j.get("random_test") or {}).get("p"), "btc_hold": j.get("btc_hold"),
            "risk_matched": j.get("risk_matched"), "max_drawdown": j.get("max_drawdown"), "actions": j.get("actions"), "coverage": j.get("coverage")}


def dev_report(conn, source: str = "cli", delays_s: list[float] | None = None, **over: Any) -> dict[str, Any]:
    """The development window: from the first tracked moment to now, never past the holdout's start."""
    from .tape import set_meta
    plan = plan_of(get_meta(conn, "plan"))
    t0 = conn.execute("SELECT MIN(added_ms) FROM tracked").fetchone()[0]
    t1 = min(now_ms(), day_ms(plan["holdout"]["start"]))
    if t0 is None or t1 <= t0:
        return {"line": "no development window yet"}
    delays = [int(d * 1000) for d in (delays_s or plan["copy"]["delays_s"])]
    judge = int(plan["holdout"]["delay_s"] * 1000)
    rep = group_report(conn, plan, int(t0), t1, delays, **over)
    rep["trial"] = log_trial(conn, source, rep["config"], int(t0), t1, _headline(rep, judge))
    rep["line"] = summary_line(rep, judge if str(judge) in rep["delays"] else delays[0]) + f" | look #{rep['trial']}"
    if source == "collector":
        set_meta(conn, "dev_report", rep)
    return rep


def evaluate_holdout(conn, now: int | None = None) -> dict[str, Any]:
    """Once: after the holdout's end + the longest delay, with the frozen plan. The result is stored under the
    plan's id and every later call returns it unchanged."""
    from psycopg.types.json import Jsonb
    frozen = get_meta(conn, "plan")
    plan, hid = plan_of(frozen), frozen["id"]
    done = get_meta(conn, f"holdout:{hid}")
    if done:
        return {**done, "status": "evaluated", "fresh": False}
    h = plan["holdout"]
    t0, t1 = day_ms(h["start"]), day_ms(h["end"])
    delays = [int(d * 1000) for d in plan["copy"]["delays_s"]]
    ready = t1 + max(delays) + 600_000
    if (now or now_ms()) < ready:
        return {"status": "waiting", "id": hid, "ready_at": ready}
    judge = int(h["delay_s"] * 1000)
    rep = group_report(conn, plan, t0, t1, sorted(set(delays) | {judge}))
    j = rep["delays"][str(judge)]
    num = {"shortlist": j["shortlist"], "top": j["top"], "btc_hold": j.get("btc_hold"), "risk_matched": j.get("risk_matched"),
           "random_p": j["random_test"]["p"], "max_drawdown": j.get("max_drawdown"), "actions": j["actions"], "coverage": j["coverage"]}
    v = verdict(num, plan["rules"])
    result = {"id": hid, "plan_sha256": frozen["sha256"], "evaluated_at": now or now_ms(), "window": [t0, t1], "numbers": num,
              "verdict": v, "report": rep, "line": f"{'PASS' if v['pass'] else 'FAIL'} " + ", ".join(
                  f"{k} {'ok' if ok else 'NO'}" for k, ok in v["checks"].items()) + " | " + summary_line(rep, judge)}
    won = conn.execute("INSERT INTO meta(key, value) VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING key",
                       (f"holdout:{hid}", Jsonb(result))).fetchone()
    if not won:                                        # someone else evaluated it a moment ago: theirs stands
        return {**get_meta(conn, f"holdout:{hid}"), "status": "evaluated", "fresh": False}
    log_trial(conn, "holdout", rep["config"], t0, t1, {**_headline(rep, judge), "pass": v["pass"]})
    return {**result, "status": "evaluated", "fresh": True}


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------
def _print_gap(addr: str, r: dict[str, Any], delay_s: float) -> None:
    print(f"{addr}: {r['orders']} orders, {r['copied']} copied, {r['missing_book']} without a book in time, "
          f"{r['too_small']} actions under ${MIN_ORDER_USD:g}, {r['no_equity']} before any account value | delay {delay_s:g} s")
    print(f"{'leader time UTC':19} {'coin':>6} {'action':>6} {'size':>12} {'leader px':>11} {'book +ms':>8} {'mid':>11} "
          f"{'copy px':>11} {'latency $':>9} {'slip $':>8} {'fee L':>7} {'fee C':>7}")
    for x in r["rows"]:
        print(f"{time.strftime('%m-%d %H:%M:%S', time.gmtime(x['time_ms'] / 1000))}.{x['time_ms'] % 1000:03d} {x['coin']:>6} {x['action']:>6} "
              f"{x['size']:>12.6g} {x['leader_px']:>11.6g} {x['book_ms'] - x['time_ms']:>8} {x['mid']:>11.6g} {x['copy_px']:>11.6g} "
              f"{x['latency_usd']:>9.4f} {x['slippage_usd']:>8.4f} {x['leader_fee']:>7.4f} {x['copy_fee']:>7.4f}")
    g = r["gap"]
    print(f"leader leg {r['leader']['pnl']:+.4f} (fees {r['leader']['fees']:.4f}, funding {r['leader']['funding']:+.4f}) | copier "
          f"{r['copier']['pnl']:+.4f} (fees {r['copier']['fees']:.4f}, funding {r['copier']['funding']:+.4f}) | gap {g['usd']:+.4f} = latency "
          f"{g['latency']:+.4f} + slippage {g['slippage']:+.4f} + fees {g['fees']:+.4f} + funding {g['funding']:+.4f} | still open "
          f"${r['open_notional']:,.2f}")


def cli(a: Any, url: str) -> int:
    from .tape import connect
    conn = connect(url)
    if get_meta(conn, "plan") is None:
        print("no frozen plan yet: start `copytest collect` first")
        return 1
    plan = plan_of(get_meta(conn, "plan"))
    over = {"taker_fee_bps": a.fee_bps, "builder_fee_bps": 0.0 if a.fee_bps is not None else None, "max_leverage": a.leverage,
            "slippage_bps": a.slippage_bps, "funding": False if a.no_funding else None}
    if a.action == "log":
        t = trials(conn)
        print(f"{t['n']} looks at the development window, {t['distinct_configs']} distinct configurations")
        for r in reversed(t["rows"]):
            res = r["result"] or {}
            curve = " ".join(f"{int(k) // 1000}s {_pct(v)}" for k, v in (res.get("curve") or {}).items())
            print(f"#{r['id']:<4} {time.strftime('%Y-%m-%d %H:%M', time.gmtime(r['at_ms'] / 1000))} {r['source']:<9} "
                  f"{r['config_hash'][:10]} {curve or res}")
        return 0
    if a.action == "holdout":
        res = evaluate_holdout(conn)
        if res["status"] == "waiting":
            print(f"holdout {res['id']}: judged once, not before {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(res['ready_at'] / 1000))}")
        else:
            print(f"holdout {res['id']} ({'just now' if res.get('fresh') else 'stored result'}): {res['line']}")
        return 0
    if a.action == "report":
        rep = dev_report(conn, "cli", [a.delay] if a.delay else None, **over)
        print(rep["line"])
        for k, row in rep.get("delays", {}).items():
            print(f"\ndelay {int(k) / 1000:g} s: shortlist {_pct(row['shortlist'])} (wallets themselves {_pct(row['shortlist_itself'])}), "
                  f"top-5 {_pct(row['top'])}, random mean {_pct(row['random_mean'])} / median {_pct(row['random_median'])}")
            quiet = sum(1 for r in row["wallets"].values() if not r["orders"])
            print(f"  ({quiet} of {len(row['wallets'])} wallets placed no order in the window)")
            for addr, r in sorted(((a, r) for a, r in row["wallets"].items() if r["orders"]), key=lambda x: -x[1]["copier"]["ret"]):
                print(f"  {addr} copy {_pct(r['copier']['ret']):>8} itself {_pct(r['leader']['ret']):>8} gap {r['gap']['usd']:+9.2f} "
                      f"(latency {r['gap']['latency']:+.2f}, slippage {r['gap']['slippage']:+.2f}, fees {r['gap']['fees']:+.2f}, "
                      f"funding {r['gap']['funding']:+.2f}) {r['actions']} trades, coverage {_pct(r['coverage'])}")
        return 0
    if a.action == "gap":
        if not a.address:
            print("gap needs a wallet address")
            return 2
        t0 = conn.execute("SELECT MIN(added_ms) FROM tracked").fetchone()[0] or 0
        t1 = min(now_ms(), day_ms(plan["holdout"]["start"]))
        delay = a.delay if a.delay is not None else plan["holdout"]["delay_s"]
        cfg = CopyCfg.from_plan(plan["copy"], delay, **over)
        addr = a.address.lower()
        fills = [dict(zip(("tid", "time_ms", "coin", "side", "px", "sz", "start_pos", "fee"), r)) for r in conn.execute(
            "SELECT tid, time_ms, coin, side, px, sz, start_pos, fee FROM fills WHERE address = %s AND time_ms >= %s AND time_ms < %s",
            (addr, t0, t1))]
        eq = [(int(t), v) for t, v in conn.execute("SELECT time_ms, value FROM equity WHERE address = %s AND time_ms < %s ORDER BY time_ms", (addr, t1))]
        r = copy_gap(fills, eq, PgMarket(conn, t0, t1), cfg, t0, t1)
        log_trial(conn, "gap", {**dataclasses.asdict(cfg), "address": addr}, t0, t1, {"copier": r["copier"]["ret"], "leader": r["leader"]["ret"]})
        _print_gap(addr, r, delay)
        return 0
    return 2
