"""Mature pump.fun coins: the Strategy tab's question, asked later in a coin's life.

The launch replay says the first minute loses whichever way you leave. This buys a coin only once it has shown
something, and holds it for minutes to hours:

    near    its curve is about 80 % full (300 SOL of market cap), the way 5K3N1v... buys; not a coin that jumped
            straight from launch to migration, there is no curve left to buy
    grad    it has just migrated to PumpSwap: the first pool trade
    mc100k  it has reached about $100k (870 SOL of market cap), on either venue
    aged1h  an hour after migrating, it still trades at or above its migration price

Each entry is sold seven ways: on the clock after 5 min, 30 min, 2 h or 6 h, or at a take-profit / stop-loss pair
(+20 / -10 %, +50 / -25 %, +100 / -50 %, else out at 6 h). Market cap is the price times a billion tokens.

A coin is looked at once, 30 h after it was created (trades are kept 72 h): what it did in its first 24 h can be an
entry, and every exit falls inside the 6 h after that, so no result is cut short. Stored as prices (reserves), not
profits, like `launches`, so the costs stay changeable.
"""
from __future__ import annotations

import bisect
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from .pumpfun import FEE, LAMPORTS, TX_COST_SOL, _rules_on, _value, connect, get_meta, set_meta

CURVE_DONE_VTOK = 279_900_000_000_000   # a curve's virtual tokens once its last real token is sold: it migrates
NEAR_MCAP = 300.0                        # SOL of market cap: a curve about 80 % of the way up
MC100K = 870.0                           # SOL of market cap: about $100k at SOL $115 (2026-09-24)
MIN_LIQ_SOL = 20.0                       # no entry into a pool thinner than this: the order would be most of it
STAKE_SOL = 0.5                          # into a ~85 SOL pool: price impact and the fixed tx cost are both under 1 %
INSTANT_S = 60                           # migrated within a minute of launch: a bundle buying its own curve out
ENTRY_S = 86_400                         # entries within a coin's first day
HOLDS = {"5m": 300, "30m": 1800, "2h": 7200, "6h": 21600}
TPSL = {"tp20_sl10": (0.2, 0.1), "tp50_sl25": (0.5, 0.25), "tp100_sl50": (1.0, 0.5)}
HORIZON_S = max(HOLDS.values())
LOOK_AT_S = ENTRY_S + HORIZON_S + 1800   # a coin's age when it is looked at: past its last possible exit
TRIGGERS = ("near", "grad", "mc100k", "aged1h")

SCHEMA = """
CREATE TABLE IF NOT EXISTS matures (mint TEXT, trig TEXT, ts INTEGER, age_s INTEGER, grad_s INTEGER, mcap REAL,
                                    states TEXT, PRIMARY KEY (mint, trig));
CREATE TABLE IF NOT EXISTS mature_cand (mint INTEGER PRIMARY KEY, done INTEGER DEFAULT 0);
"""


def mcap(vsol: int, vtok: int) -> float:
    """Market cap in SOL: the price times a billion tokens."""
    return vsol / vtok * 1e6 if vtok > 0 else 0.0


