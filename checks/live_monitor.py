"""Tonight's live copies, watched from here: every transaction of our live wallet and of the leaders it copies, one line
each as it lands, with the wallet's balance, the copies still open and what they cost, the realized P&L per coin and in
total, the failed transactions and the SOL they burned. A leader selling a coin we still hold is called out.

    python checks/live_monitor.py --wallet <our live wallet> [--leader <address> ...] [--rpc URL] [--every 20]
                                  [--since-minutes N] [--heartbeat 300]        (Ctrl-C stops it, with a last summary)

Read-only: getSignaturesForAddress, getTransaction and getBalance, nothing else; no key, nothing signed or sent. It
starts from now; --since-minutes replays that much history first, so copies opened before it started have a cost.
Our P&L is the wallet's own SOL change over a coin's buy and sell: fees, tips and the token account's rent included
(the rent comes back when the sell closes the account). Times are UTC, the transactions' block times.
"""
from __future__ import annotations

import argparse
import base64
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from hl_screener.pumpfun import _B58, D_BUY, D_SELL, D_TRADE, _no_key, b58, parse_amm_trade, parse_trade  # noqa: E402
from hl_screener.pumptx import (AMM_PROGRAM, BUY_EXACT_QUOTE_IN, BUY_EXACT_SOL_IN, PUBLIC_RPC, PUMP_PROGRAM, SELL,  # noqa: E402
                                TIP_ACCOUNTS, WSOL, Rpc, own_trade, sim_error)

TX_GAP_S = 1.2                  # between two getTransaction calls: the public RPC allows about 10 per 10 s
BACKOFF_S = (3, 6, 10)          # pauses before the retries of a failed call (429, 413, timeout...), then the next round
READ_TRIES = 5                  # rounds a transaction the RPC does not return yet is asked for
ALERT_AFTER_S = (30, 120, 600)  # a leader sold a coin we still hold: said this long after its sell
LEADER_SOLD_KEEP_S = 3600       # a leader's sell of a coin we do not hold is remembered this long (our buy may land later)
LAMPORTS = 1e9
TOKEN_UNIT = 1e6                # pump.fun coins have 6 decimals
BUY = bytes.fromhex("66063d1201daebea")   # sha256("global:buy")[:8], both programs
# the v1 trade instructions (our copies send these): (program, discriminator) -> (side, where the coin's mint is in its accounts)
TRADE_IXS = {(PUMP_PROGRAM, BUY): ("buy", 2), (PUMP_PROGRAM, BUY_EXACT_SOL_IN): ("buy", 2), (PUMP_PROGRAM, SELL): ("sell", 2),
             (AMM_PROGRAM, BUY): ("buy", 3), (AMM_PROGRAM, BUY_EXACT_QUOTE_IN): ("buy", 3), (AMM_PROGRAM, SELL): ("sell", 3)}


def say(line: str) -> None:
    print(line, flush=True)


def _ts(t: float) -> str:
    return time.strftime("%m-%d %H:%M:%S", time.gmtime(t))


