"""Exit sweep: copy a followed wallet's first buy of each coin, the way the paper does, but leave by a rule of our own
instead of when the wallet sells. Which exit would have paid, on the trades still stored?

    wallet       out when the wallet first sells (at the last price seen if it never did): today's copy, to compare with
    hold10s..10m out on the clock, counted from the wallet's buy (trade timestamps, whole seconds)
    tpX_slY      out once the position is worth X % more or Y % less than the stake, whichever comes first, else at MAX_HOLD_S
    trail15      out once it is worth 15 % less than its best since the buy, else at MAX_HOLD_S; trail15@10 the same, armed
                 only once it was worth 10 % more than the stake
    wallet_sl20  out when the wallet sells, or before that at -20 %

"Worth" is what selling the whole position would fetch at that trade's reserves, after the coin's fee and before the
transaction cost: the number a bot holding the tokens can watch. The buy lands PAPER_BUY_SLOTS after the wallet's,
each exit PAPER_SELL_SLOTS after the trade that triggers it, at the reserves every trade before that left; both pay the
coin's fee and TX_COST_SOL, like copy_sim. Our own orders do not move the stored prices, as everywhere in the replay.
"""
from __future__ import annotations

import bisect
import sqlite3
from pathlib import Path
from typing import Any

from .pumpfun import (LAMPORTS, PAPER_BUY_SLOTS, PAPER_SELL_SLOTS, PAPER_STAKE_SOL, TX_COST_SOL, _fee_known, _fee_rate, _pct,
                      _rules_line, _rules_on, _state_before, _value, connect, copy_trade, tx_cost_at, tx_costs)

HOLDS = {"hold10s": 10, "hold30s": 30, "hold1m": 60, "hold2m": 120, "hold5m": 300, "hold10m": 600}
TPSL = {"tp10_sl10": (0.1, 0.1), "tp20_sl10": (0.2, 0.1), "tp20_sl20": (0.2, 0.2), "tp30_sl15": (0.3, 0.15),
        "tp50_sl25": (0.5, 0.25), "tp100_sl50": (1.0, 0.5)}
TRAILS = {"trail15": (0.15, None), "trail15@10": (0.15, 0.10)}   # drop from the best worth, worth it must reach first
WALLET_SL = 0.2                # the stop under the wallet's own exit
MAX_HOLD_S = 1800              # a take-profit, stop or trail not hit by then sells on the clock
MAX_SLOTS = MAX_HOLD_S * 5     # slots read after the buy, to bound the read: 30 min at 0.2 s a slot, twice the chain's pace
MAX_POSITIONS = 1000           # a wallet's latest coins replayed, like stake_sweep
# forward-tested on paper beside every copy from 2026-10-10: the sweep's best on 8zkgFG over 3 days (tp100_sl50 +8.8%,
# hold2m +5.7%, both halves positive) and over all 21 followed wallets pooled (hold30s -3.9% against -5.1% selling with
# the wallet). Picked in-sample, best of 15: only copies made after the pick say whether any of it holds.
FORWARD = ("tp100_sl50", "hold2m", "hold30s")
FORWARD_MIN = 5                # closed copies of a wallet, per rule, before it gets a line of its own


def rule_spec(name: str) -> tuple[float | None, float | None, int]:
    """(take profit, stop loss, seconds held at most) of one TPSL or HOLDS rule: what the paper's and the live copies'
    own exits watch. KeyError for any other name."""
    if name in TPSL:
        return (*TPSL[name], MAX_HOLD_S)
    return None, None, HOLDS[name]


