"""Live copies on pump.fun and PumpSwap: the paper follower's rule, with real transactions.

A chosen wallet's first buy of a coin is copied with `stake_sol`, and the copy is sold when that wallet first sells:
PaperFollow's rule, so the two compare copy for copy. PUMP_LIVE picks the mode:

    dry   (the default) each copy's buy is built and simulated by the RPC the moment it would be sent, on the chain's
          newest state: no key, nothing sent. It says whether the transaction would have gone through, what it would
          have bought next to the paper copy, and how fast the tool had it ready.
    live  signed with PUMP_LIVE_KEY and sent through PUMP_LIVE_SEND (Helius Sender and Jito by default). Only
          PUMP_LIVE_WALLETS are copied, at most PUMP_LIVE_MAX_OPEN at a time, and new copies stop for the UTC day once
          PUMP_LIVE_DAY_LOSS_SOL is lost. Sells always go out: up to three tries, the last one at any price.
    off   nothing.

The key is read from the server's environment in live mode only, where its owner put it: it never reaches the
database, the log or the page. One copy per coin at a time: two wallets buying the same coin would share one token
account, and neither sell would know its part. Transactions: pumptx.
"""
from __future__ import annotations

import logging
import os
import random
import time
from dataclasses import dataclass
from typing import Any

from .pumptx import (AMM_GLOBAL_CONFIG, AMM_LISTS, PUBLIC_RPC, PUMP_GLOBAL, PUMP_LISTS, TIP_ACCOUNTS, TIP_LAMPORTS, Rpc,
                     buy_ixs, coin_of, compose, fresh_coin, own_trade, parse_global, sell_ixs, sim_error, sol_for, tokens_for)

log = logging.getLogger(__name__)

SENDERS = ("https://sender.helius-rpc.com/fast",                               # keyless, free, needs the 0.001 SOL tip
           "https://frankfurt.mainnet.block-engine.jito.wtf/api/v1/transactions")  # keyless, 1 a second per IP
LIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS lorders (id INTEGER PRIMARY KEY, mode TEXT, wallet TEXT, mint TEXT, side TEXT, venue TEXT,
    trigger_slot INTEGER, seen REAL, ready REAL, done REAL, slot INTEGER, sig TEXT, status TEXT, err TEXT, sol REAL,
    tok REAL, want REAL, units INTEGER, pnl REAL, tries INTEGER DEFAULT 1);