def b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        n = n * 58 + _B58.index(ch)
    return bytes(len(s) - len(s.lstrip("1"))) + n.to_bytes((n.bit_length() + 7) // 8, "big")


# ---------------------------------------------------------------------------
# one transaction
# ---------------------------------------------------------------------------
def keys_of(tx: dict) -> list[str]:
    loaded = tx["meta"].get("loadedAddresses") or {}
    return tx["transaction"]["message"]["accountKeys"] + loaded.get("writable", []) + loaded.get("readonly", [])


def _holdings(meta: dict, addr: str) -> dict[str, list[int]]:
    """mint -> [raw units before, after] that `addr` owns, any token account (some leaders use others than the ATA)."""
    out: dict[str, list[int]] = {}
    for when, field in ((0, "preTokenBalances"), (1, "postTokenBalances")):
        for b in meta.get(field) or []:
            if b.get("owner") == addr and b["mint"] != WSOL:
                out.setdefault(b["mint"], [0, 0])[when] += int(b["uiTokenAmount"]["amount"])
    return out


def _event_coin(logs: list[str], addr: str, meta: dict, keys: list[str]) -> tuple[str | None, bool]:
    """The coin of `addr`'s own trade event, and whether it is quoted in SOL (the collector copies no other coin): a
    curve event names both; a PumpSwap event names the trader's base and quote token accounts, whose mints the token
    balances give."""
    accounts = {keys[b["accountIndex"]]: b["mint"] for b in (meta.get("preTokenBalances") or []) + (meta.get("postTokenBalances") or [])}
    for line in logs:
        if not line.startswith("Program data: "):
            continue
        try:
            b = base64.b64decode(line[14:])
        except ValueError:
            continue
        if b[:8] == D_TRADE:
            e = parse_trade(b)
            if e and b58(e["user"]) == addr:
                return b58(e["mint"]), e["sol_quote"]
        elif b[:8] in (D_BUY, D_SELL):
            e = parse_amm_trade(b)
            if e and b58(e["user"]) == addr:
                return accounts.get(b58(b[184:216])), accounts.get(b58(b[216:248]), WSOL) == WSOL   # user base, quote accounts
    return None, True


def _trade_ix(tx: dict, keys: list[str]) -> tuple[str | None, str | None]:
    """(side, mint) of the first v1 pump.fun or PumpSwap trade instruction, top-level or inner: what a failed one tried."""
    inner = [ix for group in tx["meta"].get("innerInstructions") or [] for ix in group["instructions"]]
    for ix in tx["transaction"]["message"]["instructions"] + inner:
        program = keys[ix["programIdIndex"]]
        if program not in (PUMP_PROGRAM, AMM_PROGRAM):
            continue
        hit = TRADE_IXS.get((program, b58decode(ix["data"])[:8]))
        if hit and len(ix["accounts"]) > hit[1]:
            return hit[0], keys[ix["accounts"][hit[1]]]
    return None, None


def read_tx(tx: dict, addr: str) -> dict:
    """A getTransaction answer as `addr` lived it. kind: BUY or SELL (its own pump.fun or PumpSwap trade event), FAILED or
    OTHER. sol: what the trade cost, fees in (buy), or paid, fees out (sell), else the size of the wallet's SOL change;
    in the quote token's units when the coin is not quoted in SOL (sol_quoted false).
    tok: raw token units traded; left: what it holds of the coin after; net: its SOL change; paid: network fee and tip
    (Helius Sender's tip accounts), when it signed."""
    meta, keys = tx["meta"], keys_of(tx)
    moved = lambda i: meta["postBalances"][i] - meta["preBalances"][i]           # noqa: E731
    net = moved(keys.index(addr)) if addr in keys else 0
    paid = meta["fee"] + sum(max(0, moved(i)) for i, k in enumerate(keys) if k in TIP_ACCOUNTS) if keys[0] == addr else 0
    logs = meta.get("logMessages") or []
    got = own_trade(logs, addr)
    side, ix_mint = _trade_ix(tx, keys)
    held = _holdings(meta, addr)
    ev_mint, sol_quoted = _event_coin(logs, addr, meta, keys)
    mint = (ev_mint or ix_mint
            or max(held, key=lambda m: abs(held[m][1] - held[m][0]), default=None))
    before, after = held.get(mint, [0, 0])
    if meta.get("err") is not None:
        kind = "FAILED" if keys[0] == addr else "OTHER"         # another payer's failure changed nothing of ours
    else:
        kind = ("BUY" if got["buy"] else "SELL") if got else "OTHER"
    sol = (got["sol"] + got["fee"] if got["buy"] else got["sol"] - got["fee"]) if got else abs(net)
    return {"sig": tx["transaction"]["signatures"][0], "t": tx.get("blockTime") or int(time.time()), "kind": kind,
            "side": ("buy" if got["buy"] else "sell") if got else side, "mint": mint, "sol": sol / LAMPORTS, "sol_quoted": sol_quoted,
            "tok": got.get("tok") or abs(after - before), "left": after, "net": net / LAMPORTS, "paid": paid / LAMPORTS,
            "err": sim_error(meta) if kind == "FAILED" else None}


def ours_line(d: dict, note: str = "") -> str:
    head = f"{_ts(d['t'])} OURS   {d['kind']:<6} {d['mint'] or '-':<44}"
    if d["kind"] == "FAILED":
        body = f" {d['side'] or '?':<4} fee+tip {d['paid']:.6f}  wallet {d['net']:+.6f} | {d['err']}"
    elif d["kind"] == "OTHER":
        body = f" wallet {d['net']:+.5f} SOL  fee+tip {d['paid']:.6f}"
    else:
        amount = f"{d['sol']:.5f} SOL" if d["sol_quoted"] else "(not SOL-quoted)"
        body = f" {amount} {d['tok'] / TOKEN_UNIT:>13,.0f} tok  fee+tip {d['paid']:.6f}  wallet {d['net']:+.5f}"
    return f"{head}{body}{note} | {d['sig'][:16]}"


def leader_line(name: str, d: dict, note: str = "") -> str:
    what = ((f" {d['sol']:.4f} SOL" if d["sol_quoted"] else " not SOL-quoted: never copied") if d["kind"] in ("BUY", "SELL") else
            f" wallet {d['net']:+.4f} SOL" if d["kind"] == "OTHER" else "")
    return f"{_ts(d['t'])} {name:<6} {d['kind']:<6} {d['mint'] or '-':<44}{what}{note} | {d['sig'][:16]}"


# ---------------------------------------------------------------------------
# the book: open copies, realized P&L, failures, leaders' sells
# ---------------------------------------------------------------------------
class Book:
    def __init__(self) -> None:
        self.pos: dict[str, dict] = {}            # mint -> {"tok": raw units, "cost": SOL, "t": first buy}
        self.closed: list[tuple[str, float, float]] = []   # (mint, pnl SOL, cost SOL), one per sell
        self.failed, self.burned = 0, 0.0
        self.leader_sold: dict[str, dict] = {}    # mint -> {"name", "t", "said": alerts given}
        self.counts: dict[str, dict[str, int]] = {}

    def ours(self, d: dict) -> str:
        """Book one of our transactions; what it adds to its line."""
        if d["kind"] == "FAILED":
            self.failed += 1
            self.burned -= d["net"]
            return ""
        if d["kind"] == "BUY":
            p = self.pos.setdefault(d["mint"], {"tok": 0, "cost": 0.0, "t": d["t"]})
            p["tok"], p["cost"] = d["left"] or p["tok"] + d["tok"], p["cost"] - d["net"]
            return f" | open {len(self.pos)}"
        if d["kind"] != "SELL":
            return ""
        p = self.pos.get(d["mint"])
        if p is None:
            return " | bought before the monitor started: no P&L (see --since-minutes)"
        part = d["tok"] / (d["tok"] + d["left"]) if d["left"] else 1.0
        cost = p["cost"] * part
        pnl = d["net"] - cost
        self.closed.append((d["mint"], pnl, cost))
        held = d["t"] - p["t"]
        if d["left"]:
            p["tok"], p["cost"] = d["left"], p["cost"] - cost
        else:
            del self.pos[d["mint"]]
        ls = self.leader_sold.pop(d["mint"], None) if not d["left"] else self.leader_sold.get(d["mint"])
        lag = f", {d['t'] - ls['t']} s after {ls['name']} sold" if ls else ""
        return (f" | pnl {pnl:+.4f} SOL ({pnl / cost:+.1%}) held {held} s{lag}" if cost else f" | pnl {pnl:+.4f} SOL{lag}") \
            + (f", {d['left'] / TOKEN_UNIT:,.0f} tok left" if d["left"] else "")

    def leader(self, name: str, d: dict) -> str:
        """Book one of a leader's transactions; what it adds to its line."""
        c = self.counts.setdefault(name, {})
        c[d["kind"]] = c.get(d["kind"], 0) + 1
        if d["kind"] != "SELL" or not d["mint"]:
            return ""
        old, held = self.leader_sold.get(d["mint"]), self.pos.get(d["mint"])
        if old is None or held is None or old["t"] < held["t"] - 60:   # the first sell after our buy keeps the clock
            self.leader_sold[d["mint"]] = {"name": name, "t": d["t"], "said": 0}
        return " | WE HOLD IT" if held else ""

    def alerts(self, now: float) -> list[str]:
        """'leader sold X, we still hold it after N s': ALERT_AFTER_S after its sell, for each coin we still hold."""
        out = []
        for mint, s in self.leader_sold.items():
            p = self.pos.get(mint)
            if p is None or s["t"] < p["t"] - 60 or s["said"] >= len(ALERT_AFTER_S) or now - s["t"] < ALERT_AFTER_S[s["said"]]:
                continue                              # (a sell older than our buy was not the one our copy follows)
            s["said"] += 1
            out.append(f"{_ts(now)} ALERT  leader {s['name']} sold {mint}, we still hold it after {now - s['t']:.0f} s"
                       f" (cost {p['cost']:.4f} SOL)")
        self.leader_sold = {m: s for m, s in self.leader_sold.items() if m in self.pos or now - s["t"] < LEADER_SOLD_KEEP_S}
        return out

    def summary(self, now: float) -> str:
        opened = ", ".join(f"{m[:8]} {p['cost']:.4f} SOL {now - p['t']:.0f} s" for m, p in self.pos.items())
        pnl = sum(x for _, x, _ in self.closed)
        last = ", ".join(f"{m[:8]} {x:+.4f}" for m, x, _ in self.closed[-5:])
        leaders = ", ".join(f"{n} " + "/".join(f"{k} {v}" for k, v in sorted(c.items())) for n, c in self.counts.items())
        return (f"open {len(self.pos)}" + (f" ({opened})" if opened else "")
                + f" | sold {len(self.closed)} ({sum(1 for _, x, _ in self.closed if x > 0)} up), realized {pnl:+.4f} SOL"
                + (f" (last: {last})" if last else "") + f" | failed {self.failed}, burned {self.burned:.6f} SOL"
                + (f" | leaders: {leaders}" if leaders else ""))


# ---------------------------------------------------------------------------
# the RPC and the loop
# ---------------------------------------------------------------------------
class Chain:
    """The RPC, paced: getTransaction at most every TX_GAP_S; any failed call (429, 413, timeout...) is waited out and
    retried BACKOFF_S, then it raises and the caller leaves it for the next round."""

    def __init__(self, url: str, rpc: Rpc | None = None) -> None:
        self.rpc, self.last, self.retries = rpc or Rpc(url, timeout=20), 0.0, 0

    def ask(self, method: str, params: list):
        err = ""
        for pause in (*BACKOFF_S, None):
            if method == "getTransaction":
                time.sleep(max(0.0, self.last + TX_GAP_S - time.time()))
                self.last = time.time()
            try:
                return self.rpc.call(method, params)
            except Exception as e:  # noqa: BLE001 - rate limits, timeouts, a node behind: all waited out
                err = _no_key(f"{type(e).__name__}: {e}")[:160]
            if pause is not None:
                self.retries += 1
                time.sleep(pause)
        raise RuntimeError(f"{method} failed {len(BACKOFF_S) + 1} times: {err}")

    def signatures(self, addr: str, until: str | None = None, since: float | None = None) -> list[dict]:
        """Signatures of `addr` newer than `until`, or than the unix time `since`: newest first."""
        out: list[dict] = []
        before = None
        while True:
            opts = {"limit": 1000, "commitment": "confirmed", **({"until": until} if until else {}),
                    **({"before": before} if before else {})}
            page = self.ask("getSignaturesForAddress", [addr, opts])
            out += page
            if len(page) < 1000 or (since and (page[-1].get("blockTime") or 0) < since):
                break
            before = page[-1]["signature"]
        return [s for s in out if not since or (s.get("blockTime") or time.time()) >= since]


class Watch:
    def __init__(self, chain: Chain, wallet: str, leaders: list[str], out=say) -> None:
        self.chain, self.wallet, self.say = chain, wallet, out
        self.names = {wallet: "OURS", **{a: a[:6] for a in leaders if a != wallet}}
        self.book = Book()
        self.cursors: dict[str, str] = {}         # address -> newest signature seen, "" when it had none
        self.todo: list[tuple[int, str, dict, int]] = []   # (slot, address, signature info, rounds tried)
        self.balance: float | None = None
        self.start_balance: float | None = None

    def begin(self, since: float | None = None) -> None:
        for addr in self.names:
            try:
                self._begin(addr, since)
            except Exception as e:  # noqa: BLE001 - tried again at the next round, from then on
                self._warn(f"starting on {self.names[addr]}: {e}")

    def _begin(self, addr: str, since: float | None) -> None:
        sigs = self.chain.signatures(addr, since=since) if since else []
        newest = sigs or self.chain.ask("getSignaturesForAddress", [addr, {"limit": 1, "commitment": "confirmed"}])
        self.cursors[addr] = newest[0]["signature"] if newest else ""
        self._queue(addr, sigs)

    def _queue(self, addr: str, sigs: list[dict]) -> None:
        self.todo += [(s.get("slot") or 0, addr, s, 0) for s in reversed(sigs)]

    def _warn(self, what: str) -> None:
        self.say(f"{_ts(time.time())} WARN   {_no_key(str(what))[:200]}")

    def round(self) -> None:
        """The balance, new signatures of every address, their transactions in slot order, the alerts."""
        try:
            self.balance = self.chain.ask("getBalance", [self.wallet, {"commitment": "confirmed"}])["value"] / LAMPORTS
            self.start_balance = self.balance if self.start_balance is None else self.start_balance
        except Exception as e:  # noqa: BLE001
            self._warn(f"reading the balance: {e}")
        for addr in self.names:
            try:
                if addr not in self.cursors:
                    self._begin(addr, None)
                    continue
                sigs = self.chain.signatures(addr, until=self.cursors[addr] or None)
                if sigs:
                    self.cursors[addr] = sigs[0]["signature"]
                self._queue(addr, sigs)
            except Exception as e:  # noqa: BLE001
                self._warn(f"reading {self.names[addr]}'s signatures: {e}")
        todo, self.todo = sorted(self.todo, key=lambda x: x[0]), []
        for item in todo:
            self._read(*item)
        for line in self.book.alerts(time.time()):
            self.say(line)

    def _read(self, slot: int, addr: str, s: dict, tries: int) -> None:
        name, ours = self.names[addr], addr == self.wallet
        if s.get("err") is not None and not ours:     # a leader's failed transaction: not worth a getTransaction
            d = {"t": s.get("blockTime") or time.time(), "kind": "FAILED", "mint": None, "sig": s["signature"]}
            self.say(leader_line(name, d, self.book.leader(name, d)))
            return
        try:
            tx = self.chain.ask("getTransaction", [s["signature"], {"encoding": "json", "maxSupportedTransactionVersion": 1,
                                                                    "commitment": "confirmed"}])
            if tx is None:
                raise RuntimeError("not returned yet")
            d = read_tx(tx, addr)
        except Exception as e:  # noqa: BLE001
            if tries + 1 < READ_TRIES:
                self.todo.append((slot, addr, s, tries + 1))
            else:
                self._warn(f"gave up on {name}'s transaction {s['signature']} after {READ_TRIES} rounds: {e}")
            return
        self.say(ours_line(d, self.book.ours(d)) if ours else leader_line(name, d, self.book.leader(name, d)))

    def status(self) -> str:
        now = time.time()
        bal = "?" if self.balance is None else f"{self.balance:.4f} SOL ({self.balance - self.start_balance:+.4f} since start)"
        return f"{_ts(now)} STATUS balance {bal} | {self.book.summary(now)} | rpc retries {self.chain.retries}"


def _address(s: str) -> str:
    if not 32 <= len(s) <= 44 or any(ch not in _B58 for ch in s):
        raise argparse.ArgumentTypeError("not a Solana address (32-44 base58 characters); never paste a secret key here")
    return s


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Read-only watch of the live wallet and its leaders.")
    ap.add_argument("--wallet", required=True, type=_address, help="our live wallet's address (never its key)")
    ap.add_argument("--leader", action="append", default=[], type=_address, help="a copied wallet; repeat for several")
    ap.add_argument("--rpc", default=PUBLIC_RPC)
    ap.add_argument("--every", type=float, default=20.0, help="seconds between polls")
    ap.add_argument("--since-minutes", type=float, default=None, help="replay this much history first")
    ap.add_argument("--heartbeat", type=float, default=300.0, help="seconds between status lines")
    a = ap.parse_args(argv)
    w = Watch(Chain(a.rpc), a.wallet, a.leader)
    say(f"{_ts(time.time())} START  watching OURS {a.wallet}" + "".join(f", {n} {x}" for x, n in w.names.items() if n != "OURS")
        + f" | rpc {_no_key(a.rpc)} | every {a.every:.0f} s | times UTC | read-only")
    beat = 0.0
    try:
        w.begin(time.time() - a.since_minutes * 60 if a.since_minutes else None)
        while True:
            t0 = time.time()
            try:
                w.round()
                if t0 - beat >= a.heartbeat:
                    say(w.status())
                    beat = t0
            except Exception as e:  # noqa: BLE001 - the watch never stops on its own
                w._warn(f"round failed: {type(e).__name__}: {e}")
            time.sleep(max(0.0, t0 + a.every - time.time()))
    except KeyboardInterrupt:
        say(w.status())
        for mint, pnl, cost in w.book.closed:
            say(f"  sold {mint} {pnl:+.4f} SOL" + (f" ({pnl / cost:+.1%})" if cost else ""))


if __name__ == "__main__":
    main()