def coin_entries(path: list[tuple], cts: int, latency_slots: int) -> tuple[dict[str, dict[str, Any]], int | None]:
    """Every entry one coin offered in its first day, with the state each exit lands on, and the index of the trade
    that completed its curve (None: it never did). `path` is (slot, ts, vsol, vtok, buy, sol, fee) in slot order.
    Like the launch replay, an order lands `latency_slots` after the trade it reacts to, at the reserves every trade
    before that left."""
    if not path:
        return {}, None
    slots, times = [r[0] for r in path], [r[1] for r in path]
    done = next((i for i, r in enumerate(path) if r[3] <= CURVE_DONE_VTOK), None)
    fee_at, rate = [], FEE           # what an order landing on each trade pays, known by then: the curve's fee, then
    for k, r in enumerate(path):     # the last pool sell that paid one (PumpSwap buy events log almost none)
        if done is not None and k > done and not r[4] and r[5] >= 10**7 and r[6] > 0:
            rate = r[6] / r[5]
        fee_at.append(FEE if done is None or k <= done else rate)

    def land(i: int) -> list:
        j = bisect.bisect_left(slots, path[i][0] + latency_slots) - 1
        return [path[j][2], path[j][3], fee_at[j]]

    last_by = lambda t: bisect.bisect_right(times, t) - 1   # noqa: E731 - the last trade at or before t
    cap = lambda i: mcap(path[i][2], path[i][3])             # noqa: E731
    first_day = range(last_by(cts + ENTRY_S) + 1)
    trig: dict[str, tuple[int, int]] = {}                    # entry -> (the trade whose price it takes, when it decides)
    near = next((i for i in first_day if cap(i) >= NEAR_MCAP), None)
    if near is not None and (done is None or near < done):
        trig["near"] = (near, times[near])
    if done is not None and done + 1 in first_day:
        g = done + 1
        trig["grad"] = (g, times[g])
        t, a = times[g] + 3600, last_by(times[g] + 3600)
        if t <= cts + ENTRY_S and cap(a) >= cap(g):
            trig["aged1h"] = (a, t)                          # decided on the clock, at the last trade's price
    mc = next((i for i in first_day if cap(i) >= MC100K), None)
    if mc is not None:
        trig["mc100k"] = (mc, times[mc])
    out: dict[str, dict[str, Any]] = {}
    for name, (i, t0) in trig.items():
        entry = land(i)
        if entry[0] < MIN_LIQ_SOL * LAMPORTS or entry[1] <= 0:
            continue
        p0, landed = entry[0] / entry[1], slots[i] + latency_slots
        st = {"entry": entry, **{h: land(last_by(t0 + secs)) for h, secs in HOLDS.items()}}
        end = last_by(t0 + HORIZON_S)
        for rule, (tp, sl) in TPSL.items():
            j = next((j for j in range(i + 1, end + 1) if slots[j] >= landed
                      and not (1 - sl) * p0 < path[j][2] / path[j][3] < (1 + tp) * p0), None)
            st[rule] = land(end if j is None else j)
        out[name] = {"ts": t0, "mcap": cap(i), "states": st}
    return out, done


def mature_pnl(states: dict[str, list], stake_sol: float = STAKE_SOL, tx_cost_sol: float = TX_COST_SOL) -> dict[str, float]:
    """What each exit made on one entry: `stake_sol` bought at the entry, sold at the exit, both at their venue's fee,
    plus a transaction cost on each side."""
    vsol, vtok, fee_in = states["entry"]
    tokens = vtok - vsol * vtok / (vsol + stake_sol * LAMPORTS / (1 + fee_in))
    if tokens <= 0:
        return {}
    return {k: _value((v, t), tokens, fee) - stake_sol - 2 * tx_cost_sol for k, (v, t, fee) in states.items() if k != "entry"}


def _find_candidates(c: sqlite3.Connection, chunk: int) -> None:
    """Coins that ever traded at `NEAR_MCAP` or more, from the trades stored since the last look, read in rowid
    order: a sequential scan, one short read per chunk."""
    top = c.execute("SELECT MAX(rowid) FROM trades").fetchone()[0] or 0
    lo = get_meta(c, "mature_rowid", 0)
    lo = 0 if lo > top else lo                               # the table was emptied: start over
    while lo < top:
        hi = min(lo + chunk, top)
        found = c.execute("SELECT DISTINCT mint FROM trades WHERE rowid > ? AND rowid <= ? AND vsol * 1e6 >= ? * vtok",
                          (lo, hi, NEAR_MCAP)).fetchall()
        c.executemany("INSERT OR IGNORE INTO mature_cand (mint) VALUES (?)", found)
        set_meta(c, "mature_rowid", hi)
        c.commit()
        lo = hi