def forward_lines(db_path: str | Path, tx_cost_sol: float | None = None) -> list[str]:
    """The paper's own exits on the copies they closed, each next to the same copies sold with their wallet: all
    followed wallets pooled, then each with FORWARD_MIN+ closed a rule. Out-of-sample from 2026-10-10, unlike the sweep."""
    c = connect(db_path, readonly=True)
    try:
        costs = tx_costs(c) if tx_cost_sol is None else [[0, tx_cost_sol]]
        rows = [(w, rule, sol - pnl - 2 * tx_cost_at(costs, ts), pnl, wallet_pnl) for w, rule, sol, pnl, wallet_pnl, ts in c.execute(
            """SELECT x.wallet, substr(x.side, 3), x.sol, x.pnl, s.pnl, x.ts
               FROM pfills x LEFT JOIN pfills s ON s.wallet = x.wallet AND s.mint = x.mint AND s.side = 'sell'
               WHERE x.side LIKE 'x:%'""")]                  # the stake back: what was sold, less profit and the fees of its day
    finally:
        c.close()
    if not rows:
        return []

    def line(rs: list[tuple]) -> str:
        out = []
        for rule in sorted({r[1] for r in rs}, key=lambda n: FORWARD.index(n) if n in FORWARD else len(FORWARD)):
            mine = [r for r in rs if r[1] == rule]
            cost, both = sum(r[2] for r in mine), [r for r in mine if r[4] is not None]
            out.append(f"{rule} {_pct(sum(r[3] for r in mine) / cost if cost else None)} n{len(mine)} won "
                       f"{sum(r[3] > 0 for r in mine) / len(mine):.0%} (sold with the wallet "
                       f"{_pct(sum(r[4] for r in both) / sum(r[2] for r in both) if both else None)} on {len(both)} of them)")
        return ", ".join(out)

    by: dict[str, list[tuple]] = {}
    for r in rows:
        by.setdefault(r[0], []).append(r)
    return [f"exit forward all followed ({len(by)} wallets, since 2026-10-10): {line(rows)}",
            *(f"exit forward {w}: {line(rs)}" for w, rs in sorted(by.items())
              if len(rs) >= FORWARD_MIN * len({r[1] for r in rs}))]


def exit_pnls(c: sqlite3.Connection, wallet: int, mint: int, fb: int, t_in: int, buy_slots: int = PAPER_BUY_SLOTS,
              sell_slots: int = PAPER_SELL_SLOTS, stake_sol: float = PAPER_STAKE_SOL,
              tx_cost_sol: float = TX_COST_SOL) -> dict[str, float] | None:
    """SOL each exit rule made on one copy of `wallet`'s first buy of `mint` (slot `fb`, timestamp `t_in`);
    None when nothing traded before the copy could land."""
    landed = fb + buy_slots
    entry = _state_before(c, mint, landed)
    if not entry or entry[0] <= 0 or entry[1] <= 0:
        return None
    fee_in = _fee_rate(c, wallet, mint, 1, fb) or _fee_known(c, mint, landed)       # a PumpSwap buy logs ~0
    tokens = entry[1] - entry[0] * entry[1] / (entry[0] + stake_sol * LAMPORTS / (1 + fee_in))
    if tokens <= 0:
        return None
    fs = c.execute("SELECT MIN(slot) FROM trades WHERE wallet=? AND mint=? AND buy=0 AND slot >= ?", (wallet, mint, fb)).fetchone()[0]
    sold = _state_before(c, mint, fs + sell_slots) if fs is not None else _state_before(c, mint, None)
    out = {"wallet": copy_trade(entry, sold, stake_sol, tx_cost_sol, fee_in, _fee_rate(c, wallet, mint, 0, fs))}   # = copy_sim
    # every trade after our buy lands, up to the longest hold: what each rule watches
    path = c.execute("""SELECT slot, ts, vsol, vtok, buy, sol, fee FROM trades WHERE mint = ? AND slot >= ? AND slot < ? AND ts <= ?
                        ORDER BY slot, rowid""", (mint, landed, landed + MAX_SLOTS, t_in + MAX_HOLD_S)).fetchall()
    rate = fee0 = _fee_known(c, mint, landed)
    fee_at = []                                              # the fee a sale pays at each trade: the coin's last sell's, by then
    for r in path:
        if not r[4] and r[5] >= 10**7 and r[6] > 0:
            rate = r[6] / r[5]
        fee_at.append(rate)
    worth = [_value((r[2], r[3]), tokens, f) for r, f in zip(path, fee_at)]
    slots, times = [r[0] for r in path], [r[1] for r in path]

    def net(i: int | None) -> float:
        """Out on path[i]: the sale lands `sell_slots` later, at the state every trade before it left (None: nothing
        traded after the buy, out at the price it was bought at)."""
        if i is None or i < 0:
            return _value(entry, tokens, fee0) - stake_sol - 2 * tx_cost_sol
        j = bisect.bisect_left(slots, slots[i] + sell_slots) - 1
        return _value((path[j][2], path[j][3]), tokens, fee_at[j]) - stake_sol - 2 * tx_cost_sol

    last_by = lambda t: bisect.bisect_right(times, t) - 1   # noqa: E731 - the last trade at or before t
    end = last_by(t_in + MAX_HOLD_S)
    for name, secs in HOLDS.items():
        out[name] = net(last_by(t_in + secs))
    for name, (tp, sl) in TPSL.items():
        out[name] = net(next((i for i, w in enumerate(worth) if not (1 - sl) * stake_sol < w < (1 + tp) * stake_sol), end))
    for name, (drop, arm) in TRAILS.items():
        out[name] = net(_trail(worth, _value(entry, tokens, fee0), drop, arm, stake_sol, end))
    stop = next((i for i, w in enumerate(worth) if w <= (1 - WALLET_SL) * stake_sol and (fs is None or slots[i] < fs)), None)
    # ponytail: the stop is watched for MAX_HOLD_S only; a wallet that holds longer keeps its own exit past that
    out[f"wallet_sl{round(WALLET_SL * 100)}"] = net(stop) if stop is not None else out["wallet"]
    return out


