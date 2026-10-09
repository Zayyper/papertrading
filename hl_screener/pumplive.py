"""Live copies on pump.fun and PumpSwap: the paper follower's rule, with real transactions.

A chosen wallet's first buy of a coin is copied with `stake_sol`, and the copy is sold when that wallet first sells:
PaperFollow's rule, so the two compare copy for copy. PUMP_LIVE picks the mode:

    dry   (the default) each copy's buy is built and simulated by the RPC the moment it would be sent, on the chain's
          newest state: no key, nothing sent. It says whether the transaction would have gone through, what it would
          have bought next to the paper copy, and how fast the tool had it ready.
    live  signed with PUMP_LIVE_KEY and sent through PUMP_LIVE_SEND (Helius Sender by default). Only
          PUMP_LIVE_WALLETS are copied, at most PUMP_LIVE_MAX_OPEN at a time, and new copies stop for the UTC day once
          PUMP_LIVE_DAY_LOSS_SOL is lost. Sells always go out: up to three tries, the last one at any price.
    exit  live, but no new copies: the open ones are sold as their wallets sell (or after MAX_HOLD_S). The way out of
          live mode: dry never sells, off forgets the copies, and both leave them open.
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

from .pumptx import (AMM_GLOBAL_CONFIG, AMM_LISTS, PUBLIC_RPC, PUMP_GLOBAL, PUMP_LISTS, SLOT_S, TIP_ACCOUNTS, TIP_LAMPORTS, Refused, Rpc,
                     buy_ixs, coin_of, compose, fresh_coin, own_trade, parse_global, sell_ixs, sim_error, sol_for, tokens_for)

log = logging.getLogger(__name__)

# keyless, free, needs the 0.001 SOL tip, 1 a second per IP. Jito's own endpoint left out (2026-10-07): it wants a tip to
# one of its accounts, Helius forwards to Jito already, and its 200 could hide a Helius refusal as 'sent' for 90 s.
SENDERS = ("https://sender.helius-rpc.com/fast",)
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
RETRY_WAIT_S = 2.0       # between a failed sell and its next try: three tries within a second would meet the same rate limit
UNSENT_MAX = 8           # a sell that could not go out (an RPC read or every send refused) tries again this often, waiting
UNSENT_WAIT_S = 30.0     # RETRY_WAIT_S doubling up to this, about 2.5 min in all, before the attempt counts as one of its tries
MAX_HOLD_S = 12 * 3600   # a copy whose wallet never sells (it moved its tokens?) is sold after this long
RESERVE_SOL = 0.02       # left in the wallet for fees and new token accounts' rent
LISTS_TTL_S = 3600       # fee and buyback recipient lists, re-read this often, in the background
MAX_AGE_S = 2.0          # a buy not ready to go out this long after its wallet's is not sent (PUMP_LIVE_MAX_AGE_S): the live copies
                         # landed 4 slots (~0.9 s) behind their wallet at p50, but a quarter 13+ (~2.9 s) and one 804 (~3.5 min),
                         # held up by RPC reads (2026-10-07..09). 2 s, ~9 slots before the send, keeps the usual ones with room
                         # for a slow read and drops the slow quarter, bought after the price had moved, maybe after the wallet's sell


@dataclass(frozen=True)
class LiveCfg:
    mode: str = "dry"                          # off | dry | live | exit
    wallets: frozenset[str] = frozenset()      # whom to copy; dry with none set: every golden wallet
    stake_sol: float = 0.25
    max_open: int = 3                          # copies open or on their way at once
    day_loss_sol: float = 0.5                  # realized loss in a UTC day that stops new copies until the next
    buy_slip: float = 0.25                     # a buy fails rather than get this much fewer tokens than priced
    sell_slip: float = 0.5                     # a sell takes up to this much less SOL than priced: getting out comes first
    rpc_url: str = PUBLIC_RPC                  # reads, simulations, blockhashes, lookups
    send_urls: tuple[str, ...] = SENDERS
    sim_signer: str | None = None              # dry: simulate as this address (a funded wallet), else as the copied wallet
    max_age_s: float = MAX_AGE_S               # buys older than this when ready are not sent, nor simulated; sells always go out

    @property
    def sends(self) -> bool:
        """Signs and sends: live, and exit, which only sells what is open."""
        return self.mode in ("live", "exit")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "LiveCfg":
        env = os.environ if env is None else env
        mode = (env.get("PUMP_LIVE") or "dry").strip().lower()
        if mode not in ("off", "dry", "live", "exit"):
            raise ValueError(f"PUMP_LIVE must be off, dry, live or exit, not {mode!r}")
        listed = lambda k: tuple(x.strip() for x in (env.get(k) or "").split(",") if x.strip())    # noqa: E731
        num = lambda k, d: float(env.get(k) or d)                                                  # noqa: E731
        cfg = cls(mode=mode, wallets=frozenset(listed("PUMP_LIVE_WALLETS")), stake_sol=num("PUMP_LIVE_STAKE_SOL", 0.25),
                  max_open=int(num("PUMP_LIVE_MAX_OPEN", 3)), day_loss_sol=num("PUMP_LIVE_DAY_LOSS_SOL", 0.5),
                  buy_slip=num("PUMP_LIVE_BUY_SLIP", 0.25), sell_slip=num("PUMP_LIVE_SELL_SLIP", 0.5),
                  rpc_url=env.get("PUMP_LIVE_RPC") or PUBLIC_RPC, send_urls=listed("PUMP_LIVE_SEND") or SENDERS,
                  sim_signer=env.get("PUMP_LIVE_SIM_SIGNER") or None, max_age_s=num("PUMP_LIVE_MAX_AGE_S", MAX_AGE_S))
        buys = mode in ("dry", "live")                  # exit only sells: a bad buy setting must not keep its copies from getting out
        if not 0 <= cfg.sell_slip < 1 or buys and (not 0 < cfg.stake_sol <= 5 or not 0 <= cfg.buy_slip < 1 or cfg.max_open < 1
                                                   or cfg.day_loss_sol <= 0 or not 0 < cfg.max_age_s <= 60):
            raise ValueError("PUMP_LIVE_STAKE_SOL must be in (0, 5], the slippages in [0, 1), PUMP_LIVE_MAX_OPEN at least 1, "
                             "PUMP_LIVE_DAY_LOSS_SOL above 0 and PUMP_LIVE_MAX_AGE_S in (0, 60]")
        if cfg.sends and not ((env.get("PUMP_LIVE_KEY") or "").strip() and cfg.wallets):
            raise ValueError(f"PUMP_LIVE={mode} needs PUMP_LIVE_KEY and PUMP_LIVE_WALLETS")
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
    read-mostly caches (token programs, recipient lists) filled under a lock and the newest slot seen, and hand their
    results back through tick(). `keypair` is required in live mode and ignored otherwise."""

    def __init__(self, c, cfg: LiveCfg, rpc: Rpc | None = None, workers=None, keypair=None) -> None:
        import threading
        from concurrent.futures import ThreadPoolExecutor
        if cfg.sends and keypair is None:
            raise ValueError("live copies need the wallet's keypair")
        self.c, self.cfg = c, cfg
        c.executescript(LIVE_SCHEMA)
        self.rpc = rpc or Rpc(cfg.rpc_url, cfg.send_urls)
        self.kp = keypair if cfg.sends else None
        self.me = str(self.kp.pubkey()) if self.kp is not None else None     # the address only: never the key itself
        self.fill_lock = threading.Lock()
        self.workers = workers or ThreadPoolExecutor(max_workers=4, thread_name_prefix="pumplive")
        self.jobs: list[tuple[dict[str, Any], Any]] = []
        self.targets: set[str] = set(cfg.wallets)
        self.copied = set(c.execute("SELECT wallet, mint FROM lorders WHERE side = 'buy' AND mode = ?", (cfg.mode,)))
        if cfg.sends:            # a coin the dry run or the paper already copied of that wallet: its next buy is an add, no first buy
            self.copied |= set(c.execute("SELECT wallet, mint FROM lorders WHERE side = 'buy' UNION "
                                         "SELECT wallet, mint FROM pfills WHERE side = 'buy'"))
        self.pos = {m: {"wallet": w, "tok": tok, "cost": cost, "opened": opened, "stuck": stuck} for m, w, tok, cost, opened, stuck
                    in c.execute("SELECT mint, wallet, tok, cost, opened, stuck FROM lpos")}
        cols = ("id", "wallet", "mint", "side", "venue", "trigger_slot", "seen", "done", "sig", "tries")
        self.open = {r[0]: dict(zip(cols, r)) for r in c.execute(       # sent before a restart: looked up again
            f"SELECT {', '.join(cols)} FROM lorders WHERE status = 'sent' AND mode = 'live'")}
        self.coins: dict[str, dict[str, Any]] = {}     # mint -> what its transactions need, from its newest trade
        self.token_programs: dict[str, str] = {}       # mint -> its token program, from its creation event
        self.lists: dict[bool, tuple[float, dict[str, list[str]]]] = {}
        self.balance: float | None = None
        self.retries: list[tuple[float, str, int]] = []   # (not before, mint, try) of the sells to send again
        self.at = {"targets": 0.0, "balance": 0.0, "hash": 0.0, "lists": 0.0}
        self.tip = 0                                   # the newest slot a trade came from: how far the chain went since a buy
        self.lag = [float("inf"), float("inf"), 0.0]   # least (now - a trade's block time) this minute and the last, since:
                                                       # the usual lag, the yardstick for a trade the feed's thread got to late

    # --- from the feed -------------------------------------------------------
    def on_create(self, mint: str, token_program: bytes) -> None:
        from .pumpfun import b58
        self.token_programs[mint] = b58(token_program)

    def on_trade(self, slot: int, mint: str, user: str, e: dict[str, Any]) -> None:
        """Every trade the collector sees: our own fills, the newest state of the coins we hold or may copy, and the
        chosen wallets' first buys and first sells."""
        self.tip = max(self.tip, slot)
        if e.get("ts"):
            now = time.time()
            if now - self.lag[2] > 60:
                self.lag = [now - e["ts"], self.lag[0], now]
            else:
                self.lag[0] = min(self.lag[0], now - e["ts"])
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
                self._buy(user, mint, slot, e.get("ts"))
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

    def _buy(self, wallet: str, mint: str, slot: int, ts: int | None = None) -> None:
        coin = self.coins[mint]
        o = {"wallet": wallet, "mint": mint, "side": "buy", "venue": "pool" if coin["pool"] else "curve",
             "trigger_slot": slot, "seen": time.time(), "tries": 1, "chain_ts": ts}
        why = ("winding down" if self.cfg.mode == "exit" else self._blocked()) if self.cfg.sends else None
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
        if self._busy(mint) or any(r[1] == mint for r in self.retries) or (p.get("stuck") and tries == 1):
            return                                          # a sell is on its way or due again, or three failed: left to the owner
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
        """On a worker thread: build, then simulate (dry) or sign and send (live), unless it is a buy ready too late."""
        if self.cfg.sends:
            return self._send_live(o, signer, build)
        ixs, cu, extra = build(self._token_program(o["mint"]))
        tip = random.choice(TIP_ACCOUNTS)
        ready = time.time()
        late = self._too_late(o)
        if late:
            return {"status": "skipped", "err": late, "ready": ready}
        res = self.rpc.simulate(compose(signer, ixs, cu_limit=cu, tip_to=tip, tip_lamports=TIP_LAMPORTS))
        got, ok = own_trade(res.get("logs") or [], signer), res.get("err") is None
        return {**extra, "status": "sim_ok" if ok else "sim_err", "err": None if ok else sim_error(res), "ready": ready,
                "units": res.get("unitsConsumed"), "slot": res.get("slot"),
                "sol": (got["sol"] + got["fee"]) / 1e9 if got else None, "tok": got.get("tok")}

    def _send_live(self, o: dict[str, Any], signer: str, build) -> dict[str, Any]:
        """Built, signed, sent. 'unsent' when nothing went out: an RPC read failed first (the coin, the lists, the token
        program, the blockhash), or every endpoint refused it. A send that failed otherwise (a timeout) may have gone out
        all the same: 'sent', with its signature, known before sending, so the lookup decides, and a buy that landed
        is not forgotten with its tokens."""
        from .pumpfun import _no_key
        why = lambda e: _no_key(f"{type(e).__name__}: {e}")[:300]                       # noqa: E731
        try:
            ixs, cu, extra = build(self._token_program(o["mint"]))
            tx = compose(signer, ixs, self.rpc.blockhash(), self.kp, cu_limit=cu, tip_to=random.choice(TIP_ACCOUNTS),
                         tip_lamports=TIP_LAMPORTS)
        except Exception as e:  # noqa: BLE001 - this order's result: nothing went out
            return {"status": "unsent", "err": why(e)}
        ready = time.time()
        late = self._too_late(o)
        if late:
            return {"status": "skipped", "err": late, "ready": ready}
        try:
            return {**extra, "status": "sent", "sig": self.rpc.send(tx), "ready": ready}
        except Refused as e:
            return {**extra, "status": "unsent", "err": why(e), "ready": ready}
        except Exception as e:  # noqa: BLE001 - it may have gone out
            return {**extra, "status": "sent", "sig": str(tx.signatures[0]), "err": why(e), "ready": ready}

    def _too_late(self, o: dict[str, Any]) -> str | None:
        """On a worker thread, as a buy is about to go out: its age, by our clock since its wallet's buy reached us, or by
        the slots trades came from since (a buy that reached us late), whichever says older: the clock falls a little
        short of the real age, the slots come close to it. Past cfg.max_age_s it is another trade: the price has moved and the wallet may be selling already.
        Sells always go out: getting out comes first."""
        if o["side"] != "buy":
            return None
        now = time.time()
        age = max(now - o["seen"], (self.tip - o["trigger_slot"]) * SLOT_S)
        if o.get("chain_ts"):                          # its block's time, less the usual lag and a second (whole seconds): a
            age = max(age, now - o["chain_ts"] - min(self.lag[:2]) - 1)   # buy the feed's thread got to late, behind a stall
        if age <= self.cfg.max_age_s:
            return None
        log.info("live: not copying %s's buy of %s: ready %.1f s after it, over %g s", o["wallet"], o["mint"], age, self.cfg.max_age_s)
        return f"too late: {age:.1f} s after its wallet's buy"

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

    def _lists(self, amm: bool, max_age_s: float = float("inf")) -> dict[str, list[str]]:
        """The fee and buyback recipient lists of pump.fun's global account, or PumpSwap's global config. tick() reads
        them in the background at the start and once an hour after (`max_age_s`): a copy takes the last ones read, and
        reads them itself only when no read has worked yet, under a lock, so a burst of copies asks the RPC once."""
        at, lists = self.lists.get(amm, (0.0, None))
        if lists is not None and time.time() - at <= max_age_s:
            return lists                                    # a copy's way: no lock, no read
        with self.fill_lock:
            at, lists = self.lists.get(amm, (0.0, None))
            if lists is None or time.time() - at > max_age_s:
                try:
                    acct = self.rpc.account(AMM_GLOBAL_CONFIG if amm else PUMP_GLOBAL)
                    if acct is None:
                        raise ValueError("no global account")
                    lists = parse_global(acct[1], AMM_LISTS if amm else PUMP_LISTS)
                    self.lists[amm] = (time.time(), lists)
                except Exception as e:  # noqa: BLE001 - they change once in months: the last ones beat a sell not sent
                    if lists is None:
                        raise
                    log.info("live: the recipient lists could not be read again (%s): the last ones are used", type(e).__name__)
            return lists

    def _record(self, o: dict[str, Any], **kw: Any) -> None:
        o.update(kw)
        if "id" not in o:
            mode = "live" if self.cfg.sends else self.cfg.mode     # exit's orders are live ones: counted, reloaded, shown
            o["id"] = self.c.execute("""INSERT INTO lorders(mode, wallet, mint, side, venue, trigger_slot, seen, status, err, want, tries)
                                        VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (mode, o["wallet"], o["mint"], o["side"], o["venue"],
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
                from .pumpfun import _no_key                  # a requests error names the URL, and PUMP_LIVE_RPC may carry a key
                res = {"status": "sim_err" if self.cfg.mode == "dry" else "failed", "err": _no_key(f"{type(e).__name__}: {e}")[:300]}
            self._settled(o, res)
        now = time.time()
        due, self.retries = [r for r in self.retries if r[0] <= now], [r for r in self.retries if r[0] > now]
        for _, mint, tries in due:
            if mint in self.pos:
                self._sell(mint, None, "retry", tries)
        if not self.cfg.wallets and now - self.at["targets"] >= 60:
            self.targets = {w for (w,) in self.c.execute("SELECT wallet FROM follow WHERE golden_ever = 1")}
            self.at["targets"] = now
        if now - self.at["lists"] >= 60:                    # the recipient lists read here, off the send path: at the start, then
            self.at["lists"] = now                          # once they are LISTS_TTL_S old (a minute after a read that failed)
            self.workers.submit(lambda: [self._lists(amm, LISTS_TTL_S) for amm in (False, True)])
        if not self.cfg.sends:
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
                self.coins.pop(m, None)                     # priced on the chain: its last trade seen may be hours old, or the curve's
                self._sell(m, None, "held too long")

    def _settled(self, o: dict[str, Any], res: dict[str, Any]) -> None:
        if o["side"] == "balance":
            if "balance" in res:
                from .pumpfun import set_meta
                self.balance = res["balance"]
                set_meta(self.c, "live_wallet", {"balance": self.balance, "at": int(time.time())})   # for the 30-min line
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
        fill = o.pop("fill", None)
        if o["status"] == "sent":
            self.open[o["id"]] = o
            self._record(o)
            self.pos.get(o["mint"], {}).pop("unsent", None)
            if o.get("err"):
                log.warning("live: %s %s may have gone out (%s): looked up as %s", o["side"], o["mint"], o["err"], o["sig"])
            else:
                log.info("live: %s %s sent: %s", o["side"], o["mint"], o["sig"])
        if fill is not None:                                  # it landed before its job came back, whatever the job said
            self._fill(o, fill)
        elif o["status"] == "unsent" and o["side"] == "sell" and o["mint"] in self.pos:
            self._unsent(o)
        elif o["status"] in ("failed", "unsent"):
            self._failed(o, o.get("err") or "not sent")
        elif o["status"] != "sent":
            self._record(o)                                   # dry: what the simulation said

    def _unsent(self, o: dict[str, Any]) -> None:
        """A sell that never went out tries again, later each time, without spending one of its SELL_TRIES: a burst of
        rate limits (or the public RPC's refusals after a restart) must not leave a copy STUCK with nothing sent."""
        p = self.pos[o["mint"]]
        n = p["unsent"] = p.get("unsent", 0) + 1
        if n > UNSENT_MAX:
            p.pop("unsent")
            self._failed(o, f"not sent in {n} attempts: {o.get('err')}")
            return
        wait = min(RETRY_WAIT_S * 2 ** (n - 1), UNSENT_WAIT_S)
        self._record(o)
        self.retries.append((time.time() + wait, o["mint"], o["tries"]))
        log.warning("live: sell of %s not sent (%s): try %d again in %.0f s", o["mint"], o.get("err"), o["tries"], wait)

    def _failed(self, o: dict[str, Any], err: str) -> None:
        self.open.pop(o["id"], None)
        self._record(o, status="failed", err=str(err)[:300], done=time.time())
        log.warning("live: %s of %s failed: %s", o["side"], o["mint"], err)
        if o["side"] != "sell" or o["mint"] not in self.pos:
            return
        if o["tries"] < SELL_TRIES:
            self.retries.append((time.time() + RETRY_WAIT_S, o["mint"], o["tries"] + 1))
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
        if side == "sell" and mint in self.pos:
            self._sold_by_hand(mint, fill)
            return
        log.warning("live: our wallet traded %s outside the copies (%s)", mint, side)

    def _sold_by_hand(self, mint: str, fill: dict[str, Any]) -> None:
        """Our wallet sold a coin a copy holds, with no sell of ours on its way: its owner sold it by hand (a STUCK copy,
        or one let go of), or a sell given up on landed after all. The copy is closed, its retries dropped and its slot
        freed: it would otherwise hold a slot for good, and be sold again after MAX_HOLD_S, paying fees for tokens gone.
        A part sold leaves the copy open with the rest: the event says how many tokens went."""
        from .pumpfun import TX_COST_SOL
        p = self.pos[mint]
        part = min(1.0, fill["tok"] / p["tok"]) if p["tok"] else 1.0
        got = (fill["sol"] - fill["fee"]) / 1e9 - TX_COST_SOL
        o = {"wallet": p["wallet"], "mint": mint, "side": "sell", "venue": None, "trigger_slot": None, "seen": time.time(), "tries": 0}
        self._record(o, status="filled", err="sold outside the copies")
        self._record(o, sol=got, tok=fill["tok"], pnl=got - p["cost"] * part, done=time.time())
        if part < 1:
            p.update(tok=p["tok"] - fill["tok"], cost=p["cost"] * (1 - part))
            self.c.execute("UPDATE lpos SET tok = ?, cost = ? WHERE mint = ?", (p["tok"], p["cost"], mint))
            log.warning("live: our wallet sold %d of the %d tokens of %s outside the copies: the copy goes on with the rest",
                        fill["tok"], fill["tok"] + p["tok"], mint)
            return
        del self.pos[mint]
        self.retries = [r for r in self.retries if r[1] != mint]
        self.c.execute("DELETE FROM lpos WHERE mint = ?", (mint,))
        log.warning("live: our wallet sold %s outside the copies for %.4f SOL (%+.4f SOL): its copy is closed", mint, got, got - p["cost"])

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
            from .pumpfun import _no_key
            log.info("live: looking up %s failed (%s): again in %d s", o["sig"], _no_key(str(e))[:120], LOOK_EVERY_S)
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
    import statistics
    from .pumpfun import TX_COST_SOL, _pct, connect, get_meta
    c = connect(db_path, readonly=True)
    try:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name = 'lorders'").fetchone():
            return []
        dry = c.execute("""SELECT d.wallet, d.status = 'sim_ok', CASE d.status WHEN 'skipped' THEN 'too late' ELSE d.err END,
                                  d.tok, d.want, d.ready - d.seen, d.slot - d.trigger_slot, p.tok, d.seen
                           FROM lorders d LEFT JOIN pfills p ON p.wallet = d.wallet AND p.mint = d.mint AND p.side = 'buy'
                           WHERE d.mode = 'dry' AND d.side = 'buy' AND d.status != 'pending'""").fetchall()
        live = c.execute("""SELECT side, status, COUNT(*), COALESCE(SUM(pnl), 0) FROM lorders WHERE mode = 'live'
                            GROUP BY side, status""").fetchall()
        held = c.execute("SELECT COUNT(*), COALESCE(SUM(cost), 0), COALESCE(SUM(stuck), 0) FROM lpos").fetchone()
        wallet = get_meta(c, "live_wallet")
        # each closed live copy next to the paper copy of the same buy: their profit over what each put in
        pairs = c.execute("""SELECT s.pnl, s.pnl / b.sol, p.pnl, p.pnl / pb.sol, b.sol, pb.sol
                             FROM (SELECT wallet, mint, SUM(pnl) AS pnl FROM lorders WHERE mode = 'live' AND side = 'sell'
                                   AND status = 'filled' GROUP BY wallet, mint) s
                             JOIN lorders b ON b.mode = 'live' AND b.side = 'buy' AND b.status = 'filled' AND b.wallet = s.wallet AND b.mint = s.mint
                             JOIN pfills pb ON pb.side = 'buy' AND pb.wallet = s.wallet AND pb.mint = s.mint
                             JOIN pfills p ON p.side = 'sell' AND p.wallet = s.wallet AND p.mint = s.mint
                             WHERE b.sol > 0 AND pb.sol > 0 AND s.mint NOT IN (SELECT mint FROM lpos)""").fetchall()
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
    if live or wallet:
        n = {(side, status): (k, pnl) for side, status, k, pnl in live}
        count = lambda side, st: n.get((side, st), (0, 0))[0]                       # noqa: E731
        out.append(f"live: buys {count('buy', 'filled')} filled, {count('buy', 'failed')} failed, {count('buy', 'skipped')} skipped"
                   f" | sells {count('sell', 'filled')} filled, {count('sell', 'failed')} failed"
                   + (f", {count('sell', 'unsent')} not sent" if count("sell", "unsent") else "") + " | realized "
                   f"{n.get(('sell', 'filled'), (0, 0))[1]:+.4f} SOL | {held[0]} open ({held[1]:.3f} SOL in)"
                   + (f", {held[2]} STUCK: sell by hand" if held[2] else "")
                   + (f" | wallet {wallet['balance']:.3f} SOL at {time.strftime('%H:%M', time.gmtime(wallet['at']))} UTC" if wallet else ""))
    if pairs:
        fees = 2 * TX_COST_SOL      # a round's signatures, priority fees and tips: the same SOL on any size, so a smaller copy pays more of itself
        same = [r[1] - (r[3] + fees / r[5] - fees / r[4]) for r in pairs]
        out.append(f"live vs paper on the same {len(pairs)} copies: live {sum(r[0] for r in pairs):+.4f} SOL, paper "
                   f"{sum(r[2] for r in pairs):+.4f} SOL, live minus paper per copy median {statistics.median(r[1] - r[3] for r in pairs):+.1%}"
                   f", {statistics.median(same):+.1%} with the paper at the live stake")
    return out
