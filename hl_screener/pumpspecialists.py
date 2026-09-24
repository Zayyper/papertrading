"""Wallets that trade mature pump.fun coins: who buys a coin only once it is worth $100k or more, does it pay, and
would copying it pay? Buying those coins blindly loses (pumpmature), so this looks for the traders who pick them
better, the way the golden wallets were found among the launches.

A position is one wallet's trades in one coin, counted when its first buy there came at $100k of market cap or more
(it did not snipe the coin and add later). Its own result is what it paid and got back, plus what it still holds at
the last price; tokens it sold beyond what it bought came from elsewhere and earn it nothing. The copy is replayed:
`stake` SOL bought `latency` slots after its first buy and sold `latency` slots after its first sell (or held to the
last price), each at the fee a trade in that coin was paying then. Coins its own wallet launched are left out.
Read from the trades still stored: coins born in the last three days, so older coins' specialists are out of sight.

Wallets that made money that way in both halves of the window, themselves and copied, are paper-followed from then on
(`follow_specialists`): the ranking picks them on the past, the paper follower tests them on what comes next.
There are over a million positions, so they are built and summed inside SQLite (temp tables), never held in Python.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from .pumpfun import FEE, TX_COST_SOL, _state_before, connect, copy_trade, get_meta
from .pumpmature import CURVE_DONE_VTOK, INSTANT_S, MC_LEVELS, SOL_USD, STAKE_SOL

BUCKETS = (("$100-200k", MC_LEVELS["mc100k"]), ("$200-500k", MC_LEVELS["mc200k"]), ("$500k-1M", MC_LEVELS["mc500k"]),
           ("$1M+", MC_LEVELS["mc1m"]))
MIN_SELL = 10**7                         # a sell under 0.01 SOL rounds its fee too coarsely to read the rate from
DETAIL_MAX = 300                         # a wallet's latest positions looked at up close (ponytail: a bot with thousands
                                         # gets its copy measured on its latest 300; raise it if the report has time)

MPOS_SQL = """
CREATE TEMP TABLE mpos AS
WITH pos AS (
  SELECT wallet, mint, MIN(CASE WHEN buy = 1 THEN rowid END) AS fbr,
         SUM(CASE WHEN buy = 1 THEN sol + fee ELSE 0 END) AS cost, SUM(CASE WHEN buy = 0 THEN sol - fee ELSE 0 END) AS back,
         SUM(CASE WHEN buy = 1 THEN tok ELSE 0 END) AS tin, SUM(CASE WHEN buy = 0 THEN tok ELSE 0 END) AS tout
  FROM trades WHERE +mint IN (SELECT mint FROM mlast) GROUP BY wallet, mint)
SELECT p.wallet, p.mint, f.slot, f.ts, f.vsol * 1e6 / f.vtok AS mcap, p.cost / 1e9 AS cost,
       ((CASE WHEN p.tout > p.tin THEN p.back * 1.0 * p.tin / p.tout ELSE p.back END) - p.cost
        + CASE WHEN p.tin > p.tout AND l.vtok > 0
               THEN (l.vsol - l.vsol * 1.0 * l.vtok / (l.vtok + p.tin - p.tout)) * (1 - l.fee) ELSE 0 END) / 1e9 AS pnl,
       p.tin - p.tout > 0.02 * p.tin AS held
FROM pos p JOIN trades f ON f.rowid = p.fbr JOIN mints m ON m.id = p.mint JOIN mlast l ON l.mint = p.mint
WHERE f.vsol * 1e6 >= ? * f.vtok AND p.wallet != m.creator
"""     # a coin's tokens sold beyond what was bought came from elsewhere: only the bought share of the proceeds counts


def _fee_known(c: sqlite3.Connection, mint: int, slot: int | None) -> float:
    """The fee a trade in this coin paid at `slot` (None: now), from the last sell that paid one: PumpSwap buy events
    log almost none, and the rate moves with the coin's market cap."""
    row = c.execute("""SELECT fee * 1.0 / sol FROM trades WHERE mint = ? AND slot <= ? AND buy = 0 AND sol >= ? AND fee > 0
                       ORDER BY slot DESC LIMIT 1""", (mint, 2**62 if slot is None else slot, MIN_SELL)).fetchone()
    return row[0] if row else FEE