def _trail(worth: list[float], start: float, drop: float, arm: float | None, stake_sol: float, end: int) -> int:
    """The first trade worth `drop` less than the best before it (from `start`, the worth bought), once the best has
    reached `arm` over the stake; `end` if none is."""
    best = start
    for i, w in enumerate(worth):
        best = max(best, w)
        if (arm is None or best >= (1 + arm) * stake_sol) and w <= (1 - drop) * best:
            return i
    return end


def exit_sweep(db_path: str | Path, buy_slots: int = PAPER_BUY_SLOTS, sell_slots: int = PAPER_SELL_SLOTS,
               stake_sol: float = PAPER_STAKE_SOL, tx_cost_sol: float = TX_COST_SOL, max_positions: int = MAX_POSITIONS) -> list[str]:
    """Every exit rule on the copies of each wallet the paper copies on every first buy (golden, and today's top
    snipers; not those followed only for buying past $100k, whose copies start there), then all of them pooled,
    best rule first."""
    c = connect(db_path, readonly=True)
    try:
        t0, t1 = c.execute("SELECT MIN(ts), MAX(ts) FROM mints").fetchone()
        if t0 is None:
            return []
        mid, out, pooled = (t0 + t1) / 2, [], []
        line = lambda rows: _rules_line(_rules_on(rows, stake_sol, tx_cost_sol, mid, lambda r: r["pnl"]), detail=True)   # noqa: E731
        wallets = c.execute("""SELECT f.wallet, w.id FROM follow f JOIN wallets w ON w.addr = f.wallet
                               WHERE f.golden_ever = 1 OR f.sniper_now = 1 ORDER BY f.wallet""").fetchall()
        for addr, wid in wallets:
            rows = []
            # t.ts is the first buy's own: SQLite takes a bare column from the row MIN() picked
            for mint, fb, t_in, mts in c.execute("""SELECT t.mint, MIN(t.slot), t.ts, m.ts FROM trades t JOIN mints m ON m.id = t.mint
                                                   WHERE t.wallet = ? AND t.buy = 1 AND m.creator != t.wallet
                                                   GROUP BY t.mint ORDER BY 2 DESC LIMIT ?""", (wid, max_positions)).fetchall():
                pnl = exit_pnls(c, wid, mint, fb, t_in, buy_slots, sell_slots, stake_sol, tx_cost_sol)
                if pnl:
                    rows.append({"ts": mts, "pnl": pnl})
            if rows:
                out.append(f"exit sweep {addr} ({len(rows)} copies): {line(rows)}")
                pooled += rows
        if pooled:
            out.insert(0, f"exit sweep all followed ({len(pooled)} copies of {len(out)} wallets, {stake_sol:g} SOL in "
                          f"{buy_slots} slots after the buy, out {sell_slots} after the trigger): {line(pooled)}")
        return out
    finally:
        c.close()