def settle_matures(db_path: str | Path, latency_slots: int | None = None, now: float | None = None,
                   chunk: int = 1_000_000) -> int:
    """Look once at every candidate coin old enough and store its entries. Returns how many entries it stored."""
    now = time.time() if now is None else now
    c = connect(db_path)
    try:
        c.executescript(SCHEMA)
        if latency_slots is None:
            measured = (get_meta(c, "stats", {}) or {}).get("lag_p50")
            latency_slots = max(2, measured + 1) if measured is not None else 2
        _find_candidates(c, chunk)
        todo = c.execute("""SELECT k.mint, m.addr, m.ts FROM mature_cand k JOIN mints m ON m.id = k.mint
                            WHERE k.done = 0 AND m.ts <= ?""", (now - LOOK_AT_S,)).fetchall()
        stored = 0
        for mid, addr, cts in todo:                          # read first, then one short write per coin
            path = c.execute("SELECT slot, ts, vsol, vtok, buy, sol, fee FROM trades WHERE mint = ? ORDER BY slot, rowid",
                             (mid,)).fetchall()
            found, done = coin_entries(path, cts, latency_slots)
            grad_s = path[done][1] - cts if done is not None else None
            rows = [(addr, k, e["ts"], e["ts"] - cts, grad_s, e["mcap"], json.dumps(e["states"])) for k, e in found.items()]
            c.executemany("INSERT OR IGNORE INTO matures VALUES (?,?,?,?,?,?,?)", rows)
            c.execute("UPDATE mature_cand SET done = 1 WHERE mint = ?", (mid,))
            c.commit()
            stored += len(rows)
        c.execute("DELETE FROM mature_cand WHERE mint < (SELECT MIN(id) FROM mints)")   # their coins were pruned
        c.commit()
        return stored
    finally:
        c.close()


def mature_report(db_path: str | Path, stake_sol: float = STAKE_SOL, tx_cost_sol: float = TX_COST_SOL,
                  days: float = 30) -> dict[str, Any]:
    """Every exit on every entry, per trigger, for all coins and split by how fast they migrated."""
    params = {"stake_sol": stake_sol, "tx_cost_sol": tx_cost_sol, "near_mcap": NEAR_MCAP, "mc100k": MC100K,
              "min_liq_sol": MIN_LIQ_SOL, "instant_s": INSTANT_S, "entry_h": ENTRY_S / 3600, "horizon_h": HORIZON_S / 3600}
    c = connect(db_path, readonly=True)
    try:
        cur = c.execute("SELECT trig, ts, age_s, grad_s, states FROM matures WHERE ts >= ?", (time.time() - days * 86_400,))
        rows = [{"trig": t, "ts": ts, "age_s": a, "grad_s": g, "states": json.loads(s)} for t, ts, a, g, s in cur.fetchall()]
    except sqlite3.OperationalError:
        rows = []                                            # before the first settle made the table
    finally:
        c.close()
    if not rows:
        return {"generated": int(time.time()), "empty": True, "params": params}
    t0, t1 = min(r["ts"] for r in rows), max(r["ts"] for r in rows)
    def fast(r: dict[str, Any]) -> bool:
        """Everything happened in the first minute, as known when buying: a near entry reached 300 SOL in it (whether
        the coin then migrates is still to come), the others had already migrated in it."""
        if r["trig"] == "near":
            return r["age_s"] < INSTANT_S
        return r["grad_s"] is not None and r["grad_s"] < INSTANT_S and r["grad_s"] <= r["age_s"]
    cohorts = {"all": lambda r: True, "organic": lambda r: not fast(r), "instant": fast}
    price = lambda r: mature_pnl(r["states"], stake_sol, tx_cost_sol)     # noqa: E731
    triggers: dict[str, dict[str, list]] = {}
    counts: dict[str, dict[str, int]] = {}
    for trig in TRIGGERS:
        for name, keep in cohorts.items():
            sub = [r for r in rows if r["trig"] == trig and keep(r)]
            if sub:                                          # halves at the median: late entries stretch the window
                mid = sorted(r["ts"] for r in sub)[len(sub) // 2]
                triggers.setdefault(trig, {})[name] = _rules_on(sub, stake_sol, tx_cost_sol, mid, price)
                counts.setdefault(trig, {})[name] = len(sub)
    return {"generated": int(time.time()), "params": params, "counts": counts, "triggers": triggers,
            "window": {"start": t0, "end": t1, "hours": (t1 - t0) / 3600}}
