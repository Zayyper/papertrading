"""A close look at the followed pump.fun wallets, from the collector's own tables: how each one trades, when it
gets in, whose coins it buys, and what copying it has really made next to what it made itself.

    python -m hl_screener pump dossier [<wallet> ...]    # default: every golden wallet and every wallet copied 5+ times

The collector logs the same lines once on every start: the page is behind a password, the container log is not.
Everything comes from the trade window the collector keeps (3 days), so a wallet's older history is not in it.
"""
from __future__ import annotations

import collections
import time
from pathlib import Path
from typing import Any

from .pumpfun import LAMPORTS, cohorts_of, connect, read_launches, save_meta

FRESH_CURVE_SOL = 30.0          # a new curve's virtual SOL: what is in it beyond this was bought


def _q(xs: list[float], p: float) -> float | None:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else None


def dossier(c, addr: str, crew: set[str], launches_of: collections.Counter) -> dict[str, Any] | None:
    row = c.execute("SELECT id FROM wallets WHERE addr = ?", (addr,)).fetchone()
    if not row:
        return None
    wid = row[0]
    toks: dict[str, dict[str, Any]] = {}
    hours: collections.Counter = collections.Counter()
    n_trades = 0
    for slot, ts, mint, mslot, creator, buy, sol, tok, fee, vsol, grad in c.execute(
            """SELECT t.slot, t.ts, m.addr, m.slot, w.addr, t.buy, t.sol, t.tok, t.fee, t.vsol, m.pool IS NOT NULL
               FROM trades t JOIN mints m ON m.id = t.mint LEFT JOIN wallets w ON w.id = m.creator
               WHERE t.wallet = ? ORDER BY t.slot""", (wid,)):
        n_trades += 1
        hours[time.gmtime(ts).tm_hour] += 1
        t = toks.setdefault(mint, {"cost": 0.0, "back": 0.0, "tin": 0, "tout": 0, "first": None, "first_ts": None, "last_ts": None,
                                   "launch": mslot, "creator": creator, "grad": bool(grad), "curve_sol": None})
        if buy:
            t["cost"] += (sol + fee) / LAMPORTS
            t["tin"] += tok
            if t["first"] is None:
                t["first"], t["first_ts"] = slot, ts
                before = (vsol - sol) / LAMPORTS - FRESH_CURVE_SOL if vsol else None
                t["curve_sol"] = before if before is not None and 0 <= before < 90 else None   # on the curve, not a pool
        else:
            t["back"] += (sol - fee) / LAMPORTS
            t["tout"] += tok
            t["last_ts"] = ts
    bought = {m: t for m, t in toks.items() if t["first"] is not None}
    if not bought:
        return {"wallet": addr, "trades": n_trades, "tokens": 0}
    closed = {m: t for m, t in bought.items() if t["tout"] >= 0.98 * t["tin"]}
    # tokens it sold beyond what it bought came from elsewhere (another of its wallets?): no profit is counted on them
    pnl = {m: t["back"] * min(1.0, t["tin"] / t["tout"]) - t["cost"] for m, t in closed.items()}
    oversold = sum(1 for t in closed.values() if t["tout"] > 1.02 * t["tin"])
    buys = [(sol + fee) / LAMPORTS for sol, fee in c.execute("SELECT sol, fee FROM trades WHERE wallet = ? AND buy = 1", (wid,))]
    gains = sorted((p for p in pnl.values() if p > 0), reverse=True)
    blocks: dict[str, float] = collections.defaultdict(float)
    for m, p in pnl.items():
        ts = closed[m]["first_ts"]
        blocks[time.strftime("%m-%d %Hh", time.gmtime(ts - ts % 43_200))] += p
    first_ts = min(t["first_ts"] for t in bought.values())
    span_h = max(1e-9, (max(t["last_ts"] or t["first_ts"] for t in bought.values()) - first_ts) / 3600)
    offsets = [t["first"] - t["launch"] for t in bought.values() if t["launch"] is not None]
    creators = collections.Counter(t["creator"] for t in bought.values() if t["creator"])
    sniped = [(cr, n) for (cr, n) in c.execute("SELECT creator, COUNT(*) FROM launches WHERE ',' || buyers || ',' LIKE ? GROUP BY creator",
                                                (f"%,{wid},%",))]
    # what copying it made, next to what it made itself on the same coins
    cp = c.execute("SELECT mint, side, trigger_slot, land_slot, sol, slip_bps, pnl, timed_out FROM pfills WHERE wallet = ? ORDER BY id", (addr,)).fetchall()
    cbuys, csells = [r for r in cp if r[1] == "buy"], [r for r in cp if r[1] == "sell"]
    same = [m for m in {r[0] for r in cbuys} if m in closed]
    return {
        "wallet": addr, "trades": n_trades, "tokens": len(bought), "span_h": span_h, "per_hour": n_trades / span_h,
        "closed": len(closed), "open": len(bought) - len(closed), "pnl_sol": sum(pnl.values()), "oversold": oversold,
        "buy_sol": [_q(buys, p) for p in (0.5, 0.9)] + [max(buys) if buys else None],
        "cost_sol": sum(t["cost"] for t in closed.values()), "won": sum(p > 0 for p in pnl.values()) / len(pnl) if pnl else None,
        "median_sol": _q(list(pnl.values()), 0.5), "top3_share": sum(gains[:3]) / sum(gains) if gains else None,
        "blocks": dict(sorted(blocks.items())), "busiest_hours": [h for h, _ in hours.most_common(3)],
        "offset_share": {k: sum(lo <= o <= hi for o in offsets) / len(offsets) for k, (lo, hi) in
                         {"0-1": (0, 1), "2-10": (2, 10), "11-150": (11, 150), "later": (151, 10**12)}.items()} if offsets else {},
        "curve_sol_p50": _q([t["curve_sol"] for t in bought.values() if t["curve_sol"] is not None], 0.5),
        "hold_s": [_q([t["last_ts"] - t["first_ts"] for t in closed.values() if t["last_ts"]], p) for p in (0.1, 0.5, 0.9)],
        "creators": len(creators), "top_creators": creators.most_common(3),
        "repeat_maker_share": sum(1 for t in bought.values() if launches_of[t["creator"]] >= 3) / len(bought),
        "crew_share": sum(1 for m in bought if m in crew) / len(bought),
        "graduated_share": sum(t["grad"] for t in bought.values()) / len(bought),
        "launched": launches_of[addr], "sniped": sorted(sniped, key=lambda x: -x[1])[:3], "sniped_total": sum(n for _, n in sniped),
        "copies": len(cbuys), "copy_closed": len(csells), "copy_cost": sum(r[4] for r in cbuys), "copy_pnl": sum(r[6] or 0 for r in csells),
        "copy_won": sum((r[6] or 0) > 0 for r in csells) / len(csells) if csells else None,
        "delay_slots": _q([r[3] - r[2] for r in cp], 0.5), "slip_buy_bps": _q([r[5] for r in cbuys if r[5] is not None], 0.5),
        "slip_sell_bps": _q([r[5] for r in csells if r[5] is not None], 0.5), "timed_out": sum(r[7] or 0 for r in cp),
        "same_tokens": len(same), "own_on_same": sum(pnl[m] for m in same), "own_cost_on_same": sum(closed[m]["cost"] for m in same),
        "mints": set(bought),
    }