def _build(c: sqlite3.Connection, min_mcap: float) -> int:
    """Temp table `mpos`: one row per position whose first buy came at `min_mcap` SOL of market cap or more, with its
    own result in SOL. Returns how many; 0 before pumpmature made its candidate table."""
    try:
        mints = [m for (m,) in c.execute("SELECT mint FROM mature_cand")]
    except sqlite3.OperationalError:
        return 0
    c.execute("CREATE TEMP TABLE mlast (mint INTEGER PRIMARY KEY, vsol INTEGER, vtok INTEGER, fee REAL)")
    c.executemany("INSERT INTO mlast VALUES (?, ?, ?, ?)",
                  [(m, *(_state_before(c, m, None) or (0, 0)), _fee_known(c, m, None)) for m in mints])
    c.execute(MPOS_SQL, (min_mcap,))
    c.execute("CREATE INDEX temp.ix_mpos_wallet ON mpos (wallet)")
    c.commit()                                               # temp tables only: the store itself is read-only here
    return c.execute("SELECT COUNT(*) FROM mpos").fetchone()[0]


def _detail(c: sqlite3.Connection, wallet: int, p: dict[str, Any], latency: int, stake: float, tx: float) -> dict[str, Any]:
    """A position up close: when the wallet first sold, what a copy made, and whether the coin was a bundle."""
    mint, fb = p["mint"], p["slot"]
    fs = c.execute("SELECT slot, ts FROM trades WHERE wallet = ? AND mint = ? AND buy = 0 AND slot >= ? ORDER BY slot LIMIT 1",
                   (wallet, mint, fb)).fetchone()
    done = c.execute("SELECT slot, ts FROM trades WHERE mint = ? AND vtok <= ? ORDER BY slot LIMIT 1", (mint, CURVE_DONE_VTOK)).fetchone()
    mts = c.execute("SELECT ts FROM mints WHERE id = ?", (mint,)).fetchone()[0]
    out = {"hold_s": fs[1] - p["ts"] if fs else None,
           "bundle": bool(done and done[1] - mts < INSTANT_S and done[0] <= fb), "copy": None}
    entry = _state_before(c, mint, fb + latency)
    if entry and entry[0] > 0 and entry[1] > 0:
        exit_ = _state_before(c, mint, fs[0] + latency) if fs else _state_before(c, mint, None)
        out["copy"] = copy_trade(entry, exit_, stake, tx, _fee_known(c, mint, fb + latency),
                                 _fee_known(c, mint, fs[0] + latency if fs else None))
    return out


def _roi(ps: list[dict[str, Any]], key: str = "pnl", cost: str = "cost") -> float | None:
    spent = sum(p[cost] for p in ps)
    return sum(p[key] for p in ps) / spent if spent else None