CREATE INDEX IF NOT EXISTS ix_lorders ON lorders(wallet, mint);
CREATE INDEX IF NOT EXISTS ix_lorders_day ON lorders(mode, side, done);
CREATE TABLE IF NOT EXISTS lpos (mint TEXT PRIMARY KEY, wallet TEXT, tok INTEGER, cost REAL, opened INTEGER, stuck INTEGER DEFAULT 0);
"""
FILL_WAIT_S = 15         # a sent transaction the feed has not shown by then is looked up on the chain
LOOK_EVERY_S = 5         # and again this often (getTransaction: 10 calls per 10 s on the public RPC)
EXPIRE_S = 90            # a blockhash lasts 60-90 s: a transaction still unknown after this never landed
SELL_TRIES = 3           # the last try takes whatever the coin pays and leaves the token account open
MAX_HOLD_S = 12 * 3600   # a copy whose wallet never sells (it moved its tokens?) is sold after this long
RESERVE_SOL = 0.02       # left in the wallet for fees and new token accounts' rent
LISTS_TTL_S = 3600       # fee and buyback recipient lists, re-read this often


@dataclass(frozen=True)
class LiveCfg:
    mode: str = "dry"                          # off | dry | live
    wallets: frozenset[str] = frozenset()      # whom to copy; dry with none set: every golden wallet
    stake_sol: float = 0.25
    max_open: int = 3                          # copies open or on their way at once
    day_loss_sol: float = 0.5                  # realized loss in a UTC day that stops new copies until the next
    buy_slip: float = 0.25                     # a buy fails rather than get this much fewer tokens than priced
    sell_slip: float = 0.5                     # a sell takes up to this much less SOL than priced: getting out comes first
    rpc_url: str = PUBLIC_RPC                  # reads, simulations, blockhashes, lookups
    send_urls: tuple[str, ...] = SENDERS
    sim_signer: str | None = None              # dry: simulate as this address (a funded wallet), else as the copied wallet

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "LiveCfg":
        env = os.environ if env is None else env
        mode = (env.get("PUMP_LIVE") or "dry").strip().lower()
        if mode not in ("off", "dry", "live"):
            raise ValueError(f"PUMP_LIVE must be off, dry or live, not {mode!r}")
        listed = lambda k: tuple(x.strip() for x in (env.get(k) or "").split(",") if x.strip())    # noqa: E731
        num = lambda k, d: float(env.get(k) or d)                                                  # noqa: E731
        cfg = cls(mode=mode, wallets=frozenset(listed("PUMP_LIVE_WALLETS")), stake_sol=num("PUMP_LIVE_STAKE_SOL", 0.25),
                  max_open=int(num("PUMP_LIVE_MAX_OPEN", 3)), day_loss_sol=num("PUMP_LIVE_DAY_LOSS_SOL", 0.5),
                  buy_slip=num("PUMP_LIVE_BUY_SLIP", 0.25), sell_slip=num("PUMP_LIVE_SELL_SLIP", 0.5),
                  rpc_url=env.get("PUMP_LIVE_RPC") or PUBLIC_RPC, send_urls=listed("PUMP_LIVE_SEND") or SENDERS,
                  sim_signer=env.get("PUMP_LIVE_SIM_SIGNER") or None)
        if not 0 < cfg.stake_sol <= 5 or not 0 <= cfg.buy_slip < 1 or not 0 <= cfg.sell_slip < 1 or cfg.max_open < 1 \
                or cfg.day_loss_sol <= 0:
            raise ValueError("PUMP_LIVE_STAKE_SOL must be in (0, 5], the slippages in [0, 1), PUMP_LIVE_MAX_OPEN at least 1 "
                             "and PUMP_LIVE_DAY_LOSS_SOL above 0")
        if mode == "live" and not ((env.get("PUMP_LIVE_KEY") or "").strip() and cfg.wallets):
            raise ValueError("PUMP_LIVE=live needs PUMP_LIVE_KEY and PUMP_LIVE_WALLETS")
        return cfg


def load_keypair(env: dict[str, str] | None = None):
    """The signing key from PUMP_LIVE_KEY, turned into a Keypair at once: the text itself is never kept on anything that
    could be printed, saved or logged. A malformed key is reported without echoing it."""
    from solders.keypair import Keypair
    env = os.environ if env is None else env
    try:
        return Keypair.from_base58_string((env.get("PUMP_LIVE_KEY") or "").strip())
    except Exception:  # noqa: BLE001 - the message could carry the key
        raise ValueError("PUMP_LIVE_KEY is not a base58 64-byte secret key") from None


class LiveFollow:
    """The executor. The database, the orders and the positions are only touched on the feed's thread (on_trade,
    tick); building, simulating, sending and looking up run on worker threads, which share nothing with it but two
    read-mostly caches (token programs, recipient lists) filled under a lock, and hand their results back through
    tick(). `keypair` is required in live mode and ignored otherwise."""

    def __init__(self, c, cfg: LiveCfg, rpc: Rpc | None = None, workers=None, keypair=None) -> None:
        import threading
        from concurrent.futures import ThreadPoolExecutor
        if cfg.mode == "live" and keypair is None:
            raise ValueError("live copies need the wallet's keypair")
        self.c, self.cfg = c, cfg
        c.executescript(LIVE_SCHEMA)
        self.rpc = rpc or Rpc(cfg.rpc_url, cfg.send_urls)
        self.kp = keypair if cfg.mode == "live" else None
        self.me = str(self.kp.pubkey()) if self.kp is not None else None     # the address only: never the key itself
        self.fill_lock = threading.Lock()
        self.workers = workers or ThreadPoolExecutor(max_workers=4, thread_name_prefix="pumplive")
        self.jobs: list[tuple[dict[str, Any], Any]] = []
        self.targets: set[str] = set(cfg.wallets)
        self.copied = set(c.execute("SELECT wallet, mint FROM lorders WHERE side = 'buy' AND mode = ?", (cfg.mode,)))
        self.pos = {m: {"wallet": w, "tok": tok, "cost": cost, "opened": opened, "stuck": stuck} for m, w, tok, cost, opened, stuck
                    in c.execute("SELECT mint, wallet, tok, cost, opened, stuck FROM lpos")}
        cols = ("id", "wallet", "mint", "side", "venue", "trigger_slot", "seen", "done", "sig", "tries")
        self.open = {r[0]: dict(zip(cols, r)) for r in c.execute(       # sent before a restart: looked up again
            f"SELECT {', '.join(cols)} FROM lorders WHERE status = 'sent' AND mode = 'live'")}
        self.coins: dict[str, dict[str, Any]] = {}     # mint -> what its transactions need, from its newest trade
        self.token_programs: dict[str, str] = {}       # mint -> its token program, from its creation event
        self.lists: dict[bool, tuple[float, dict[str, list[str]]]] = {}
        self.balance: float | None = None
        self.at = {"targets": 0.0, "balance": 0.0, "hash": 0.0}

    # --- from the feed -------------------------------------------------------
    def on_create(self, mint: str, token_program: bytes) -> None:
        from .pumpfun import b58
        self.token_programs[mint] = b58(token_program)

    def on_trade(self, slot: int, mint: str, user: str, e: dict[str, Any]) -> None:
        """Every trade the collector sees: our own fills, the newest state of the coins we hold or may copy, and the
        chosen wallets' first buys and first sells."""
        if self.me is not None and user == self.me:
            self._own_fill(mint, e)
            return
        if not e.get("tok") or not (user in self.targets or mint in self.pos or self._busy(mint)):
            return
        self.coins[mint] = coin_of(mint, e)
        if user not in self.targets:
            return
        if e["buy"]:
            if (user, mint) not in self.copied and mint not in self.pos and not self._busy(mint):
                self.copied.add((user, mint))
                self._buy(user, mint, slot)
        elif self.pos.get(mint, {}).get("wallet") == user:
            self._sell(mint, slot, "its wallet sold")
        else:
            for o in self._orders():
                if o["mint"] == mint and o["side"] == "buy" and o["wallet"] == user:
                    o["sell_after"] = slot                 # it sold before our buy showed up: out the moment it does

    def _orders(self) -> list[dict[str, Any]]:
        return [*self.open.values(), *(o for o, _ in self.jobs if o["side"] in ("buy", "sell"))]

    def _busy(self, mint: str) -> bool:
        return any(o["mint"] == mint for o in self._orders())

    # --- orders ------------------------------------------------------------
    def _blocked(self) -> str | None:
        buying = sum(1 for o in self._orders() if o["side"] == "buy")
        if len(self.pos) + buying >= self.cfg.max_open:
            return f"{len(self.pos) + buying} copies open already"
        lost = -self._day_pnl()
        if lost >= self.cfg.day_loss_sol:
            return f"{lost:.3f} SOL lost today"
        if self.balance is None:
            return "balance not read yet"
        free = self.balance - buying * self.cfg.stake_sol     # the balance read may predate the buys still on their way
        if free < self.cfg.stake_sol + RESERVE_SOL:
            return f"balance {free:.3f} SOL"
        return None

    def _day_pnl(self) -> float:
        now = time.time()
        return self.c.execute("SELECT COALESCE(SUM(pnl), 0) FROM lorders WHERE side = 'sell' AND mode = 'live' AND done >= ?",
                              (now - now % 86_400,)).fetchone()[0]

    def _buy(self, wallet: str, mint: str, slot: int) -> None:
        coin = self.coins[mint]
        o = {"wallet": wallet, "mint": mint, "side": "buy", "venue": "pool" if coin["pool"] else "curve",
             "trigger_slot": slot, "seen": time.time(), "tries": 1}
        why = self._blocked() if self.cfg.mode == "live" else None
        if why:
            self._record(o, status="skipped", err=why)
            log.info("live: not copying %s's buy of %s: %s", wallet, mint, why)
            return
        lamports = int(self.cfg.stake_sol * 1e9)
        o["want"] = tokens_for(coin, lamports)
        min_tok = int(o["want"] * (1 - self.cfg.buy_slip))
        signer = self.me or self.cfg.sim_signer or wallet
        self._record(o, status="pending")
        self.jobs.append((o, self.workers.submit(
            self._send, o, signer, lambda tp: (*buy_ixs(coin, signer, lamports, min_tok, tp, self._lists(bool(coin["pool"]))), {}))))

    def _sell(self, mint: str, slot: int | None, why: str, tries: int = 1) -> None:
        p = self.pos[mint]
        if self._busy(mint) or (p.get("stuck") and tries == 1):
            return                                          # a sell is on its way, or three failed: left to the owner
        coin = None if tries > 1 else self.coins.get(mint)  # a retry, or a coin no trade has shown: read from the chain
        o = {"wallet": p["wallet"], "mint": mint, "side": "sell", "venue": ("pool" if coin["pool"] else "curve") if coin else None,
             "trigger_slot": slot, "seen": time.time(), "tries": tries}
        self._record(o, status="pending")
        log.info("live: selling %s (%s, try %d)", mint, why, tries)
        self.jobs.append((o, self.workers.submit(self._send, o, self.me, lambda tp: self._sell_ixs(coin, mint, p["tok"], tries, tp))))

    def _sell_ixs(self, coin: dict[str, Any] | None, mint: str, tok: int, tries: int, tp: str) -> tuple[list, int, dict[str, Any]]:
        """On a worker thread: the sell, priced on the newest state known."""
        coin = coin or fresh_coin(self.rpc, mint, tp)
        want = sol_for(coin, tok)
        min_sol = 0 if tries >= SELL_TRIES else int(want * (1 - self.cfg.sell_slip))
        ixs, cu = sell_ixs(coin, self.me, tok, min_sol, tp, self._lists(bool(coin["pool"])), close=tries < SELL_TRIES)
        return ixs, cu, {"want": want, "venue": "pool" if coin["pool"] else "curve"}

    def _send(self, o: dict[str, Any], signer: str, build) -> dict[str, Any]:
        """On a worker thread: build, then simulate (dry) or sign and send (live)."""
        ixs, cu, extra = build(self._token_program(o["mint"]))
        tip = random.choice(TIP_ACCOUNTS)
        if self.cfg.mode == "dry":
            ready = time.time()
            res = self.rpc.simulate(compose(signer, ixs, cu_limit=cu, tip_to=tip, tip_lamports=TIP_LAMPORTS))
            got, ok = own_trade(res.get("logs") or [], signer), res.get("err") is None
            return {**extra, "status": "sim_ok" if ok else "sim_err", "err": None if ok else sim_error(res), "ready": ready,
                    "units": res.get("unitsConsumed"), "slot": res.get("slot"),
                    "sol": (got["sol"] + got["fee"]) / 1e9 if got else None, "tok": got.get("tok")}
        tx = compose(signer, ixs, self.rpc.blockhash(), self.kp, cu_limit=cu, tip_to=tip, tip_lamports=TIP_LAMPORTS)
        ready = time.time()
        return {**extra, "status": "sent", "sig": self.rpc.send(tx), "ready": ready}

    def _token_program(self, mint: str) -> str:
        tp = self.token_programs.get(mint)
        if tp is None:                                      # a coin born before this start: its mint's owner, asked once
            with self.fill_lock:
                tp = self.token_programs.get(mint)
                if tp is None:
                    acct = self.rpc.account(mint)
                    if acct is None:
                        raise ValueError(f"no mint account {mint}")
                    tp = self.token_programs[mint] = acct[0]
        return tp

    def _lists(self, amm: bool) -> dict[str, list[str]]:
        """The fee and buyback recipient lists of pump.fun's global account, or PumpSwap's global config, read once an
        hour: under a lock, so a burst of copies asks the RPC once."""
        with self.fill_lock:
            at, lists = self.lists.get(amm, (0.0, None))
            if lists is None or time.time() - at > LISTS_TTL_S:
                acct = self.rpc.account(AMM_GLOBAL_CONFIG if amm else PUMP_GLOBAL)
                if acct is None:
                    raise ValueError("no global account")
                lists = parse_global(acct[1], AMM_LISTS if amm else PUMP_LISTS)
                self.lists[amm] = (time.time(), lists)
            return lists

    def _record(self, o: dict[str, Any], **kw: Any) -> None:
        o.update(kw)
        if "id" not in o:
            o["id"] = self.c.execute("""INSERT INTO lorders(mode, wallet, mint, side, venue, trigger_slot, seen, status, err, want, tries)
                                        VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (self.cfg.mode, o["wallet"], o["mint"], o["side"], o["venue"],
                                        o["trigger_slot"], o["seen"], o["status"], o.get("err"), o.get("want"), o["tries"])).lastrowid
        else:
            self.c.execute("""UPDATE lorders SET venue=?, status=?, err=?, sig=?, sol=?, tok=?, want=?, units=?, slot=?, ready=?,
                              done=?, pnl=? WHERE id=?""", (o["venue"], o["status"], o.get("err"), o.get("sig"), o.get("sol"),
                              o.get("tok"), o.get("want"), o.get("units"), o.get("slot"), o.get("ready"), o.get("done"), o.get("pnl"), o["id"]))

    # --- results -----------------------------------------------------------
    def tick(self) -> None:
        """Every second, on the feed's thread: finished jobs, late transactions, the balance, copies held too long."""
        for job in [j for j in self.jobs if j[1].done()]:
            self.jobs.remove(job)
            o, fut = job
            try:
                res = fut.result()
            except Exception as e:  # noqa: BLE001 - an RPC or build failure is this order's result, not the feed's
                res = {"status": "sim_err" if self.cfg.mode == "dry" else "failed", "err": f"{type(e).__name__}: {e}"[:300]}
            self._settled(o, res)
        now = time.time()
        if not self.cfg.wallets and now - self.at["targets"] >= 60:
            self.targets = {w for (w,) in self.c.execute("SELECT wallet FROM follow WHERE golden_ever = 1")}
            self.at["targets"] = now
        if self.cfg.mode != "live":
            return
        if now - self.at["hash"] >= 10:                     # a fresh blockhash kept ready: a send never waits for one
            self.at["hash"] = now
            self.workers.submit(self.rpc.blockhash, 5.0)
        if now - self.at["balance"] >= 30:
            self.at["balance"] = now
            self.jobs.append(({"side": "balance", "mint": ""}, self.workers.submit(lambda: {"balance": self.rpc.balance(self.me) / 1e9})))
        for o in list(self.open.values()):
            if now >= o.get("next_look", (o["done"] or 0) + FILL_WAIT_S) and not o.get("looking"):
                o["looking"] = True
                self.jobs.append(({"side": "lookup", "mint": o["mint"], "id": o["id"]}, self.workers.submit(self._lookup, dict(o))))
        for m, p in list(self.pos.items()):
            if now - p["opened"] >= MAX_HOLD_S:
                self._sell(m, None, "held too long")

    def _settled(self, o: dict[str, Any], res: dict[str, Any]) -> None:
        if o["side"] == "balance":
            self.balance = res.get("balance", self.balance)
            return
        if o["side"] == "lookup":                             # a sent transaction looked up on the chain
            real = self.open.get(o["id"])
            if real is None:
                return                                        # the feed showed it meanwhile
            real["looking"], real["next_look"] = False, time.time() + LOOK_EVERY_S
            if res.get("status") == "filled":
                self._fill(real, res)
            elif res.get("status") in ("failed", "expired"):
                self._failed(real, res.get("err") or res["status"])
            return
        o.update(res, done=time.time())
        if o["status"] == "sent":
            self.open[o["id"]] = o
            self._record(o)
            log.info("live: %s %s sent: %s", o["side"], o["mint"], o["sig"])
            if o.get("fill"):                                 # it landed before its send came back
                self._fill(o, o.pop("fill"))
        elif o["status"] == "failed":
            self._failed(o, o.get("err") or "not sent")
        else:
            self._record(o)                                   # dry: what the simulation said

    def _failed(self, o: dict[str, Any], err: str) -> None:
        self.open.pop(o["id"], None)
        self._record(o, status="failed", err=str(err)[:300], done=time.time())
        log.warning("live: %s of %s failed: %s", o["side"], o["mint"], err)
        if o["side"] != "sell" or o["mint"] not in self.pos:
            return
        if o["tries"] < SELL_TRIES:
            self._sell(o["mint"], None, "retry", o["tries"] + 1)
        else:
            self.pos[o["mint"]]["stuck"] = 1
            self.c.execute("UPDATE lpos SET stuck = 1 WHERE mint = ?", (o["mint"],))
            log.error("live: could not sell %s in %d tries: it is left for its owner to sell", o["mint"], SELL_TRIES)

    def _own_fill(self, mint: str, e: dict[str, Any]) -> None:
        side, fill = ("buy" if e["buy"] else "sell"), {"sol": e["sol"], "tok": e["tok"], "fee": e["fee"]}
        o = next((o for o in self.open.values() if o["mint"] == mint and o["side"] == side), None)
        if o is not None:
            self._fill(o, fill)
            return
        o = next((o for o, _ in self.jobs if o.get("mint") == mint and o.get("side") == side), None)
        if o is not None:
            o["fill"] = fill                                  # landed before its send came back: settled together
            return
        log.warning("live: our wallet traded %s outside the copies (%s)", mint, side)

    def _fill(self, o: dict[str, Any], fill: dict[str, Any]) -> None:
        from .pumpfun import TX_COST_SOL
        self.open.pop(o["id"], None)
        now = time.time()
        if o["side"] == "buy":
            cost = (fill["sol"] + fill["fee"]) / 1e9 + TX_COST_SOL
            self.pos[o["mint"]] = {"wallet": o["wallet"], "tok": fill["tok"], "cost": cost, "opened": int(now), "stuck": 0}
            self.c.execute("INSERT OR REPLACE INTO lpos(mint, wallet, tok, cost, opened) VALUES (?,?,?,?,?)",
                           (o["mint"], o["wallet"], fill["tok"], cost, int(now)))
            self._record(o, status="filled", sol=cost, tok=fill["tok"], done=now)
            log.info("live: bought %s for %.4f SOL", o["mint"], cost)
            if o.get("sell_after") is not None:
                self._sell(o["mint"], o["sell_after"], "its wallet sold first")
        else:
            p = self.pos.pop(o["mint"], None) or {"cost": 0.0}
            got = (fill["sol"] - fill["fee"]) / 1e9 - TX_COST_SOL
            self.c.execute("DELETE FROM lpos WHERE mint = ?", (o["mint"],))
            self._record(o, status="filled", sol=got, tok=fill["tok"], pnl=got - p["cost"], done=now)
            log.info("live: sold %s for %.4f SOL: %+.4f SOL", o["mint"], got, got - p["cost"])

    def _lookup(self, o: dict[str, Any]) -> dict[str, Any]:
        """On a worker thread: the fate of a sent transaction the feed has not shown. Only the chain's answer settles
        it: a lookup that fails (rate limit, network) asks again later, or a buy that landed would be forgotten."""
        try:
            tx = self.rpc.call("getTransaction", [o["sig"], {"encoding": "json", "maxSupportedTransactionVersion": 1,
                                                             "commitment": "confirmed"}])
        except Exception as e:  # noqa: BLE001
            log.info("live: looking up %s failed (%s): again in %d s", o["sig"], str(e)[:120], LOOK_EVERY_S)
            return {}
        if tx is None:
            return {"status": "expired"} if time.time() - (o["done"] or 0) >= EXPIRE_S else {}
        if tx["meta"].get("err") is not None:
            return {"status": "failed", "err": sim_error(tx["meta"])}
        got = own_trade(tx["meta"].get("logMessages") or [], self.me)
        return {"status": "filled", **got} if got else {"status": "failed", "err": "landed without a trade"}


def live_lines(db_path, min_per_wallet: int = 5) -> list[str]:
    """For the log: the dry run (would the copies have gone through, and what would they have bought next to the paper
    copy of the same buy and to the price the wallet's buy left), and the live copies' results."""
    import collections
    from .pumpfun import _pct, connect
    c = connect(db_path, readonly=True)
    try:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name = 'lorders'").fetchone():
            return []
        dry = c.execute("""SELECT d.wallet, d.status = 'sim_ok', d.err, d.tok, d.want, d.ready - d.seen, d.slot - d.trigger_slot, p.tok, d.seen
                           FROM lorders d LEFT JOIN pfills p ON p.wallet = d.wallet AND p.mint = d.mint AND p.side = 'buy'
                           WHERE d.mode = 'dry' AND d.side = 'buy' AND d.status != 'pending'""").fetchall()
        live = c.execute("""SELECT side, status, COUNT(*), COALESCE(SUM(pnl), 0) FROM lorders WHERE mode = 'live'
                            GROUP BY side, status""").fetchall()
        held = c.execute("SELECT COUNT(*), COALESCE(SUM(cost), 0), COALESCE(SUM(stuck), 0) FROM lpos").fetchone()
    finally:
        c.close()
    q = lambda xs, p: xs[min(len(xs) - 1, int(p * len(xs)))] if xs else None       # noqa: E731

    def line(name: str, rs: list) -> str:
        ok = [r for r in rs if r[1]]
        errs = collections.Counter(r[2] for r in rs if not r[1])
        paper = sorted(r[3] / r[7] - 1 for r in ok if r[3] and r[7])
        left = sorted(r[3] / r[4] - 1 for r in ok if r[3] and r[4])
        ready = sorted(r[5] * 1000 for r in rs if r[5] is not None)
        slots = sorted(r[6] for r in rs if r[6] is not None)
        return (f"{name}: {len(rs)} buys, {len(ok)} would have gone through ({len(ok) / len(rs):.0%})"
                + (f", failed {', '.join(f'{k} x{v}' for k, v in errs.most_common(3))}" if errs else "")
                + f" | tokens vs the paper copy p10/p50/p90 {'/'.join(_pct(q(paper, p)) for p in (.1, .5, .9))} (n{len(paper)}),"
                f" vs the price its buy left p50 {_pct(q(left, .5))} | ready p50 {q(ready, .5) or 0:.0f} ms after its buy reached us,"
                f" simulated p50 {q(slots, .5)} slots after it")

    out = []
    if dry:
        since = time.strftime("%m-%d %H:%M", time.gmtime(min(r[8] for r in dry)))
        out.append(line(f"dry run (live copies simulated, nothing sent) since {since} UTC", dry))
        by = collections.defaultdict(list)
        for r in dry:
            by[r[0]].append(r)
        out += [line(f"dry run {w}", rs) for w, rs in sorted(by.items()) if len(rs) >= min_per_wallet]
    if live:
        n = {(side, status): (k, pnl) for side, status, k, pnl in live}
        count = lambda side, st: n.get((side, st), (0, 0))[0]                       # noqa: E731
        out.append(f"live: buys {count('buy', 'filled')} filled, {count('buy', 'failed')} failed, {count('buy', 'skipped')} skipped"
                   f" | sells {count('sell', 'filled')} filled, {count('sell', 'failed')} failed | realized "
                   f"{n.get(('sell', 'filled'), (0, 0))[1]:+.4f} SOL | {held[0]} open ({held[1]:.3f} SOL in)"
                   + (f", {held[2]} STUCK: sell by hand" if held[2] else ""))
    return out