def _pct(x: float | None) -> str:
    return f"{x:+.1%}" if x is not None else "n/a"


def _num(x: float | None, nd: int = 0) -> str:
    return "n/a" if x is None else f"{x:.{nd}f}"


def lines(d: dict[str, Any], status: str = "") -> list[str]:
    a = d["wallet"]
    if not d.get("tokens"):
        return [f"dossier {a}{status}: no buys in the collector's window ({d.get('trades', 0)} trades)"]
    return [
        f"dossier {a}{status}",
        f"  itself: {d['trades']} trades on {d['tokens']} coins over {d['span_h']:.0f} h ({d['per_hour']:.1f}/h), {d['closed']} closed "
        f"{d['pnl_sol']:+.3f} SOL on {d['cost_sol']:.2f} SOL spent ({_pct(d['pnl_sol'] / d['cost_sol'] if d['cost_sol'] else None)}), "
        f"won {_pct(d['won'])}, median {d['median_sol']:+.4f} SOL, best 3 coins = {_pct(d['top3_share'])} of its gains, {d['open']} still held, "
        f"{d['oversold']} sold more than it bought (not counted) | buys p50/p90/max {'/'.join(_num(b, 2) for b in d['buy_sol'])} SOL",
        "  by 12 h: " + ", ".join(f"{k} {v:+.2f}" for k, v in d["blocks"].items()),
        f"  entry: {', '.join(f'{k} slots after launch {v:.0%}' for k, v in d['offset_share'].items())} | SOL already in the curve "
        f"p50 {_num(d['curve_sol_p50'], 2)} | holds p10/p50/p90 {'/'.join(_num(h) + 's' for h in d['hold_s'])} | "
        f"busiest hours UTC {d['busiest_hours']}",
        f"  whose coins: {d['creators']} makers, {d['repeat_maker_share']:.0%} of coins from repeat makers, {d['crew_share']:.0%} sniped by a "
        f"known crew, {d['graduated_share']:.0%} graduated | it launched {d['launched']} | it sniped {d['sniped_total']} launches"
        + (f", most from {', '.join(f'{c[:6]}…({n})' for c, n in d['sniped'])}" if d["sniped"] else ""),
        f"  copying it: {d['copies']} copies, {d['copy_closed']} closed, {d['copy_pnl']:+.3f} SOL on {d['copy_cost']:.2f} SOL "
        f"({_pct(d['copy_pnl'] / d['copy_cost'] if d['copy_cost'] else None)}), won {_pct(d['copy_won'])}, delay p50 {_num(d['delay_slots'])} slots, "
        f"slippage p50 buy {_num(d['slip_buy_bps'])} / sell {_num(d['slip_sell_bps'])} bps, {d['timed_out']} landed on a quiet coin | "
        f"the wallet on those same {d['same_tokens']} coins: {d['own_on_same']:+.3f} SOL "
        f"({_pct(d['own_on_same'] / d['own_cost_on_same'] if d['own_cost_on_same'] else None)})",
    ]