def _wallet_row(c: sqlite3.Connection, w: int, ps: list[dict[str, Any]], mid: float, latency: int, stake: float, tx: float,
                min_coins: int) -> dict[str, Any]:
    close = sorted(ps, key=lambda p: p["ts"])[-DETAIL_MAX:]
    for p in close:
        p.update(_detail(c, w, p, latency, stake, tx))
    copied = [dict(p, copy_cost=stake) for p in close if p["copy"] is not None]
    wins = sorted((p["pnl"] for p in ps if p["pnl"] > 0), reverse=True)
    holds = sorted(p["hold_s"] for p in close if p["hold_s"] is not None)
    bought = c.execute("SELECT COUNT(DISTINCT mint) FROM trades WHERE wallet = ? AND buy = 1", (w,)).fetchone()[0]
    half = lambda xs, first: [p for p in xs if (p["ts"] < mid) == first]          # noqa: E731
    r = {"addr": c.execute("SELECT addr FROM wallets WHERE id = ?", (w,)).fetchone()[0], "n": len(ps),
         "share": len(ps) / bought if bought else None, "cost_sol": sum(p["cost"] for p in ps), "pnl_sol": sum(p["pnl"] for p in ps),
         "roi": _roi(ps), "roi_h1": _roi(half(ps, True)), "roi_h2": _roi(half(ps, False)),
         "win_rate": sum(p["pnl"] > 0 for p in ps) / len(ps), "top2_share": sum(wins[:2]) / sum(wins) if wins else None,
         "mcap_med_usd": sorted(p["mcap"] for p in ps)[len(ps) // 2] * SOL_USD,
         "hold_med_min": holds[len(holds) // 2] / 60 if holds else None,
         "bundle_share": sum(p["bundle"] for p in close) / len(close), "held_share": sum(bool(p["held"]) for p in ps) / len(ps),
         "copy_n": len(copied), "copy_roi": _roi(copied, "copy", "copy_cost"),
         "copy_roi_h1": _roi(half(copied, True), "copy", "copy_cost"), "copy_roi_h2": _roi(half(copied, False), "copy", "copy_cost"),
         "copy_win_rate": sum(p["copy"] > 0 for p in copied) / len(copied) if copied else None}
    both = all((r[k] or 0) > 0 for k in ("roi_h1", "roi_h2", "copy_roi_h1", "copy_roi_h2"))
    r["follow"] = bool(both and len(ps) >= min_coins)       # worth testing forward: the paper follower takes it on
    r["passes"] = bool(r["follow"] and (r["top2_share"] or 1) <= 0.5)   # and not two lucky coins
    return r


def specialists(db_path: str | Path, min_mcap: float = MC_LEVELS["mc100k"], min_coins: int = 10, detail_top: int = 40,
                stake: float = STAKE_SOL, tx: float = TX_COST_SOL, latency: int | None = None) -> dict[str, Any]:
    """The base rate by entry market cap over every such position, then the `detail_top` wallets with `min_coins`+ such
    coins that made the most, up close and copied, best copy first."""
    t_build = time.time()
    c = connect(db_path, readonly=True)
    try:
        if latency is None:
            measured = (get_meta(c, "stats", {}) or {}).get("lag_p50")
            latency = max(2, measured + 1) if measured is not None else 2
        params = {"min_mcap_usd": min_mcap * SOL_USD, "min_coins": min_coins, "stake_sol": stake, "tx_cost_sol": tx,
                  "latency_slots": latency}
        n_pos = _build(c, min_mcap)
        if not n_pos:
            return {"generated": int(t_build), "empty": True, "params": params}
        mid = c.execute("SELECT ts FROM mpos ORDER BY ts LIMIT 1 OFFSET ?", (n_pos // 2,)).fetchone()[0]   # equal halves
        edges = [lo for _, lo in BUCKETS[1:]]
        buckets = [{"bucket": BUCKETS[b][0], "n": n, "roi": pnl / cost if cost else None, "win_rate": won / n}
                   for b, n, pnl, cost, won in c.execute(
                       """SELECT CASE WHEN mcap < ? THEN 0 WHEN mcap < ? THEN 1 WHEN mcap < ? THEN 2 ELSE 3 END AS b,
                                 COUNT(*), SUM(pnl), SUM(cost), SUM(pnl > 0) FROM mpos GROUP BY b ORDER BY b""", edges)]
        many = c.execute("""SELECT wallet, SUM(pnl) AS pnl, SUM(CASE WHEN ts < :mid THEN pnl END), SUM(CASE WHEN ts < :mid THEN cost END),
                                   SUM(CASE WHEN ts >= :mid THEN pnl END), SUM(CASE WHEN ts >= :mid THEN cost END)
                            FROM mpos GROUP BY wallet HAVING COUNT(*) >= :min""", {"mid": mid, "min": min_coins}).fetchall()
        both = sum(1 for _, _, p1, c1, p2, c2 in many if c1 and c2 and p1 > 0 and p2 > 0)
        cols = ("mint", "slot", "ts", "mcap", "cost", "pnl", "held")
        rows = []
        for w, *_ in sorted(many, key=lambda r: r[1], reverse=True)[:detail_top]:
            ps = [dict(zip(cols, r)) for r in c.execute(f"SELECT {', '.join(cols)} FROM mpos WHERE wallet = ?", (w,))]
            rows.append(_wallet_row(c, w, ps, mid, latency, stake, tx, min_coins))
        rows.sort(key=lambda r: r["copy_roi"] if r["copy_roi"] is not None else -9, reverse=True)
        n_wallets = c.execute("SELECT COUNT(DISTINCT wallet) FROM mpos").fetchone()[0]
        return {"generated": int(time.time()), "build_s": round(time.time() - t_build, 1), "params": params,
                "counts": {"positions": n_pos, "wallets": n_wallets, "wallets_min": len(many), "both_halves": both,
                           "profitable": sum(1 for r in many if r[1] > 0)},
                "buckets": buckets, "wallets": rows}
    finally:
        c.close()


def follow_specialists(db_path: str | Path, rep: dict[str, Any]) -> list[str]:
    """Paper-follow, for good, every ranked wallet with `follow` set; returns the ones followed for the first time."""
    addrs = [r["addr"] for r in rep.get("wallets", []) if r.get("follow")]
    if not addrs:
        return []
    c = connect(db_path)
    try:
        before = {w for (w,) in c.execute("SELECT wallet FROM follow WHERE mature_ever = 1")}
        c.executemany("""INSERT INTO follow (wallet, added_at, golden_now, golden_ever, sniper_now, mature_ever) VALUES (?, ?, 0, 0, 0, 1)
                         ON CONFLICT(wallet) DO UPDATE SET mature_ever = 1""", [(a, int(time.time())) for a in addrs])
        c.commit()
        return [a for a in addrs if a not in before]
    finally:
        c.close()


def log_lines(rep: dict[str, Any], top: int = 10) -> list[str]:
    """The ranking for the collector's log: the page is behind a password, the log is not."""
    if rep.get("empty"):
        return ["specialists: no position bought at $100k+ yet"]
    n, p = rep["counts"], rep["params"]
    pct = lambda x, nd=0: f"{x:+.{nd}%}" if x is not None else "n/a"       # noqa: E731
    lines = [f"specialists (first buy at ${p['min_mcap_usd'] / 1000:.0f}k+ market cap): {n['positions']:,} positions by "
             f"{n['wallets']:,} wallets; their own result by entry: " + ", ".join(
                 f"{b['bucket']} {pct(b['roi'], 1)} (n{b['n']}, won {b['win_rate']:.0%})" for b in rep["buckets"])
             + f" | {n['wallets_min']:,} wallets with {p['min_coins']}+ such coins: {n['profitable']:,} made money, "
             f"{n['both_halves']:,} in both halves; copies at {p['stake_sol']} SOL, {p['latency_slots']} slots late"]
    for i, r in enumerate(rep["wallets"][:top], 1):
        hold = f"{r['hold_med_min']:.0f} min" if r["hold_med_min"] is not None else "n/a"
        tag = ", PASSES" if r["passes"] else ", followed" if r["follow"] else ""
        lines.append(f"specialist {i} {r['addr']}: {r['n']} coins ({r['share']:.0%} of what it buys), own {pct(r['roi'], 1)} "
                     f"[{pct(r['roi_h1'])}/{pct(r['roi_h2'])}] won {r['win_rate']:.0%} top 2 {r['top2_share'] or 0:.0%}, "
                     f"copy {pct(r['copy_roi'], 1)} [{pct(r['copy_roi_h1'])}/{pct(r['copy_roi_h2'])}] won "
                     f"{r['copy_win_rate'] or 0:.0%}, buys at ${r['mcap_med_usd'] / 1000:.0f}k median, holds {hold}, "
                     f"{r['bundle_share']:.0%} bundles, {r['held_share']:.0%} still held{tag}")
    return lines