def dossiers(db_path: str | Path, wallets: list[str] | None = None, min_copies: int = 5) -> list[str]:
    """Lines for the given wallets, or for every golden wallet and every wallet copied `min_copies`+ times."""
    c = connect(db_path, readonly=True)
    try:
        follow = {w: (g, ge, sn, me) for w, g, ge, sn, me in
                  c.execute("SELECT wallet, golden_now, golden_ever, sniper_now, mature_ever FROM follow")}
        if not wallets:
            counts = dict(c.execute("SELECT wallet, COUNT(*) FROM pfills WHERE side = 'buy' GROUP BY wallet"))
            wallets = sorted(w for w, (_, ge, _, me) in follow.items() if ge or me or counts.get(w, 0) >= min_copies)
        rows = read_launches(c)
        crew = {r["mint"] for r in cohorts_of(rows)["crew"]}
        launches_of = collections.Counter(r["creator"] for r in rows)
        found = [d for d in (dossier(c, w, crew, launches_of) for w in wallets) if d]
    finally:
        c.close()
    out: list[str] = []
    for d in found:
        g, ge, sn, me = follow.get(d["wallet"], (0, 0, 0, 0))
        status = (" (golden now)" if g else " (was golden, still followed)" if ge else " (a top sniper now)" if sn
                  else " (buys past $100k)" if me else " (was a top sniper)")
        out += lines(d, status)
        shared = sorted(((o["wallet"], len(d["mints"] & o["mints"])) for o in found
                         if o is not d and d.get("mints") and o.get("mints")), key=lambda x: -x[1])
        if shared and shared[0][1]:
            out.append("  coins in common with: " + ", ".join(f"{w[:6]}…{w[-4:]} ({n})" for w, n in shared[:4] if n))
    save_meta(db_path, "dossiers", {"at": int(time.time()), "lines": out})
    return out
