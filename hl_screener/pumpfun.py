"""pump.fun wallets on Solana: who snipes new tokens, who trades them profitably, and could a copier keep it.

    python -m hl_screener pump collect    # stream pump.fun from Solana 24/7 into SQLite, re-rank every 30 min
    python -m hl_screener pump report     # rank wallets now from what was collected

Data: the pump.fun program's own CreateEvent / TradeEvent and, once a token graduates, the PumpSwap pool's
BuyEvent / SellEvent (layouts: github.com/pump-fun/pump-public-docs, idl/pump.json and idl/pump_amm.json),
read from Solana's public RPC with logsSubscribe; slotSubscribe on the same socket measures how many slots
behind the chain each trade reaches us. No key. SOLANA_WS_URL swaps the RPC; SOLANA_WS_FALLBACK is used only
while the main one keeps failing (Helius meters websockets at 2 credits per 0.1 MB: a free plan cannot carry
this stream 24/7, but it can cover outages). Scope: tokens created while the collector runs, quoted in SOL.
PumpSwap trades are stored like curve trades, with the pool's effective reserves after the trade, so prices,
profits and copies run straight through graduation.

Copy cost is not modelled here, it is replayed: a copier that lands `latency_slots` after the wallet
buys at the reserves left by every trade before that slot, and sells the same way after the wallet's
first sell, paying that trade's fees and a fixed per-transaction cost.
"""
from __future__ import annotations

import asyncio
import base64
import collections
import hashlib
import itertools
import json
import logging
import os
import queue
import signal
import sqlite3
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .pumppools import WORKED_S
from .pumptx import PUBLIC_RPC, Rpc, canonical_pool, fresh_coin

log = logging.getLogger(__name__)

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"     # PumpSwap, where graduated tokens trade
PUBLIC_WS = "wss://api.mainnet-beta.solana.com"
D_TRADE = hashlib.sha256(b"event:TradeEvent").digest()[:8]
D_CREATE = hashlib.sha256(b"event:CreateEvent").digest()[:8]
D_BUY = hashlib.sha256(b"event:BuyEvent").digest()[:8]
D_SELL = hashlib.sha256(b"event:SellEvent").digest()[:8]
D_POOL = hashlib.sha256(b"event:CreatePoolEvent").digest()[:8]
SOL_QUOTE = bytes(32)          # quote_mint = default pubkey: the curve is quoted in native SOL
WSOL = "So11111111111111111111111111111111111111112"        # a PumpSwap pool quoted in SOL
FEE = 0.0125                   # 0.95 % protocol + 0.30 % creator per side, the common case (ponytail: per-token creator fees vary; read them from the events if it matters)
# what a copied transaction costs on top of pump.fun's fee. A copy that wants the next block pays for it:
# the signature fee is fixed, the priority fee and the tip are auctions, so these are assumptions, not quotes.
BASE_FEE_SOL = 0.000005        # Solana's signature fee, 5,000 lamports
PRIORITY_SOL = 0.0005          # compute-unit price a fill that cannot wait has to pay
TIP_SOL = 0.001                # Jito tip: what buys a place at the top of the block
TX_COST_SOL = BASE_FEE_SOL + PRIORITY_SOL + TIP_SOL   # charged on the copy's buy and again on its sell
PAPER_STAKE_SOL = 0.25         # each live paper copy (0.1 until 2026-09-26): the size stake_sweep found best for every golden wallet
PAPER_BUY_SLOTS = 4            # where the live copies landed behind their wallet, p50 of 36 rounds (2026-10-07..09): buys 4
PAPER_SELL_SLOTS = 3           # slots, sells 3; the replay's 2 was a slot or two kinder than the chain
LAMPORTS = 1e9
MIN_FREE_GB = 1.0              # below this much free disk, trades are not stored: the server's last space is the system's
LOW_DISK_GB = 3.0              # below this much, prune down to LOW_DISK_KEEP_S of history instead of the full retention
LOW_DISK_KEEP_S = 36 * 3600    # not less than 31 h: mature coins are settled ~30.5 h after creation (pumpmature.LOOK_AT_S)
DISK_WARN_GB = 6.0             # below this much, the log warns once an hour, while there is still time to act
GAP_PACE_S = 0.5               # between two reads after a feed gap (a sold copy costs a few more, paced too): the public RPC
                               # allows 40 per 10 s per method and 100 per 10 s in all, shared with the live copies' calls
GAP_LOOK_BACK_S = 3 * 86_400   # copies opened longer ago are not checked: a wallet that never sells a dead coin would be read on every reconnect
GAP_KEPT = 0.99                # a leader holding less than this share of the tokens we saw it buy sold while we were blind
POOL_READS = 10                # pools read at once for followed wallets' trades in pools we do not follow (see _found): a bot
                               # trading in hundreds of them must not queue reads on the public RPC's 40 per 10 s
WAL_LIMIT = 64 * 2**20         # bytes the write-ahead log keeps after a checkpoint resets it
SILENT_S = 30                  # this long without pump.fun's logs (they come many times a second), a connection is stalled,
                               # even with its slots still coming
FALLBACK_S = 900               # one stretch on the fallback feed
FALLBACK_PER_DAY = 2           # at most this many a UTC day: Helius's free credits cover ~10 h of this stream
SNIPER_TOP = 5                 # snipers copied at any moment: the busiest of the window
SNIPER_EVERY_S = 300           # how often that ranking is redone; it turns over fast
SNIPER_WINDOW_H = 2.0          # the snipes it is ranked on
LATE_STATES = ("x180", "e20", "e50", "c8", "c8x")   # the later entries' states, see _late_states
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    s = ""
    while n:
        n, r = divmod(n, 58)
        s = _B58[r] + s
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + s


# ---------------------------------------------------------------------------
# event decoding (Anchor events in "Program data:" log lines)
# ---------------------------------------------------------------------------
class _Reader:
    def __init__(self, b: bytes):
        self.b, self.o = b, 8                     # after the 8-byte event discriminator

    def take(self, fmt: str) -> Any:
        v = struct.unpack_from(fmt, self.b, self.o)
        self.o += struct.calcsize(fmt)
        return v if len(v) > 1 else v[0]

    def skip(self, n: int) -> None:
        self.o += n

    def pk(self) -> bytes:
        self.o += 32
        return self.b[self.o - 32:self.o]

    def string(self) -> str:
        n = self.take("<I")
        self.o += n
        return self.b[self.o - n:self.o].decode("utf-8", "replace")


TRADE_TAIL = 8     # bytes pump.fun's upgrade of 2026-10-02 ~15:48 UTC appended, undocumented, to TradeEvent, BuyEvent, SellEvent
CREATE_TAIL = 1    # the byte CreateV2 coins' CreateEvent carries past the published layout (2026-10-08 ~16:20 UTC on)


def _whole(r: _Reader, tails: tuple[int, ...] = (0,)) -> bool:
    """The event was read to the end of its layout, give or take a tail pump.fun is known to append. Any other length
    means the layout changed: the event is refused rather than read wrong, and the minute line counts it. The exact
    length alone refused the 8 bytes appended on 2026-10-02, and no trade was stored for three days."""
    return len(r.b) - r.o in tails


def _name(s: str) -> bool:
    """An instruction name, e.g. buy_exact_sol_in: the check that the fields before it sit where the layout says."""
    return 0 < len(s) <= 40 and s.isascii() and s.replace("_", "").isalnum()


def parse_trade(b: bytes) -> dict[str, Any] | None:
    """TradeEvent, or None when the bytes do not fit its layout (as published, or with the tail of 2026-10-02):
    a changed layout must stop the data, not silently corrupt it."""
    try:
        r = _Reader(b)
        mint = r.pk()
        sol, tok, is_buy = r.take("<QQ?")
        user = r.pk()
        ts = r.take("<q")
        vsol, vtok = r.take("<QQ")
        r.skip(16)                                # real reserves
        fee_recipient = r.pk()
        r.skip(8)                                 # fee_basis_points
        fee = r.take("<Q")
        creator = r.pk()
        r.skip(8)                                 # creator_fee_basis_points
        fee += r.take("<Q")                       # creator_fee
        r.skip(1 + 8 * 4)                         # track_volume, (un)claimed tokens, current_sol_volume, last_update
        ix = r.string()                           # ix_name
        mayhem = r.take("<?")
        cashback_bps = r.take("<Q")               # > 0 on a cashback coin: its sells name more accounts
        r.skip(8 * 3)                             # cashback amount, buyback bps/amount
        r.skip(34 * r.take("<I"))                 # shareholders: vec<(pubkey, u16)>
        quote = r.pk()
        r.skip(8 * 5)                             # quote amount/reserves, holder rewards
    except (struct.error, ValueError):
        return None
    if not _whole(r, (0, TRADE_TAIL)) or not _name(ix):
        return None
    return {"mint": mint, "user": user, "buy": is_buy, "sol": sol, "tok": tok, "fee": fee, "ts": ts,
            "vsol": vsol, "vtok": vtok, "sol_quote": quote == SOL_QUOTE, "fee_recipient": fee_recipient, "creator": creator,
            "mayhem": mayhem, "cashback_bps": cashback_bps, "ix": ix}   # what a live copy's transaction needs


def parse_create(b: bytes) -> dict[str, Any] | None:
    try:
        r = _Reader(b)
        name, symbol = r.string(), r.string()
        r.string()                                # uri
        mint = r.pk()
        r.skip(32)                                # bonding_curve
        user = r.pk()                             # the wallet that launched it (its buys are the dev's)
        r.skip(32)                                # creator (fee recipient)
        ts = r.take("<q")
        r.skip(8 * 4)                             # reserves, supply
        token_program = r.pk()
        r.skip(2)                                 # mayhem, cashback
        quote = r.pk()
        r.skip(8 + 8 + 1)                         # virtual_quote_reserves, creator_fee_bps, is_holder_reward
    except (struct.error, ValueError):
        return None
    if not _whole(r, (0, CREATE_TAIL)):
        return None
    return {"mint": mint, "user": user, "name": name[:64], "symbol": symbol[:32], "ts": ts, "sol_quote": quote == SOL_QUOTE,
            "token_program": token_program}


def parse_amm_trade(b: bytes) -> dict[str, Any] | None:
    """PumpSwap BuyEvent / SellEvent, in the same terms as a curve trade: `sol` the amount the pool priced,
    `fee` everything the user paid on top (buy) or lost (sell), `vsol`/`vtok` the pool's effective reserves
    (real + virtual quote) AFTER the trade. The event itself carries the reserves before it: checked on
    13,379 consecutive live trades, and constant product on real + virtual reserves reproduces them."""
    buy = b[:8] == D_BUY
    try:
        r = _Reader(b)
        ts = r.take("<q")
        v = r.take("<13Q")
        pool, user = r.pk(), r.pk()
        r.skip(32 * 2)                            # the user's token accounts
        fee_recipient = r.pk()                    # protocol_fee_recipient
        r.skip(32)                                # its token account
        creator = r.pk()                          # coin_creator
        r.skip(16)                                # creator fee bps/amount
        ix = "sell"
        if buy:
            r.skip(1 + 8 * 4 + 8)                 # track_volume, volume totals, min_base_amount_out
            ix = r.string()                       # ix_name
        cashback_bps = r.take("<Q")
        r.skip(8 * 3)                             # cashback amount, buyback bps/amount
        vq = int.from_bytes(r.b[r.o:r.o + 16], "little", signed=True)
        r.skip(16 + 1 + 8 + 16)                   # virtual_quote_reserves, can_boost, base_supply, holder rewards
    except (struct.error, ValueError):
        return None
    if not _whole(r, (0, TRADE_TAIL)) or not _name(ix):
        return None
    base, B, Q, q, q_net, user_q = v[0], v[4], v[5], v[6], v[11], v[12]
    if buy:
        vtok, vsol, fee = B - base, Q + q_net + vq, user_q - q
    else:
        vtok, vsol, fee = B + base, Q - q_net + vq, q - user_q
    return {"pool": pool, "user": user, "buy": buy, "sol": q, "tok": base, "fee": max(fee, 0), "ts": ts, "vsol": vsol, "vtok": vtok,
            "fee_recipient": fee_recipient, "creator": creator, "cashback_bps": cashback_bps}


def parse_create_pool(b: bytes) -> dict[str, Any] | None:
    try:
        r = _Reader(b)
        r.skip(8 + 2 + 32)                        # timestamp, index, creator
        base, quote = r.pk(), r.pk()
        r.skip(2 + 8 * 7 + 1)                     # decimals, amounts, pool_bump
        pool = r.pk()
        r.skip(32 * 4 + 1 + 8 + 1 + 1)            # lp_mint, token accounts, coin_creator, flags, creator_fee_bps
    except (struct.error, ValueError):
        return None
    if not _whole(r):
        return None
    return {"pool": pool, "base_mint": base, "quote_mint": quote}


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (id INTEGER PRIMARY KEY, addr TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS mints (id INTEGER PRIMARY KEY, addr TEXT UNIQUE NOT NULL, slot INTEGER, ts INTEGER,
                                  creator INTEGER, name TEXT, symbol TEXT);
CREATE TABLE IF NOT EXISTS trades (slot INTEGER, ts INTEGER, mint INTEGER, wallet INTEGER, buy INTEGER,
                                   sol INTEGER, tok INTEGER, fee INTEGER, vsol INTEGER, vtok INTEGER);
CREATE INDEX IF NOT EXISTS ix_trades_mint ON trades(mint, slot);
CREATE INDEX IF NOT EXISTS ix_trades_wallet ON trades(wallet, mint, slot);
CREATE INDEX IF NOT EXISTS ix_mints_ts ON mints(ts);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS follow (wallet TEXT PRIMARY KEY, added_at INTEGER, golden_now INTEGER, report_copy_roi REAL,
                                   golden_ever INTEGER DEFAULT 1, sniper_now INTEGER DEFAULT 0, sniper_rank INTEGER,
                                   mature_ever INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS pfills (id INTEGER PRIMARY KEY, wallet TEXT, mint TEXT, side TEXT, trigger_slot INTEGER,
                                   land_slot INTEGER, ts INTEGER, sol REAL, tok REAL, leader_px REAL, px REAL,
                                   slip_bps REAL, pnl REAL, timed_out INTEGER);
CREATE TABLE IF NOT EXISTS ppos (wallet TEXT, mint TEXT, tok REAL, cost REAL, opened INTEGER, PRIMARY KEY (wallet, mint));
CREATE TABLE IF NOT EXISTS opos (wallet TEXT, mint TEXT, cost REAL, proceeds REAL, tok REAL, PRIMARY KEY (wallet, mint));
CREATE TABLE IF NOT EXISTS psnap (ts INTEGER, wallet TEXT, copy_pnl REAL, copy_cost REAL, own_pnl REAL, own_cost REAL);
CREATE INDEX IF NOT EXISTS ix_psnap ON psnap(wallet, ts);
CREATE INDEX IF NOT EXISTS ix_pfills ON pfills(wallet, mint);   -- the dry run and the go-live rule join each copy to its paper fill
-- one small permanent row per launch: trades are pruned after a few days, this is what keeps a maker's history.
-- It stores the curve state each exit rule reached, not a profit, so the cost assumptions stay changeable.
CREATE TABLE IF NOT EXISTS launches (
  mint TEXT PRIMARY KEY, creator TEXT, slot INTEGER, ts INTEGER, symbol TEXT, graduated INTEGER,
  dev_sold_s REAL, peak REAL, n_trades INTEGER, n_buyers INTEGER,
  entry_vsol INTEGER, entry_vtok INTEGER, maker_vsol INTEGER, maker_vtok INTEGER, end_vsol INTEGER, end_vtok INTEGER,
  be_vsol INTEGER, be_vtok INTEGER, t20_vsol INTEGER, t20_vtok INTEGER, t50_vsol INTEGER, t50_vtok INTEGER,
  t100_vsol INTEGER, t100_vtok INTEGER, fee_in REAL, fee_out REAL, stake REAL, latency INTEGER,
  buyers TEXT, dev_buy REAL,    -- who sniped it (our own wallet ids) and what the maker put into its own bag
  s30_vsol INTEGER, s30_vtok INTEGER, s60_vsol INTEGER, s60_vtok INTEGER,    -- the price 30 s and 60 s in
  x180_vsol INTEGER, x180_vtok INTEGER, e20_vsol INTEGER, e20_vtok INTEGER, e50_vsol INTEGER, e50_vtok INTEGER,
  c8_vsol INTEGER, c8_vtok INTEGER, c8x_vsol INTEGER, c8x_vtok INTEGER, late INTEGER);   -- the later entries (LATE_STATES)
CREATE INDEX IF NOT EXISTS ix_launches_creator ON launches(creator, ts);
CREATE INDEX IF NOT EXISTS ix_launches_ts ON launches(ts);
"""


def connect(path: str | Path, readonly: bool = False) -> sqlite3.Connection:
    p = Path(path)
    if readonly:
        return sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True, timeout=30, check_same_thread=False)
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(p, timeout=30, check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute(f"PRAGMA journal_size_limit={WAL_LIMIT}")   # a reset log shrinks back: pruning once left gigabytes of it
    c.executescript(SCHEMA)
    for ddl in ("ALTER TABLE launches ADD COLUMN buyers TEXT",               # before maker wallets were linked
                "ALTER TABLE launches ADD COLUMN dev_buy REAL",
                *(f"ALTER TABLE launches ADD COLUMN s{s}_{v} INTEGER" for s in (30, 60) for v in ("vsol", "vtok")),
                *(f"ALTER TABLE launches ADD COLUMN {k}_{v} INTEGER" for k in LATE_STATES for v in ("vsol", "vtok")),
                "ALTER TABLE launches ADD COLUMN late INTEGER",            # 1 once the later entries are computed
                "ALTER TABLE mints ADD COLUMN pool TEXT",                    # before PumpSwap tracking
                "ALTER TABLE follow ADD COLUMN golden_ever INTEGER DEFAULT 1",   # before the top snipers were followed too:
                "ALTER TABLE follow ADD COLUMN sniper_now INTEGER DEFAULT 0",    # everyone already there was followed for being golden
                "ALTER TABLE follow ADD COLUMN sniper_rank INTEGER",
                "ALTER TABLE follow ADD COLUMN mature_ever INTEGER DEFAULT 0"):  # before the wallets that buy past $100k
        try:
            c.execute(ddl)
        except sqlite3.OperationalError:
            pass                                               # already there
    return c


def set_meta(c: sqlite3.Connection, key: str, value: Any) -> None:
    c.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))


def get_meta(c: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


# ---------------------------------------------------------------------------
# paper follower: the golden wallets, followed live
# ---------------------------------------------------------------------------
class PaperFollow:
    """Forward test of the golden wallets, out-of-sample by construction: a wallet is followed only from the
    moment a report flags it, and kept after that. Its first buy of each token is copied with `stake_sol`,
    landing `latency_slots` after it, or right after the newest slot the feed has shown if the feed lags more;
    the copy is sold when the wallet first sells, landing `sell_latency_slots` after it. Priced on the live curve
    at the wallet's own fee rate plus `tx_cost_sol` per transaction (signature, priority fee, tip): the report's
    replay rules, at the delays the live copies met rather than the replay's 2 slots.
    Nothing is ever sent to Solana."""

    def __init__(self, c: sqlite3.Connection, latency_slots: int = PAPER_BUY_SLOTS, stake_sol: float = PAPER_STAKE_SOL,
                 tx_cost_sol: float = TX_COST_SOL, sell_latency_slots: int = PAPER_SELL_SLOTS):
        from .pumpmature import MC_LEVELS                      # here: that module builds on this one
        self.c, self.L, self.Ls, self.stake, self.tx = c, latency_slots, sell_latency_slots, stake_sol, tx_cost_sol
        self.follow: set[str] = set()
        self.mature_only: set[str] = set()                     # followed only for buying past $100k: only those buys are copied
        self.mature_mcap = MC_LEVELS["mc100k"]
        self.skip: set[tuple[str, str]] = set()                # (wallet, mint) such a wallet came to under $100k
        self.sell_rate: dict[str, float] = {}                  # mint -> the fee its last sell paid: PumpSwap buys log almost none
        self.sniper_cfg = {"top": SNIPER_TOP, "every_s": SNIPER_EVERY_S, "window_h": SNIPER_WINDOW_H}   # shown on the page
        # the followed wallets' own positions, in SOL and tokens: what the copy is compared with
        self.own = {(w, m): [cost, proceeds, tok] for w, m, cost, proceeds, tok in c.execute("SELECT wallet, mint, cost, proceeds, tok FROM opos")}
        self.reload()
        self.pos = {(w, m): [tok, cost, opened] for w, m, tok, cost, opened in c.execute("SELECT wallet, mint, tok, cost, opened FROM ppos")}
        self.copied = set(c.execute("SELECT DISTINCT wallet, mint FROM pfills WHERE side = 'buy'"))
        self.pending: dict[str, list[dict[str, Any]]] = {}
        self.curve: dict[str, tuple[int, int, float]] = {}     # mint -> reserves after its last trade, when seen
        self.tip = 0                                            # newest slot the feed has shown
        for m in {m for (_, m) in self.pos} | self._held():    # after a restart, mark open positions at the last stored price
            row = c.execute("""SELECT t.vsol, t.vtok FROM trades t JOIN mints mm ON mm.id = t.mint WHERE mm.addr = ?
                               ORDER BY t.slot DESC, t.rowid DESC LIMIT 1""", (m,)).fetchone()
            if row:
                self.curve[m] = (row[0], row[1], time.time())

    def reload(self) -> None:
        """Who is copied right now: every wallet a report ever called golden or found buying past $100k at a profit,
        plus the snipers currently in the top set."""
        rows = self.c.execute("""SELECT wallet, added_at, golden_ever = 1 OR sniper_now = 1 FROM follow
                                 WHERE golden_ever = 1 OR sniper_now = 1 OR mature_ever = 1""").fetchall()
        follow = {w: added for w, added, _ in rows}
        self.mature_only = {w for w, _, other in rows if not other}
        for w in follow.keys() - self.follow:                  # newly followed: the minutes between the ranking and this reload
            if not any(k[0] == w for k in self.own):
                since = max(follow[w] or 0, time.time() - 600)
                for m, buy, sol, tok, fee in self.c.execute(
                        """SELECT mm.addr, t.buy, t.sol, t.tok, t.fee FROM trades t JOIN wallets w ON w.id = t.wallet
                           JOIN mints mm ON mm.id = t.mint WHERE w.addr = ? AND t.ts >= ? ORDER BY t.slot, t.rowid""", (w, since)).fetchall():
                    self._own((w, m), buy, sol, tok, fee)
        self.follow = set(follow)

    def _held(self) -> set[str]:
        return {m for (_, m), p in self.own.items() if p[2] > 0}

    def _own(self, key: tuple[str, str], buy: bool, sol: int, tok: int, fee: int) -> None:
        """The followed wallet's own position in each token it buys once we follow it. A sell counts only for
        the share we saw it buy: the rest it held from before."""
        p = self.own.get(key)
        if buy:
            p = self.own.setdefault(key, [0.0, 0.0, 0.0])
            p[0] += (sol + fee) / LAMPORTS
            p[2] += tok
        elif p and p[2] > 0 and tok > 0:
            p[1] += (sol - fee) / LAMPORTS * min(1.0, p[2] / tok)
            p[2] = max(0.0, p[2] - tok)
        else:
            return
        self.c.execute("INSERT OR REPLACE INTO opos VALUES (?,?,?,?,?)", (*key, *p))

    def _worth(self, mint: str, tok: float) -> float | None:
        """SOL that selling `tok` would fetch at the token's live curve, after the fee; None when never priced."""
        st = self.curve.get(mint)
        return (st[0] - st[0] * st[1] / (st[1] + tok)) * (1 - FEE) / LAMPORTS if st and st[1] > 0 else None

    def on_trade(self, slot: int, mint: str, user: str, e: dict[str, Any]) -> None:
        self.tip = max(self.tip, slot)
        acts = self.pending.get(mint)
        if acts:                                                # copies due: land at the state before this trade
            self._run_due(mint, acts, [slot >= a["land"] for a in acts])
        self.curve[mint] = (e["vsol"], e["vtok"], time.time())
        if not e["buy"] and e["fee"] > 0 and e["sol"] >= 10**7:
            self.sell_rate[mint] = e["fee"] / e["sol"]
        key, side = (user, mint), ("buy" if e["buy"] else "sell")
        mine = [a for a in self.pending.get(mint, ()) if a["wallet"] == user]
        following = user in self.follow
        if not e["tok"] or not (following or key in self.pos or mine):
            return
        if user in self.mature_only and key not in self.own and key not in self.pos and not mine:
            if key in self.skip or not e["buy"] or e["vsol"] * 1e6 < self.mature_mcap * e["vtok"]:
                self.skip.add(key)                              # it came to this coin under $100k: not what it is followed for
                return
        if following:
            self._own(key, e["buy"], e["sol"], e["tok"], e["fee"])
        elif e["buy"]:
            return                                              # out of the top snipers: no new copies, open ones still exit
        if any(a["side"] == side for a in mine):
            return
        if side == "buy" and key in self.copied:
            return                                              # only its first buy of a token is copied
        if side == "sell" and key not in self.pos and not mine:
            return                                              # nothing copied to sell
        if side == "buy":
            self.copied.add(key)
        leader_px = ((e["sol"] + e["fee"]) if e["buy"] else (e["sol"] - e["fee"])) / e["tok"]
        rate = e["fee"] / e["sol"] if e["sol"] else FEE
        if e["buy"] and e["fee"] == 0:                          # a PumpSwap buy logs no fee: it pays what the coin's sells pay
            rate = self.sell_rate.get(mint, FEE)
        late = self.L if e["buy"] else self.Ls
        self.pending.setdefault(mint, []).append({"wallet": user, "side": side, "trigger": slot, "land": max(slot + late, self.tip + 1),
                                                  "rate": rate, "leader_px": leader_px, "t": time.time()})

    def _run_due(self, mint: str, acts: list[dict[str, Any]], due: list[bool], timed_out: bool = False) -> None:
        """Run the queue up to its last due action, in order, so a copied sell never runs before its buy."""
        last = max((i for i, d in enumerate(due) if d), default=-1)
        if last < 0:
            return
        if acts[last + 1:]:
            self.pending[mint] = acts[last + 1:]
        else:
            self.pending.pop(mint, None)
        for a in acts[:last + 1]:
            self._execute(a, mint, timed_out)

    def tick(self) -> None:
        """Copies on a token that went quiet land at its current state: nothing traded since."""
        now = time.time()
        for mint, acts in list(self.pending.items()):
            self._run_due(mint, acts, [now - a["t"] > (a["land"] - a["trigger"]) * 0.4 + 2 for a in acts], timed_out=True)

    def sell_now(self, wallet: str, mint: str) -> bool:
        """Sell a copy whose wallet sold unseen (while the feed was down): at the coin's next trade, or at its last known
        state if it stays quiet. No slippage is measured: there is no leader price to measure it against. False when
        there is nothing left to sell."""
        st = self.curve.get(mint)
        if not st or st[0] <= 0 or st[1] <= 0:
            return False                                        # no price to sell at: _execute would drop the action
        if (wallet, mint) not in self.pos or any(a["wallet"] == wallet and a["side"] == "sell" for a in self.pending.get(mint, ())):
            return False
        slot = max(self.tip, 1)
        self.pending.setdefault(mint, []).append({"wallet": wallet, "side": "sell", "trigger": slot, "land": slot + 1,
                                                  "rate": self.sell_rate.get(mint, FEE), "leader_px": 0.0, "t": time.time()})
        return True

    def _execute(self, a: dict[str, Any], mint: str, timed_out: bool = False) -> None:
        st, key = self.curve.get(mint), (a["wallet"], mint)
        if not st or st[0] <= 0 or st[1] <= 0:
            return
        vsol, vtok, now = st[0], st[1], int(time.time())
        if a["side"] == "buy":
            tok = vtok - vsol * vtok / (vsol + self.stake * LAMPORTS / (1 + a["rate"]))
            if tok <= 0:
                return
            sol, pnl, px = self.stake, None, self.stake * LAMPORTS / tok
            slip = (px / a["leader_px"] - 1) * 1e4 if a["leader_px"] > 0 else None
            self.pos[key] = [tok, self.stake, now]
            self.c.execute("INSERT OR REPLACE INTO ppos VALUES (?,?,?,?,?)", (*key, tok, self.stake, now))
        else:
            held = self.pos.pop(key, None)
            if held is None:
                return
            tok, cost = held[0], held[1]
            sol = (vsol - vsol * vtok / (vtok + tok)) * (1 - a["rate"]) / LAMPORTS
            pnl, px = sol - cost - 2 * self.tx, sol * LAMPORTS / tok
            slip = (1 - px / a["leader_px"]) * 1e4 if a["leader_px"] > 0 else None
            self.c.execute("DELETE FROM ppos WHERE wallet = ? AND mint = ?", key)
        self.c.execute("""INSERT INTO pfills(wallet, mint, side, trigger_slot, land_slot, ts, sol, tok, leader_px, px, slip_bps, pnl, timed_out)
                          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (*key, a["side"], a["trigger"], a["land"], now, sol, tok, a["leader_px"], px, slip, pnl, int(timed_out)))

    def forget(self, max_age_s: float = 6 * 3600) -> None:
        keep = {m for (_, m) in self.pos} | set(self.pending) | self._held()
        cutoff = time.time() - max_age_s
        self.curve = {m: v for m, v in self.curve.items() if v[2] >= cutoff or m in keep}

    def summary(self) -> dict[str, Any]:
        rows = {w: {"wallet": w, "added_at": added, "golden_now": bool(g), "golden_ever": bool(ge), "sniper_now": bool(sn),
                    "sniper_rank": rank, "mature_ever": bool(me), "report_copy_roi": roi, "copied": 0, "closed": 0,
                    "open": 0, "realized": 0.0, "unrealized": 0.0, "wins": 0, "delay_slots": None, "slip_bps": None,
                    "own_cost": 0.0, "own_pnl": 0.0, "invested": 0.0}
                for w, added, g, ge, sn, rank, me, roi in self.c.execute(
                    "SELECT wallet, added_at, golden_now, golden_ever, sniper_now, sniper_rank, mature_ever, report_copy_roi FROM follow")}
        for (w, m), (cost, proceeds, tok) in self.own.items():   # the wallet itself, held tokens at the live curve (0 if never priced)
            if w in rows:
                rows[w]["own_cost"] += cost
                rows[w]["own_pnl"] += proceeds - cost + ((self._worth(m, tok) or 0.0) if tok > 0 else 0.0)
        for w, copied, closed, realized, wins, delay, slip, invested in self.c.execute(
                """SELECT wallet, SUM(side = 'buy'), SUM(side = 'sell'), COALESCE(SUM(pnl), 0), SUM(pnl > 0),
                          AVG(land_slot - trigger_slot), AVG(slip_bps), SUM(CASE WHEN side = 'buy' THEN sol ELSE 0 END)
                   FROM pfills GROUP BY wallet"""):
            if w in rows:
                rows[w].update(copied=copied, closed=closed, realized=realized, wins=wins, delay_slots=delay, slip_bps=slip,
                               invested=invested)
        open_ = []
        for (w, m), (tok, cost, opened) in self.pos.items():
            value = self._worth(m, tok)
            pnl = value - cost - 2 * self.tx if value is not None else None
            if w in rows:
                rows[w]["open"] += 1
                rows[w]["unrealized"] += pnl or 0.0
            open_.append({"wallet": w, "mint": m, "cost": cost, "value": value, "pnl": pnl, "opened": opened})
        for r in rows.values():
            r["total"] = r["realized"] + r["unrealized"]
            r["roi"] = r["total"] / r["invested"] if r["invested"] else None   # over the SOL put in: the stake changed once
            r["own_roi"] = r["own_pnl"] / r["own_cost"] if r["own_cost"] else None
            r["win_rate"] = r["wins"] / r["closed"] if r["closed"] else None
        cols = ("wallet", "mint", "side", "trigger_slot", "land_slot", "ts", "sol", "slip_bps", "pnl", "timed_out")
        recent = [dict(zip(cols, r)) for r in self.c.execute(f"SELECT {', '.join(cols)} FROM pfills ORDER BY id DESC LIMIT 50")]
        return {"at": int(time.time()), "stake_sol": self.stake, "latency_slots": self.L, "sell_latency_slots": self.Ls, "tx_cost_sol": self.tx,
                "tx_cost_parts": {"base_fee": BASE_FEE_SOL, "priority": PRIORITY_SOL, "tip": TIP_SOL},
                "snipers": self.sniper_cfg, "pending": sum(len(v) for v in self.pending.values()),
                "wallets": sorted(rows.values(), key=lambda r: r["added_at"] or 0), "open": open_, "recent": recent}


def update_follow(db_path: str | Path, rep: dict[str, Any]) -> int:
    """Start following every wallet this report flags golden; remember which are still golden now."""
    golden = {r["addr"]: r for r in rep.get("traders", []) if r.get("golden")}
    c = connect(db_path)
    try:
        c.execute("UPDATE follow SET golden_now = 0")
        for addr, r in golden.items():
            c.execute("""INSERT INTO follow(wallet, added_at, golden_now, golden_ever, report_copy_roi) VALUES (?, ?, 1, 1, ?)
                         ON CONFLICT(wallet) DO UPDATE SET golden_now = 1, golden_ever = 1, report_copy_roi = excluded.report_copy_roi""",
                      (addr, int(time.time()), r.get("copy_roi")))
        c.commit()
        return len(golden)
    finally:
        c.close()


SNIPERS_SQL = """
WITH recent AS MATERIALIZED (SELECT id, slot, creator FROM mints WHERE ts >= :since)
SELECT w.addr, COUNT(DISTINCT t.mint) AS snipes
FROM recent m
JOIN trades t INDEXED BY ix_trades_mint ON t.mint = m.id AND t.slot <= m.slot + :snipe AND t.buy = 1 AND t.wallet != m.creator
                                        AND t.fee > 0
JOIN wallets w ON w.id = t.wallet
GROUP BY t.wallet ORDER BY snipes DESC LIMIT :top
"""     # MATERIALIZED: read the window's tokens first, then only the first slots of each, never the whole trades table


def update_snipers(db_path: str | Path, top_n: int = SNIPER_TOP, window_h: float = SNIPER_WINDOW_H,
                   snipe_slots: int = 2) -> list[tuple[str, int]]:
    """Follow the snipers of the moment: the `top_n` wallets that bought the most tokens within `snipe_slots` of
    their creation over the last `window_h`, launchers excluded, and fee-free buys: only pump.fun's mayhem agent buys the
    curve without a fee, on nearly every mayhem coin, and its own trade moves the price after it, so a copy of it lands
    on the move (-7 % a copy on 2026-09-21's trades). That ranking turns over fast, so it is redone
    every few minutes and a wallet is copied only while it is in the set; copies already open still exit on its
    sells. Unlike a golden wallet, which is followed for good, a sniper leaves the moment it drops out."""
    c = connect(db_path)
    try:
        rows = c.execute(SNIPERS_SQL, {"snipe": snipe_slots, "since": time.time() - window_h * 3600, "top": top_n}).fetchall()
        c.execute("UPDATE follow SET sniper_now = 0, sniper_rank = NULL WHERE sniper_now = 1")
        for i, (addr, _) in enumerate(rows, start=1):
            c.execute("""INSERT INTO follow(wallet, added_at, golden_now, golden_ever, sniper_now, sniper_rank) VALUES (?, ?, 0, 0, 1, ?)
                         ON CONFLICT(wallet) DO UPDATE SET sniper_now = 1, sniper_rank = excluded.sniper_rank""",
                      (addr, int(time.time()), i))
        c.commit()
        return rows
    finally:
        c.close()


def paper_series(c: sqlite3.Connection) -> dict[str, list[list[float]]]:
    """Per followed wallet, [ts, copy return, wallet return] from the 5-minute snapshots, starting at 0 the moment it
    was followed. Return = profit over SOL put in: into copies, or into the tokens the wallet bought since then."""
    out = {w: [[added or 0, 0.0, 0.0]] for w, added in c.execute("SELECT wallet, added_at FROM follow")}
    for ts, w, cp, cc, op, oc in c.execute("SELECT ts, wallet, copy_pnl, copy_cost, own_pnl, own_cost FROM psnap ORDER BY wallet, ts"):
        if w in out:
            out[w].append([ts, cp / cc if cc else 0.0, op / oc if oc else 0.0])
    return out


# ---------------------------------------------------------------------------
# collector
# ---------------------------------------------------------------------------
def host_resources(path: Path) -> dict[str, float]:
    """Free disk where the database lives, and free memory (Linux): this store grows about 1.2 GB a day."""
    import shutil
    du = shutil.disk_usage(path)
    out = {"disk_free_gb": round(du.free / 1e9, 1), "disk_total_gb": round(du.total / 1e9, 1)}
    try:
        with open("/proc/meminfo") as fh:
            mem = {k: int(v.split()[0]) for k, v in (line.split(":", 1) for line in fh)}
        out.update(mem_avail_gb=round(mem["MemAvailable"] / 1e6, 1), mem_total_gb=round(mem["MemTotal"] / 1e6, 1))
    except (OSError, KeyError, ValueError):
        pass
    return out


def _no_key(text: str) -> str:
    """Error texts can echo the URL: never let an API key reach the logs or the page."""
    import re
    return re.sub(r"(api[-_]?key=)[^&\s'\"]+", r"\1***", text)


class Collector:
    def __init__(self, db_path: str | Path, ws_url: str = PUBLIC_WS, retention_days: float = 2.0, fallback_url: str | None = None,
                 rpc_url: str = PUBLIC_RPC):
        self.db_path = Path(db_path)
        self.c = connect(self.db_path)
        self.ws_url, self.fallback_url = ws_url, fallback_url or None
        self.retention_s = retention_days * 86_400
        self.wallets: dict[str, int] = dict(self.c.execute("SELECT addr, id FROM wallets"))
        cutoff = time.time() - self.retention_s
        self.mints: dict[str, tuple[int, int]] = {a: (i, ts) for a, i, ts in self.c.execute("SELECT addr, id, ts FROM mints WHERE ts >= ?", (cutoff,))}
        self.pools: dict[str, str] = {p: a for a, p in self.c.execute("SELECT addr, pool FROM mints WHERE pool IS NOT NULL AND ts >= ?", (cutoff,))}
        self.buf: list[tuple] = []
        self.stats: dict[str, Any] = {"since": int(time.time()), "trades": 0, "amm_trades": 0, "mints": 0, "graduated": 0, "non_sol": 0,
                                      "parse_errors": 0, "reconnects": 0, "gap_s": 0.0, "gap_checks": 0, "gap_sold": 0,
                                      **(get_meta(self.c, "stats") or {})}
        self.stats["started"] = int(time.time())
        self.stats["ws"] = ws_url.split("?")[0]              # never store an API key that may sit in the query string
        self.stats["fallback"] = bool(self.fallback_url)
        self.paper = PaperFollow(self.c)
        self.live = None                                      # live copies, simulated or sent (pumplive), set by collect()
        self.last_paper = self.last_snap = 0.0
        self.tip = 0                                          # the chain's newest slot (processed), from slotSubscribe
        self.lags: collections.deque[int] = collections.deque(maxlen=20_000)
        self.sigs: collections.deque[str] = collections.deque(maxlen=20_000)
        self.sig_set: set[str] = set()
        self.fails, self.backoff, self.fallback_until = 0, 1.0, 0.0     # the feed's recent failures (see _dropped)
        self.fallback_day, self.fallback_used = "", 0
        self.last_db_error = 0.0
        self.disk_alarm = (0.0, logging.NOTSET)                 # when the log last warned about the disk, and how loudly
        self.halt = threading.Event()                           # set on the way out: the background work stops
        self.rpc = Rpc(rpc_url)                                 # reads only: what a copied wallet holds after a feed gap
        self.gap_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gapcheck")
        self.gap_job = None                                     # the newest gap check, running or waiting
        self.gap_out: queue.SimpleQueue = queue.SimpleQueue()   # (kind, wallet, mint, coin now) whose wallet sold, to the feed's thread
        self.pool_reads = ThreadPoolExecutor(max_workers=1, thread_name_prefix="poolread")   # not behind a gap check's minutes
        self.finding: dict[str, tuple[Any, list[tuple]]] = {}  # pool -> (the read of its coin, the trades waiting for it)
        self.not_ours: set[str] = set()                         # pools read and found not a pump.fun coin's SOL pool
        from .pumppools import PoolFeed                         # our pools and followed wallets, one subscription each (see pumppools)
        self.pool_feed = PoolFeed(ws_url, self.on_logs, pinned=lambda: self._pinned_pools() | self._watched())
        for pool, last in self.c.execute("""SELECT m.pool, (SELECT t.ts FROM trades t WHERE t.mint = m.id ORDER BY t.slot DESC LIMIT 1)
                                             FROM mints m WHERE m.pool IS NOT NULL AND m.ts >= ?""", (cutoff,)):
            self.pool_feed.add(pool, last or 0.0)

    def _pinned_pools(self) -> set[str]:
        """Pools where a copy, paper or live, is open or on its way: their leader's sell, and a sale by hand of a live
        copy, must still reach us. A live copy pins its own pool: the paper's may have closed, or never opened."""
        open_ = {m for (_, m) in self.paper.pos} | set(self.paper.pending)
        if self.live is not None:
            open_ |= set(self.live.pos) | {o["mint"] for o in self.live._orders()}
        return {p for p, m in self.pools.items() if m in open_}

    def _watched(self) -> set[str]:
        """Wallets whose every trade must reach us, on a coin or pool we follow or not: the followed ones, paper and live,
        and those a copy is open on (a sniper out of the top set still exits). Each has its own subscription."""
        out = self.paper.follow | {w for w, _ in self.paper.pos}
        if self.live is not None:
            out |= self.live.targets | {p["wallet"] for p in self.live.pos.values()}
        return out

    def wallet_id(self, addr: str) -> int:
        i = self.wallets.get(addr)
        if i is None:
            i = self.c.execute("INSERT INTO wallets(addr) VALUES (?)", (addr,)).lastrowid
            self.wallets[addr] = i
        return i

    def on_logs(self, slot: int, logs: list[str], sig: str | None = None) -> None:
        if self.finding:
            self._found()                                  # pools read meanwhile: their waiting trades go before this one
        if sig:                                            # a transaction touching both programs arrives on both feeds,
                                                           # and a followed wallet's trade on its own subscription too
            if sig in self.sig_set:
                return
            if len(self.sigs) == self.sigs.maxlen:
                self.sig_set.discard(self.sigs[0])
            self.sigs.append(sig)
            self.sig_set.add(sig)
        if self.tip:
            self.lags.append(max(0, self.tip - slot))      # how far behind the chain this trade reached us
            self.paper.tip = max(self.paper.tip, self.tip)
        stack: list[str] = []                              # which program is running: only its own events count
        for line in logs:
            if not line.startswith("Program data: "):
                if line.startswith("Program ") and " invoke [" in line:
                    stack.append(line.split(" ", 2)[1])
                elif line.startswith("Program ") and (line.endswith(" success") or " failed" in line) and stack:
                    stack.pop()
                continue
            program = stack[-1] if stack else None
            if program not in (PUMP_PROGRAM, AMM_PROGRAM):
                continue                                   # another program's event, even if it shares a name
            try:
                b = base64.b64decode(line[14:])
            except ValueError:
                continue
            if program == AMM_PROGRAM:
                if b[:8] not in (D_BUY, D_SELL, D_POOL):
                    continue
            elif b[:8] not in (D_CREATE, D_TRADE):
                continue
            if b[:8] in (D_BUY, D_SELL):
                e = parse_amm_trade(b)
                if e is None:
                    self.stats["parse_errors"] += 1
                    continue
                pool = b58(e["pool"])
                mint = self.pools.get(pool)
                if pool in self.finding:                   # its coin is being read: the trade waits behind the first, in order
                    self.finding[pool][1].append((slot, b58(e["user"]), e))
                elif mint is not None:                     # a pool of a token we track, after its graduation
                    self.stats["amm_trades"] += 1
                    self.pool_feed.touch(pool)
                    self._trade(slot, mint, b58(e["user"]), e)
                elif pool not in self.not_ours and b58(e["user"]) in self._watched():
                    if len(self.finding) < POOL_READS:
                        self.finding[pool] = (self.pool_reads.submit(self._pool_mint, pool), [(slot, b58(e["user"]), e)])
                    else:
                        self.stats["pool_reads_full"] = self.stats.get("pool_reads_full", 0) + 1   # a followed trade not copied
            elif b[:8] == D_POOL:
                e = parse_create_pool(b)
                if e is None:
                    self.stats["parse_errors"] += 1
                    continue
                if b58(e["quote_mint"]) != WSOL:
                    continue
                mint, pool = b58(e["base_mint"]), b58(e["pool"])
                if mint in self.mints and pool not in self.pools:
                    self.pools[pool] = mint
                    self.pool_feed.add(pool)               # its trades come on a pool subscription from now on
                    self.c.execute("UPDATE mints SET pool = ? WHERE addr = ?", (pool, mint))
                    self.stats["graduated"] += 1
            elif b[:8] == D_CREATE:
                e = parse_create(b)
                if e is None:
                    self.stats["parse_errors"] += 1
                elif not e["sol_quote"]:
                    self.stats["non_sol"] += 1
                else:
                    addr = b58(e["mint"])
                    cur = self.c.execute("INSERT OR IGNORE INTO mints(addr, slot, ts, creator, name, symbol) VALUES (?,?,?,?,?,?)",
                                         (addr, slot, e["ts"], self.wallet_id(b58(e["user"])), e["name"], e["symbol"]))
                    if cur.rowcount:
                        self.mints[addr] = (cur.lastrowid, e["ts"])
                        self.stats["mints"] += 1
                    if self.live is not None:
                        try:
                            self.live.on_create(addr, e["token_program"])
                        except Exception:  # noqa: BLE001 - the live copies must never stop the feed
                            log.exception("live copies failed on the creation of %s", addr)
            elif b[:8] == D_TRADE:
                e = parse_trade(b)
                if e is None:
                    self.stats["parse_errors"] += 1
                    continue
                if e["sol_quote"]:
                    self._trade(slot, b58(e["mint"]), b58(e["user"]), e)

    def _pool_mint(self, pool: str) -> str | None:
        """On a worker thread: the coin a PumpSwap pool trades, from the pool's account (base mint at byte 43), or None
        when it is not the pool pump.fun migrates a coin into (another quote, another launchpad's token). A pool just
        migrated can be unknown yet to a lagging node behind the public RPC: read up to 3 times, GAP_PACE_S apart."""
        for n in range(3):
            if n and self.halt.wait(GAP_PACE_S):
                break                                      # the collector is stopping
            try:
                acct, why = self.rpc.account(pool), ValueError(f"no account {pool} on the RPC yet")
            except Exception as e:  # noqa: BLE001 - read again; _found says it if every read fails
                acct, why = None, e
            if acct is not None:
                mint = b58(acct[1][43:75]) if acct[0] == AMM_PROGRAM and len(acct[1]) >= 75 else None
                return mint if mint and canonical_pool(mint) == pool else None
        raise why

    def _found(self) -> None:
        """A followed wallet traded in a pool we do not follow: one quiet for over an hour and dropped, one migrated while
        the feed was down, or a coin older than the ones we track. In the live test's first hours that was 3 of the
        leader's 4 PumpSwap first buys, never seen. Its own subscription brings such a trade now, and the pool's account
        names the coin (_pool_mint): the pool is followed from here on (an older coin's while a copy is open on it), and
        the trades that waited go to the copies as the pool's feed would have brought them, so a first buy is copied at
        the reserves it left. On the feed's thread."""
        for pool in [p for p, (job, _) in self.finding.items() if job.done()]:
            job, held = self.finding.pop(pool)
            try:
                mint = job.result()
            except Exception as e:  # noqa: BLE001 - this read's failure: the wallet's next trade there reads it again
                log.warning("could not read the PumpSwap pool %s (%s): %d trade(s) of followed wallets in it not copied",
                            pool, _no_key(f"{type(e).__name__}: {e}")[:200], len(held))
                continue
            if mint is None:
                self.not_ours.add(pool)                    # never read again
                continue
            self.pools[pool] = mint
            if mint in self.mints:                         # a coin we track: followed and stored, as after its CreatePoolEvent
                self.pool_feed.add(pool)
                try:
                    self.c.execute("UPDATE mints SET pool = ? WHERE addr = ?", (pool, mint))
                except sqlite3.OperationalError as err:    # a full disk must not cost the trades below
                    self._db_error(err)
            else:                                          # an older one: subscribed only while a copy is open on it
                self.pool_feed.kick.set()                  # (_pinned_pools), so a busy wallet cannot crowd out ours
            self.stats["pools_found"] = self.stats.get("pools_found", 0) + 1
            log.info("PumpSwap pool %s of %s found through %s's trade: followed from now on", pool, mint, held[0][1])
            for slot, user, e in held:
                self.stats["amm_trades"] += 1
                self._trade(slot, mint, user, e)

    def _db_error(self, e: sqlite3.OperationalError) -> None:
        if time.time() - self.last_db_error >= 60:                # once a minute: a full disk fails every write
            log.warning("database write failed (%s): skipped, still following the feed", e)
            self.last_db_error = time.time()

    def _trade(self, slot: int, mint: str, user: str, e: dict[str, Any]) -> None:
        try:
            self.paper.on_trade(slot, mint, user, e)              # followed wallets are copied on any token
        except sqlite3.OperationalError as err:                  # its fills are written here: a full disk must not stop the feed
            self._db_error(err)
        if self.live is not None:
            try:
                self.live.on_trade(slot, mint, user, e)
            except sqlite3.OperationalError as err:
                self._db_error(err)
            except Exception:  # noqa: BLE001 - the live copies must never stop the feed
                log.exception("live copies failed on a trade of %s", mint)
        m = self.mints.get(mint)
        if m is None:
            return                                                # born before we started watching: not stored
        self.buf.append((slot, e["ts"], m[0], self.wallet_id(user), int(e["buy"]),
                         e["sol"], e["tok"], e["fee"], e["vsol"], e["vtok"]))

    def flush(self) -> None:
        """Write the buffered trades and the paper follower's state. A write that fails (a full disk did, 17 restarts in
        a row) drops that batch and keeps the feed going: the collector must outlive its own storage."""
        try:
            self._flush()
        except sqlite3.OperationalError as e:
            try:
                self.c.rollback()
            except sqlite3.Error:
                pass
            self.stats["skipped_low_disk"] = self.stats.get("skipped_low_disk", 0) + len(self.buf)
            self.buf.clear()
            self._db_error(e)

    def _flush(self) -> None:
        if self.buf:
            self.stats["paused_low_disk"] = self.stats.get("disk_free_gb", MIN_FREE_GB) < MIN_FREE_GB
            if self.stats["paused_low_disk"]:                     # a full disk takes the whole server down: skip, keep following
                self.stats["skipped_low_disk"] = self.stats.get("skipped_low_disk", 0) + len(self.buf)
            else:
                self.c.executemany("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?)", self.buf)
                self.stats["trades"] += len(self.buf)
            self.buf.clear()
        while not self.halt.is_set() and not self.gap_out.empty():   # what a gap check found, acted on here, where the state lives;
            self._gap_close(*self.gap_out.get_nowait())                 # not on the way out: a sell sent then would never be settled
        self.paper.tick()
        if self.live is not None:
            try:
                self.live.tick()
            except sqlite3.OperationalError:
                raise                                             # flush() handles a full disk
            except Exception:  # noqa: BLE001 - the live copies must never stop the feed
                log.exception("live copies failed")
        now = time.time()
        if now - self.last_paper >= 10:
            self.paper.reload()                                   # the report thread adds golden wallets
            summ = self.paper.summary()
            set_meta(self.c, "paper", summ)
            if now - self.last_snap >= 300:                       # the chart: copy and wallet, every 5 min
                self.c.executemany("INSERT INTO psnap VALUES (?,?,?,?,?,?)", [
                    (int(now), r["wallet"], r["total"], r["invested"], r["own_pnl"], r["own_cost"])
                    for r in summ["wallets"] if r["copied"] or r["golden_ever"] or r["sniper_now"] or r["mature_ever"]])   # a sniper that never traded needs no line
                self.last_snap = now
            if self.lags:
                s = sorted(self.lags)
                self.stats.update({f"lag_p{p}": s[min(len(s) - 1, len(s) * p // 100)] for p in (50, 90, 99)})
            self.stats.update(host_resources(self.db_path.parent), **self.pool_feed.stats)
            self._warn_disk()
            self.last_paper = now
        set_meta(self.c, "stats", self.stats)
        self.c.commit()

    def _warn_disk(self) -> None:
        """Say it in the log before the disk fills: a warning an hour under DISK_WARN_GB, an error an hour under
        LOW_DISK_GB, the first error at once."""
        free = self.stats.get("disk_free_gb")
        if free is None or free >= DISK_WARN_GB:
            return
        level = logging.ERROR if free < LOW_DISK_GB else logging.WARNING
        if time.time() - self.disk_alarm[0] < 3600 and level <= self.disk_alarm[1]:
            return
        self.disk_alarm = (time.time(), level)
        log.log(level, "disk %g of %g GB free: under %g GB the collector keeps %g h of trades, under %g GB it stops storing them",
                free, self.stats.get("disk_total_gb") or 0, LOW_DISK_GB, LOW_DISK_KEEP_S / 3600, MIN_FREE_GB)

    def check_gap(self, why: str) -> None:
        """A copy exits on its wallet's first sell, and a sell made while the feed was down (a reconnect, a restart) is
        never seen: that copy would stay open. So after each gap one job reads what every copied wallet holds now, off
        the feed's thread, and _flush closes the copies whose wallet sold. Copies older than GAP_LOOK_BACK_S are left alone."""
        since = time.time() - GAP_LOOK_BACK_S
        live = [("live", p["wallet"], m) for m, p in (self.live.pos.items() if self.live is not None else ())
                if not p.get("stuck") and p["opened"] >= since]
        paper = [("paper", w, m) for (w, m), (_, _, opened) in self.paper.pos.items() if opened >= since]
        todo = [(kind, w, m, self.paper.own[(w, m)][2] if (w, m) in self.paper.own else 0.0)   # what we saw it buy
                for kind, w, m in live + paper]
        if not todo:
            return
        if self.gap_job is not None:
            self.gap_job.cancel()                                 # one still waiting is replaced by this newer list
        self.gap_job = self.gap_pool.submit(self._gap_job, todo, why)
        self.stats["gap_checks"] += 1
        log.info("gap check after the %s: reading what the wallets of %d open copies hold", why, len(todo))

    def _gap_job(self, todo: list[tuple[str, str, str, float]], why: str) -> None:
        """On the worker thread: each wallet's balance of the coin, one read every GAP_PACE_S, real money first, and for
        a paper copy whose wallet sold, the coin as the chain has it now, to price the exit (a few more reads, rare; a
        live sell reads it itself). It touches the RPC and the queue only. A wallet that cannot be read keeps its copy."""
        failed = []
        for kind, wallet, mint, bought in todo:
            if self.halt.wait(GAP_PACE_S):
                return                                            # the collector is stopping
            try:
                accounts = self.rpc.call("getTokenAccountsByOwner", [wallet, {"mint": mint},
                                                                     {"encoding": "jsonParsed", "commitment": "confirmed"}])["value"]
                held = sum(int(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]) for a in accounts)
            except Exception as e:  # noqa: BLE001 - one unreadable wallet must not end the check
                failed.append(f"{type(e).__name__}: {e}")
                continue
            if held == 0 or held < GAP_KEPT * bought:
                if kind == "paper" and self.halt.wait(GAP_PACE_S):
                    return
                self.gap_out.put((kind, wallet, mint, self._coin_now(mint) if kind == "paper" else None))
        if failed:
            log.warning("gap check after the %s: %d of %d wallets could not be read (%s): their copies stay open",
                        why, len(failed), len(todo), _no_key(failed[0])[:200])

    def _coin_now(self, mint: str) -> dict[str, Any] | None:
        """On the worker thread: the coin's curve or pool as the chain has it now, or None when it cannot be read."""
        try:
            return fresh_coin(self.rpc, mint, self.rpc.account(mint)[0])      # the mint's owner is its token program
        except Exception as e:  # noqa: BLE001 - the copy still sells, at the last state the feed saw
            log.info("gap check: could not read %s from the chain (%s): its copy sells at the last state seen",
                     mint, _no_key(f"{type(e).__name__}: {e}")[:200])
            return None

    def _gap_close(self, kind: str, wallet: str, mint: str, coin: dict[str, Any] | None) -> None:
        if kind == "paper":
            if coin is not None:                                  # priced at the curve now, not at the one before the gap
                self.paper.curve[mint] = (coin["vsol"], coin["vtok"], time.time())
            if not self.paper.sell_now(wallet, mint):
                if (wallet, mint) in self.paper.pos and not self.paper.curve.get(mint):
                    log.info("gap check: %s sold %s, but the coin has no known price yet: the copy waits for its next trade", wallet, mint)
                return                                            # closed meanwhile, its sell on its way, or nothing to price it at
        else:
            p = self.live.pos.get(mint) if self.live is not None else None
            if p is None or p["wallet"] != wallet or p.get("stuck") or self.live._busy(mint):
                return                                            # closed meanwhile, left to its owner, or its sell is out
            self.live.coins.pop(mint, None)                       # so the sell reads the coin from the chain, not from before the gap
            try:
                self.live._sell(mint, None, "its wallet sold while the feed was down")
            except sqlite3.OperationalError:
                raise                                             # flush() handles a full disk
            except Exception:  # noqa: BLE001 - the live copies must never stop the feed
                log.exception("live copies failed to sell %s", mint)
                return
        log.warning("gap check: %s sold %s while the feed was down: closing the %s copy", wallet, mint, kind)
        self.stats["gap_sold"] += 1

    def forget_old_mints(self) -> None:
        cutoff = time.time() - self.retention_s
        self.mints = {a: v for a, v in self.mints.items() if v[1] >= cutoff}
        self.paper.forget()

    def _dropped(self, worked_s: float, on_fallback: bool) -> float:
        """A feed connection ended after bringing pump.fun's logs for `worked_s` seconds: returns how long to wait before
        the next one. One refused, silent or dropped within WORKED_S is a failure: the wait grows, up to 60 s, as a client
        reconnecting in a loop needs, and after two in a row the fallback takes over for FALLBACK_S, FALLBACK_PER_DAY
        times a day. One that worked longer was closed by the server, which the public RPC does every 1-5 min since
        2026-09-29 (close code 1002): the next one opens in 2 s, where a wait doubling to 60 s cost up to half the trades,
        and the failures start again from none, so a single refusal after it spends none of the fallback's day."""
        worked = worked_s >= WORKED_S
        self.fails = 0 if worked else self.fails + 1
        self.backoff = 2.0 if worked else min(self.backoff * 2, 60.0)
        if on_fallback and worked_s < 10:                     # the fallback refuses us (Helius answered 429 on 2026-09-25):
            self.fallback_until = 0.0                          # back to the main feed, which at least lets some through
            return self.backoff
        day = time.strftime("%Y-%m-%d", time.gmtime())
        if day != self.fallback_day:
            self.fallback_day, self.fallback_used = day, 0
        if self.fallback_url and not on_fallback and self.fails >= 2 and self.fallback_used < FALLBACK_PER_DAY:
            self.fallback_until = time.time() + FALLBACK_S
            self.fallback_used += 1
            log.warning("main feed failed %d times in a row: using the fallback for %.0f min (%d of %d today)",
                        self.fails, FALLBACK_S / 60, self.fallback_used, FALLBACK_PER_DAY)
        self.stats["fallback_used_today"] = self.fallback_used
        return self.backoff

    async def _flush_loop(self, stop: asyncio.Event) -> None:
        """flush() once a second, whatever the feeds do. The pool feeds write through this connection too, and while
        only a pump.fun message led to a flush, a pool trade's write stayed uncommitted for as long as that feed was
        silent or waiting to reconnect (up to 60 s): the maintenance thread gave up on the lock (2026-09-29, "database
        is locked" in the prune and the report). A failure other than the disk's stops the collector, as it always did."""
        while not stop.is_set():
            await asyncio.sleep(1)
            try:
                self.flush()
            except Exception:  # noqa: BLE001 - said, then the collector stops and the container restarts it
                log.exception("writing failed: stopping")
                stop.set()

    async def run(self, stop: asyncio.Event | None = None) -> None:
        import websockets  # here so `pump report` works without the package
        stop = stop or asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):   # PID 1 in the container ignored a redeploy's SIGTERM until the kill, and the
            try:                                       # RPC went on counting the sockets never closed: it refused the next container
                asyncio.get_running_loop().add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):   # Windows, or not the main thread: Ctrl+C still ends it
                pass
        last_line, last_forget = time.time(), time.time()
        gap_from = None
        seen = (self.stats["trades"], self.stats["mints"], self.stats["parse_errors"])
        pools = asyncio.create_task(self.pool_feed.run(stop))   # beside the pump.fun feed, on the public RPC
        writes = asyncio.create_task(self._flush_loop(stop))
        self.check_gap("restart")                               # the copies loaded from the database: were they sold meanwhile?
        while not stop.is_set():
            on_fallback = bool(self.fallback_url) and time.time() < self.fallback_until
            url = self.fallback_url if on_fallback else self.ws_url
            opened = heard = time.time()                          # heard: pump.fun's logs, last seen on this connection
            try:
                async with websockets.connect(url, max_size=2**24, max_queue=4096, ping_interval=20, ping_timeout=30,
                                              close_timeout=5) as ws:     # 5 s for a closing handshake: a stop fits its 15 s grace
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",   # not all of PumpSwap:
                                              "params": [{"mentions": [PUMP_PROGRAM]}, {"commitment": "confirmed"}]}))
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": 3, "method": "slotSubscribe"}))   # our pools, in pumppools
                    self.stats["ws"] = ("fallback: " if on_fallback else "") + url.split("?")[0]
                    log.info("connected to %s", self.stats["ws"])
                    opened = heard = time.time()
                    while not stop.is_set():
                        try:
                            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=1))   # a second at most: a stop is seen at once
                        except asyncio.TimeoutError:
                            msg = {}
                        method = msg.get("method")
                        if method == "slotNotification":
                            self.tip = max(self.tip, msg["params"]["result"]["slot"])
                        elif method == "logsNotification":             # the slots alone keep neither the feed nor the page's "live"
                            heard = time.time()
                            self.stats["heartbeat"] = int(heard)
                            if gap_from is not None:                   # listening again: the gap ends here, not at the connect
                                self.stats["gap_s"] = round(self.stats.get("gap_s", 0) + heard - gap_from, 1)
                                gap_from = None
                                self.check_gap("reconnect")
                            res = msg["params"]["result"]
                            if not res["value"].get("err"):
                                self.on_logs(res["context"]["slot"], res["value"]["logs"], res["value"].get("signature"))
                        elif "error" in msg:
                            why = f"subscription {msg.get('id')} refused: {_no_key(str(msg['error']))[:160]}"
                            if msg.get("id") == 1:
                                raise ConnectionError(why)             # pump.fun's logs will not come: no use waiting SILENT_S
                            log.warning("feed %s", why)
                        now = time.time()
                        if now - heard >= SILENT_S:                    # after each frame or second, not on a timeout only: the slots never stop
                            raise TimeoutError(f"{SILENT_S:g} s without pump.fun's logs")
                        if on_fallback and now >= self.fallback_until:
                            log.info("fallback window over: back to the main feed")
                            gap_from = gap_from or now                 # blind until the main feed's first logs
                            break
                        if now - last_line >= 60:
                            pf, unread = self.pool_feed.stats, self.stats["parse_errors"] - seen[2]
                            log.info("last minute: %d trades, %d new tokens (total %d / %d) | delay %s slot(s) | pools %s of %s followed on "
                                     "%s connections, %s drops | disk %s of %s GB free, memory %s of %s GB free%s%s",
                                     self.stats["trades"] - seen[0], self.stats["mints"] - seen[1], self.stats["trades"], self.stats["mints"],
                                     self.stats.get("lag_p50"), pf.get("pools_followed"), pf.get("pools_wanted"), pf.get("pool_conns"),
                                     pf.get("pool_drops"), self.stats.get("disk_free_gb"), self.stats.get("disk_total_gb"),
                                     self.stats.get("mem_avail_gb", "?"), self.stats.get("mem_total_gb", "?"),
                                     f" | NOT STORING trades: under {MIN_FREE_GB:g} GB of disk free" if self.stats.get("paused_low_disk") else "",
                                     f" | {unread} events not understood: has pump.fun changed a layout?" if unread else "")
                            seen, last_line = (self.stats["trades"], self.stats["mints"], self.stats["parse_errors"]), now
                        if now - last_forget >= 3600:
                            self.forget_old_mints()
                            last_forget = now
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - any feed failure: keep what we have, reconnect
                gap_from = gap_from or time.time()
                self.flush()
                self.stats["reconnects"] += 1
                self.stats["last_error"] = _no_key(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {type(e).__name__}: {e}")[:300]
                lived = time.time() - opened
                wait = self._dropped(heard - opened, on_fallback)
                log.warning("feed error after %.0fs connected (%s); reconnecting in %.0fs", lived, self.stats["last_error"], wait)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=wait)   # a stop cuts the wait short
                except asyncio.TimeoutError:
                    pass
        self.halt.set()                                          # the gap worker stops reading: its findings wait for the next start
        pools.cancel()
        writes.cancel()
        await asyncio.gather(pools, writes, return_exceptions=True)   # the pools' sockets close properly too (PoolFeed.run)
        self.flush()
        log.info("stopped: the feeds are closed and what they brought is written")


def prune(db_path: str | Path, retention_s: float) -> int:
    """Drop tokens older than the retention window with all their trades, in short transactions."""
    c = connect(db_path)
    try:
        cutoff = time.time() - retention_s
        dropped = 0
        while True:
            ids = [r[0] for r in c.execute("SELECT id FROM mints WHERE ts < ? LIMIT 200", (cutoff,))]
            if not ids:
                return dropped
            q = ",".join("?" * len(ids))
            c.execute(f"DELETE FROM trades WHERE mint IN ({q})", ids)
            c.execute(f"DELETE FROM mints WHERE id IN ({q})", ids)
            c.commit()
            dropped += len(ids)
    finally:
        c.close()


def checkpoint(db_path: str | Path) -> tuple[int, int, int] | None:
    """Fold the write-ahead log into the database and cut the file back. Prunes delete in bulk and the report's long
    reads keep the log from resetting meanwhile; on 2026-09-25 the disk filled right after pruning began."""
    c = connect(db_path)
    try:
        return c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        c.close()


def _pct(x: float | None, nd: int = 1) -> str:
    return f"{x:+.{nd}%}" if x is not None else "n/a"


def paper_lines(db_path: str | Path) -> list[str]:
    """The paper follower's forward results for the log, by why each wallet is followed: the page is behind a password,
    and these numbers are what tests every ranking on the trades that came after it."""
    c = connect(db_path, readonly=True)
    try:
        summ = get_meta(c, "paper") or {}
        stake = summ.get("stake_sol") or PAPER_STAKE_SOL
        # each golden wallet's copies at today's size alone: the go-live call is made on these, not on the 0.1 SOL ones
        at_stake = c.execute("""SELECT b.wallet, COUNT(*), COUNT(s.pnl), COALESCE(SUM(s.pnl), 0), COALESCE(SUM(s.pnl > 0), 0)
                                FROM pfills b JOIN follow f ON f.wallet = b.wallet AND f.golden_ever = 1
                                LEFT JOIN pfills s ON s.wallet = b.wallet AND s.mint = b.mint AND s.side = 'sell'
                                WHERE b.side = 'buy' AND ABS(b.sol - ?) < 1e-9 GROUP BY b.wallet ORDER BY b.wallet""", (stake,)).fetchall()
    finally:
        c.close()
    ws = summ.get("wallets") or []
    rate = lambda won, n: f"{won / n:.0%}" if n else "n/a"      # noqa: E731
    groups = {"bought past $100k": lambda r: r.get("mature_ever"), "golden": lambda r: r.get("golden_ever"),
              "snipers": lambda r: not r.get("golden_ever") and not r.get("mature_ever")}
    out = []
    for name, keep in groups.items():
        g = [r for r in ws if keep(r)]
        copied, closed = sum(r["copied"] or 0 for r in g), sum(r["closed"] or 0 for r in g)
        if not copied:
            continue
        total, own_cost, invested = (sum(r.get(k, 0.0) for r in g) for k in ("total", "own_cost", "invested"))
        out.append(f"paper {name}: {len(g)} wallets, {copied} copies ({closed} closed), {total:+.3f} SOL = "
                   f"{_pct(total / invested if invested else None)} per copy, won {rate(sum(r['wins'] or 0 for r in g), closed)}; "
                   f"the wallets themselves {_pct(sum(r['own_pnl'] for r in g) / own_cost if own_cost else None)}")
    for r in ws:
        if r.get("mature_ever") and r["copied"]:
            out.append(f"paper bought past $100k {r['wallet']}: {r['copied']} copies ({r['closed']} closed), {r['total']:+.3f} SOL "
                       f"= {_pct(r['roi'])} per copy, won {rate(r['wins'] or 0, r['closed'])}; itself {_pct(r['own_roi'])}")
    for w, n, closed, pnl, won in at_stake:
        out.append(f"paper golden {w} at {stake:g} SOL: {n} copies ({closed} closed), {pnl:+.3f} SOL = "
                   f"{_pct(pnl / (closed * stake) if closed else None)} per closed copy, won {rate(won, closed)}")
    return out


def _rules_line(rules: list[dict[str, Any]], detail: bool = False) -> str:
    """Every rule of one group for the log: 'tp100 -2.0% [-2%/-2%] n37217, ...', both halves in brackets; with
    `detail`, also the win rate, the median trade and the average without the two best."""
    def one(r: dict[str, Any]) -> str:
        s = f"{r['rule']} {_pct(r['roi'])} [{_pct(r['roi_h1'], 0)}/{_pct(r['roi_h2'], 0)}] n{r['n']}"
        return s + (f" won {r['win_rate']:.0%} median {_pct(r.get('median'))} without top 2 {_pct(r.get('roi_ex2'))}" if detail else "")
    return ", ".join(one(r) for r in rules)


def _best(rules: list[dict[str, Any]] | None) -> str:
    """The top rule of one cohort for the log line, e.g. 'tp100 -1.9%'."""
    b = (rules or [{}])[0]
    return f"{b['rule']} {b['roi']:+.1%}" if b.get("roi") is not None else "n/a"


def collect(db_path: str | Path, ws_url: str, retention_days: float, report_every_s: float = 1800,
            fallback_url: str | None = None, sniper_every_s: float = SNIPER_EVERY_S, sniper_top: int = SNIPER_TOP) -> int:
    logging.getLogger(__name__).setLevel(logging.INFO)
    logging.getLogger("hl_screener.pumplive").setLevel(logging.INFO)    # the live copies' sent, bought, sold and skipped lines
    # the gap checks read about twice a second: on the public RPC, not on PUMP_LIVE_RPC, where a keyed free plan's
    # credits would last days and the live copies' own reads would be refused with them
    col = Collector(db_path, ws_url, retention_days, fallback_url, rpc_url=os.environ.get("PUMP_GAP_RPC") or PUBLIC_RPC)
    col.paper.sniper_cfg = {"top": sniper_top, "every_s": sniper_every_s, "window_h": SNIPER_WINDOW_H}
    from .pumplive import LiveCfg, LiveFollow, live_lines, load_keypair   # here: those modules build on this one
    from .pumpgo import go_lines
    try:
        live = LiveCfg.from_env()
        if live.mode != "off":
            col.live = LiveFollow(col.c, live, keypair=load_keypair() if live.sends else None)
            who = ", ".join(sorted(live.wallets)) or "the golden wallets"
            print(f"live copies: {live.mode}{f' from {col.live.me}' if col.live.me else ''}, " + (
                  f"winding down: no new copies, the {len(col.live.pos)} open ones sold as their wallets sell"
                  if live.mode == "exit" else f"copying {who} with {live.stake_sol:g} SOL" + (
                      f", at most {live.max_open} open, new copies stop after {live.day_loss_sol:g} SOL lost in a day"
                      if live.mode == "live" else ", simulated, nothing sent")), flush=True)
    except Exception as e:  # noqa: BLE001 - a bad setting must not stop the collector; the key never reaches the message
        log.error("live copies off: %s", e)
    print(f"pump collector: {col.stats['ws']}{' (fallback feed set)' if fallback_url else ''}, keeping {retention_days:g} days, "
          f"db {db_path}, ranking wallets every {report_every_s / 60:.0f} min, top {sniper_top} snipers every "
          f"{sniper_every_s / 60:.0f} min. Ctrl+C to stop.", flush=True)
    halt = col.halt                                            # set on the way out: this thread and a gap check still reading stop

    def maintenance() -> None:                                 # own thread and connection: never stalls the feed
        from .pumpmature import mature_report, settle_matures   # here: those modules build on this one
        from .pumpspecialists import follow_specialists, log_lines, specialists
        try:                                                   # once per start: the followed wallets up close, in the log
            from .pumpdossier import dossiers
            for line in dossiers(db_path) + stake_sweep(db_path):
                log.info("%s", line)
        except Exception:  # noqa: BLE001
            log.exception("dossiers failed")
        last_report, last_top, pruned = time.time(), [], 0
        while not halt.wait(sniper_every_s):
            try:                                               # small steps, folded each time: bulk deletes swell the log
                low = (col.stats.get("disk_free_gb") or LOW_DISK_GB) < LOW_DISK_GB
                pruned += prune(db_path, min(col.retention_s, LOW_DISK_KEEP_S) if low else col.retention_s)
                checkpoint(db_path)
            except Exception:  # noqa: BLE001
                log.exception("prune failed")
            try:                                               # coins 30 h old, before their trades are pruned
                stored = settle_matures(db_path)
                if stored:
                    log.info("mature coins: %d entries stored", stored)
            except Exception:  # noqa: BLE001 - its own failure must not stop the launches and the ranking
                log.exception("mature coins failed")
            try:
                settled = settle_launches(db_path)              # freeze closed launches before their trades are pruned
                if settled:
                    log.info("settled %d launches", settled)
                top = update_snipers(db_path, sniper_top)       # cheap: only the first slots of the window's tokens
                if [a for a, _ in top] != last_top:              # full addresses: the page is behind a password, the log is not
                    log.info("top %d snipers: %s", len(top), ", ".join(f"{a} ({n})" for a, n in top))
                    last_top = [a for a, _ in top]
                if time.time() - last_report >= report_every_s:
                    n, pruned = pruned, 0
                    rep = build_report(db_path)
                    save_report(db_path, rep)
                    g = update_follow(db_path, rep)
                    strat = strategy_report(db_path)
                    try:
                        strat["mature"] = mat = mature_report(db_path)
                    except Exception:  # noqa: BLE001
                        log.exception("mature report failed")
                        mat = {}
                    try:                                    # the wallets that buy those coins, copied, and the best followed
                        spec = specialists(db_path)
                        strat.setdefault("mature", {})["specialists"] = spec
                        new = follow_specialists(db_path, spec)
                        if new:
                            log.info("paper-following %d more wallets that buy past $100k, from now on: %s", len(new), ", ".join(new))
                    except Exception:  # noqa: BLE001
                        log.exception("specialists failed")
                        spec = None
                    save_meta(db_path, "strategies", strat)
                    last_report = time.time()
                    gold = [r["addr"] for r in rep.get("traders", []) if r.get("golden")]
                    co, cnt = strat.get("cohorts") or {}, (strat.get("counts") or {}).get("cohorts") or {}
                    log.info("report: %s wallets ranked, %d golden (followed from now on)%s, %s tokens pruned | strategies on "
                             "%s launches, best %s | crew %s launches, best %s; 2nd coin on %s, best %s; first coin %s, best %s",
                             rep.get("counts", {}).get("wallets_ranked"), g, f": {', '.join(gold)}" if gold else "", n,
                             strat.get("counts", {}).get("launches"), _best(strat.get("rules")),
                             cnt.get("crew", 0), _best(co.get("crew")), cnt.get("crew_2nd", 0), _best(co.get("crew_2nd")),
                             cnt.get("first_coin", 0), _best(co.get("first_coin")))
                    for name, rules in co.items():                  # the whole Strategy tab, one line per cohort
                        log.info("strategies %s (%s launches): %s", name, cnt.get(name, 0), _rules_line(rules))
                    for trig, groups in (mat.get("triggers") or {}).items():
                        for name, rules in groups.items():
                            log.info("mature %s %s (%s coins, %s SOL each): %s", trig, name, mat["counts"][trig][name],
                                     mat["params"]["stake_sol"], _rules_line(rules, detail=True))
                    for line in log_lines(spec) if spec else ():
                        log.info("%s", line)
                    for line in paper_lines(db_path) + live_lines(db_path) + go_lines(db_path) + stake_sweep(db_path):   # forward test, live copies, the go-live rule, sizes
                        log.info("%s", line)
                    try:                                             # the same copies, sold by rules of our own
                        from .pumpexits import exit_sweep            # here: that module builds on this one
                        for line in exit_sweep(db_path):
                            log.info("%s", line)
                    except Exception:  # noqa: BLE001
                        log.exception("exit sweep failed")
                    checkpoint(db_path)                              # the long reads are over: let the log reset
            except Exception:  # noqa: BLE001
                log.exception("maintenance failed")

    threading.Thread(target=maintenance, daemon=True).start()
    try:
        asyncio.run(col.run())                                 # returns on SIGTERM (a redeploy) with its sockets closed: exit 0
    except KeyboardInterrupt:                                  # Ctrl+C on Windows, where the loop takes no signal handlers
        col.flush()
    finally:
        halt.set()
        col.c.close()
    return 0


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
WALLETS_SQL = """
WITH pos AS (
  SELECT wallet, mint,
         MIN(CASE WHEN buy = 1 THEN slot END) AS fb,
         SUM(CASE WHEN buy = 1 THEN sol + fee ELSE 0 END) AS cost,
         SUM(CASE WHEN buy = 0 THEN sol - fee ELSE 0 END) AS proceeds,
         SUM(CASE WHEN buy = 1 THEN tok ELSE -tok END) AS left_tok,
         MIN(ts) AS t0, MAX(ts) AS t1, SUM(buy = 0) AS n_sells
  FROM trades GROUP BY wallet, mint),
last AS (SELECT mint, vsol, vtok, MAX(rowid) FROM trades GROUP BY mint),
val AS (
  SELECT pos.*, m.slot AS mslot, m.ts AS mts,
         pos.proceeds - pos.cost + CASE WHEN pos.left_tok > 0 AND last.vtok > 0
             THEN (last.vsol - last.vsol * 1.0 * last.vtok / (last.vtok + pos.left_tok)) * (1 - :fee) ELSE 0 END AS pnl
  FROM pos JOIN mints m ON m.id = pos.mint JOIN last ON last.mint = pos.mint
  WHERE pos.fb IS NOT NULL AND pos.wallet != m.creator),
ranked AS (SELECT val.*, ROW_NUMBER() OVER (PARTITION BY wallet ORDER BY pnl DESC) AS rn FROM val)
SELECT r.wallet, w.addr, COUNT(*) AS n, SUM(r.cost) AS cost, SUM(r.pnl) AS pnl, SUM(r.pnl > 0) AS wins,
       SUM(r.fb - r.mslot <= :snipe) AS snipes, SUM(r.fb = r.mslot) AS block0,
       SUM(CASE WHEN r.fb - r.mslot <= :snipe THEN r.pnl ELSE 0 END) AS snipe_pnl,
       SUM(CASE WHEN r.fb - r.mslot <= :snipe AND r.pnl > 0 THEN 1 ELSE 0 END) AS snipe_wins,
       SUM(CASE WHEN r.mts < :mid THEN r.pnl ELSE 0 END) AS pnl_h1, SUM(CASE WHEN r.mts >= :mid THEN r.pnl ELSE 0 END) AS pnl_h2,
       SUM(r.mts < :mid) AS n_h1,
       SUM(CASE WHEN r.rn <= 2 AND r.pnl > 0 THEN r.pnl ELSE 0 END) AS top2,
       SUM(CASE WHEN r.pnl > 0 THEN r.pnl ELSE 0 END) AS gross_win,
       AVG(CASE WHEN r.n_sells > 0 THEN r.t1 - r.t0 END) AS avg_hold_s
FROM ranked r JOIN wallets w ON w.id = r.wallet
GROUP BY r.wallet HAVING n >= :min_n OR snipes >= :min_snipes
"""


def _state_before(c: sqlite3.Connection, mint: int, slot: int | None) -> tuple[int, int] | None:
    """Curve reserves left by every trade before `slot` (None: after the last trade seen)."""
    if slot is None:
        return c.execute("SELECT vsol, vtok FROM trades WHERE mint=? ORDER BY slot DESC, rowid DESC LIMIT 1", (mint,)).fetchone()
    return c.execute("SELECT vsol, vtok FROM trades WHERE mint=? AND slot < ? ORDER BY slot DESC, rowid DESC LIMIT 1", (mint, slot)).fetchone()


def copy_trade(entry: tuple[int, int], exit_: tuple[int, int], stake_sol: float, tx_cost_sol: float,
               fee_in: float = FEE, fee_out: float = FEE) -> float:
    """SOL PnL of buying `stake_sol` at reserves `entry` and selling everything at reserves `exit_`
    (bonding curve or PumpSwap pool: both are constant product on the stored reserves). `tx_cost_sol` is
    everything one transaction costs outside pump.fun's own fee - signature, priority fee, tip - and is
    paid twice, once to get in and once to get out."""
    vsol, vtok = entry
    net = stake_sol * LAMPORTS / (1 + fee_in)
    tokens = vtok - vsol * vtok / (vsol + net)
    vsol2, vtok2 = exit_
    out = (vsol2 - vsol2 * vtok2 / (vtok2 + tokens)) * (1 - fee_out) if vtok2 > 0 else 0.0
    return out / LAMPORTS - stake_sol - 2 * tx_cost_sol


def _fee_known(c: sqlite3.Connection, mint: int, slot: int | None) -> float:
    """The fee a trade in this coin paid at `slot` (None: now), from the last sell that paid one: PumpSwap buy events
    log almost none, and the rate moves with the coin's market cap."""
    row = c.execute("""SELECT fee * 1.0 / sol FROM trades WHERE mint = ? AND slot <= ? AND buy = 0 AND sol >= ? AND fee > 0
                       ORDER BY slot DESC LIMIT 1""", (mint, 2**62 if slot is None else slot, 10**7)).fetchone()
    return row[0] if row else FEE


def _fee_rate(c: sqlite3.Connection, wallet: int, mint: int, buy: int, slot: int | None) -> float:
    """The wallet's own fee rate on that trade: 1.25 % on the curve, less on PumpSwap."""
    if slot is None:
        return FEE
    row = c.execute("SELECT fee * 1.0 / sol FROM trades WHERE wallet=? AND mint=? AND buy=? AND slot=? AND sol > 0 LIMIT 1",
                    (wallet, mint, buy, slot)).fetchone()
    return row[0] if row and row[0] is not None else FEE


def _mint_fee_rate(c: sqlite3.Connection, mint: int, buy: int) -> float:
    """The fee rate this token charges: 1.25 % on the curve, less on PumpSwap, and creators can set their own. From a trade
    that paid one: pump.fun's mayhem agent trades fee-free, and on most mayhem coins its sell is the first."""
    row = c.execute("SELECT fee * 1.0 / sol FROM trades WHERE mint=? AND buy=? AND sol > 0 AND fee > 0 LIMIT 1", (mint, buy)).fetchone()
    return row[0] if row and row[0] is not None else FEE


def _value(state: tuple[int, int], tok: float, fee_out: float) -> float:
    """SOL a sale of `tok` tokens returns at these reserves, after the token's fee."""
    vsol, vtok = state
    return (vsol - vsol * vtok / (vtok + tok)) * (1 - fee_out) / LAMPORTS if vtok > 0 and tok > 0 else 0.0


def strategy_states(c: sqlite3.Connection, mint: int, cslot: int, cts: int, creator: int, latency_slots: int,
                    stake_sol: float, hold_s: float, targets: tuple[float, ...] = (0.2, 0.5, 1.0)) -> dict[str, Any] | None:
    """Where each exit rule lands on one launch, as curve states, plus what the launch did:

    copy       out when the maker first sells, the plain copy
    be         the stake is worth taking back: break-even sells here, the rest rides on with the maker
    t20/50/100 the position is worth that much more than the stake, gross
    end        the last price of the window: where a rule with no exit ends up

    Entry is `latency_slots` after the creation slot and every exit lands the same `latency_slots` after the trade
    that triggers it, because a rule can only react to a price it has already seen. States, not profits: the fee,
    priority fee and tip are charged later by `launch_pnl`, so those assumptions stay changeable."""
    entry = _state_before(c, mint, cslot + latency_slots)
    if not entry or entry[0] <= 0 or entry[1] <= 0:
        return None                                         # nothing traded before we could get in
    fee_in, fee_out = _mint_fee_rate(c, mint, 1), _mint_fee_rate(c, mint, 0)
    tokens = entry[1] - entry[0] * entry[1] / (entry[0] + stake_sol * LAMPORTS / (1 + fee_in))
    if tokens <= 0:
        return None
    # ponytail: `bought` sums the stored trades, so one the feed missed shifts it; store TradeEvent.real_sol_reserves if that bites
    path = c.execute("""SELECT slot, vsol, vtok, wallet, buy, ts, bought FROM (
                            SELECT slot, vsol, vtok, wallet, buy, ts, rowid AS r,
                                   SUM(CASE WHEN buy THEN sol ELSE -sol END) OVER (ORDER BY slot, rowid) AS bought
                            FROM trades WHERE mint = ? AND slot <= ?)
                        WHERE slot >= ? ORDER BY slot, r""",
                     (mint, cslot + max(1, int(hold_s / 0.4)), cslot + latency_slots)).fetchall()

    def land(i: int) -> tuple[int, int]:
        """The reserves our order reaches: the last state before it lands, `latency_slots` after path[i]."""
        j, s = i, path[i][0] + latency_slots
        while j + 1 < len(path) and path[j + 1][0] < s:
            j += 1
        return path[j][1], path[j][2]

    worth = [_value((r[1], r[2]), tokens, fee_out) for r in path]
    maker = next((i for i, r in enumerate(path) if r[3] == creator and not r[4]), None)
    out: dict[str, Any] = {"entry": entry, "end": (path[-1][1], path[-1][2]) if path else entry,
                           "maker": land(maker) if maker is not None else ((path[-1][1], path[-1][2]) if path else entry),
                           "peak": max(worth) / stake_sol if worth else 0.0,
                           "dev_sold_s": (path[maker][5] - cts) if maker is not None else None,
                           "n_trades": len(path), "n_buyers": len({r[3] for r in path if r[4]}),
                           "fee_in": fee_in, "fee_out": fee_out}
    for tp in targets:
        i = next((i for i, v in enumerate(worth) if v >= (1 + tp) * stake_sol), None)
        out[f"t{round(tp * 100)}"] = land(i) if i is not None else None
    be = next((i for i, v in enumerate(worth) if v >= stake_sol), None)
    out["be"] = land(be) if be is not None and (maker is None or maker > be) else None
    for secs in (30, 60):                                    # out on the clock, before the maker usually dumps
        i = next((i for i in range(len(path) - 1, -1, -1) if path[i][5] <= cts + secs), None)
        out[f"s{secs}"] = land(i) if i is not None else None
    out.update(_late_states(path, land, cts, out["s60"], latency_slots, stake_sol, fee_in, fee_out))
    return out


def _late_states(path: list, land, cts: int, entry60: tuple[int, int] | None, latency_slots: int, stake_sol: float,
                 fee_in: float, fee_out: float, hold_s: float = 120.0, curve_sol: float = 8.0) -> dict[str, Any]:
    """A later entry, the rhythm of the steadiest wallet we follow (7VsGe3…): in a minute after launch, or once
    `curve_sol` SOL has been bought into the curve, and out `hold_s` later.

    x180     in at 60 s (the s60 state), out 2 minutes after that
    e20/e50  that position worth +20 % / +50 % before the 2 minutes are up
    c8, c8x  in once 8 SOL is in the curve, out 2 minutes after that: SOL bought net of sells since creation (`path`'s
             last column), not virtual SOL over the 30 a curve starts at, which a mayhem coin's agent moves without buying"""
    last_by = lambda t: next((i for i in range(len(path) - 1, -1, -1) if path[i][5] <= t), None)   # noqa: E731
    out: dict[str, Any] = {k: None for k in LATE_STATES}
    i60 = last_by(cts + 60)
    if entry60 and i60 is not None and entry60[0] > 0 and entry60[1] > 0:
        tokens = entry60[1] - entry60[0] * entry60[1] / (entry60[0] + stake_sol * LAMPORTS / (1 + fee_in))
        k = last_by(cts + 60 + hold_s)
        out["x180"] = land(k)
        landed = path[i60][0] + latency_slots                # our buy lands here: only what trades after it is ours
        for tp, key in ((0.2, "e20"), (0.5, "e50")):
            j = next((j for j in range(i60 + 1, k + 1) if path[j][0] >= landed
                      and _value((path[j][1], path[j][2]), tokens, fee_out) >= (1 + tp) * stake_sol), None)
            out[key] = land(j) if j is not None else None
    c = next((i for i, r in enumerate(path) if r[6] >= curve_sol * LAMPORTS), None)
    if c is not None:
        out["c8"], out["c8x"] = land(c), land(last_by(path[c][5] + hold_s))
    return out


def launch_pnl(st: dict[str, Any], stake_sol: float = 0.1, tx_cost_sol: float = TX_COST_SOL) -> dict[str, float]:
    """What each rule made on one settled launch, at today's costs. The thresholds were measured on the gross
    value, so the fee, priority fee and tip come out of the result rather than out of the trigger.
    ponytail: the break-even sale is sized at the price it lands on, not the price that triggered it."""
    entry, fee_out = st["entry"], st["fee_out"]
    tokens = entry[1] - entry[0] * entry[1] / (entry[0] + stake_sol * LAMPORTS / (1 + st["fee_in"]))
    if tokens <= 0:
        return {}
    net = lambda state, n=2: _value(state, tokens, fee_out) - stake_sol - n * tx_cost_sol   # noqa: E731
    out = {"copy": net(st["maker"]), "hold": net(st["end"])}
    for name in ("t20", "t50", "t100"):
        out["tp" + name[1:]] = net(st[name] or st["maker"])   # never got there: it leaves with the maker
    for secs in (30, 60):                                     # the clock exits: out before the maker's usual dump
        out[f"sell{secs}s"] = net(st.get(f"s{secs}") or st["entry"])
    if not st["be"]:
        out["breakeven"] = out["copy"]                        # the maker left first, or it was never worth the stake
    else:
        state, back = st["be"], stake_sol + 3 * tx_cost_sol   # the stake back, and the three transactions this rule pays
        t = back * LAMPORTS / (1 - fee_out)
        sold = min(tokens, state[1] * (state[0] / (state[0] - t) - 1)) if state[0] > t else tokens
        out["breakeven"] = _value(state, sold, fee_out) + _value(st["maker"], tokens - sold, fee_out) - stake_sol - 3 * tx_cost_sol
    return {**out, **_late_pnl(st, stake_sol, tx_cost_sol)}


def _late_pnl(st: dict[str, Any], stake_sol: float, tx_cost_sol: float) -> dict[str, float]:
    """The later entries (see _late_states), each its own stake bought at its own, later price."""
    def bought_at(e: tuple[int, int] | None):
        if not e or e[0] <= 0 or e[1] <= 0:
            return None
        tok = e[1] - e[0] * e[1] / (e[0] + stake_sol * LAMPORTS / (1 + st["fee_in"]))
        return (lambda state: _value(state, tok, st["fee_out"]) - stake_sol - 2 * tx_cost_sol) if tok > 0 else None

    out: dict[str, float] = {}
    late = bought_at(st.get("s60")) if st.get("x180") else None
    if late:
        out["late60_2m"] = late(st["x180"])
        out["late60_tp20"] = late(st["e20"] or st["x180"])      # +20 % if it comes within the 2 minutes, else out on time
        out["late60_tp50"] = late(st["e50"] or st["x180"])
        if st.get("dev_sold_s") is None or st["dev_sold_s"] > 60:
            out["late60_2m_held"] = out["late60_2m"]            # only coins whose maker had not sold yet: knowable at 60 s
    sol8 = bought_at(st.get("c8")) if st.get("c8x") else None
    if sol8:
        out["sol8_2m"] = sol8(st["c8x"])
    return out


def strategy_sim(c: sqlite3.Connection, mint: int, cslot: int, creator: int, latency_slots: int, stake_sol: float,
                 tx_cost_sol: float, hold_s: float, targets: tuple[float, ...] = (0.2, 0.5, 1.0)) -> dict[str, float] | None:
    """Every rule on one launch, straight from the trades: the states, priced at today's costs."""
    st = strategy_states(c, mint, cslot, 0, creator, latency_slots, stake_sol, hold_s, targets)
    return launch_pnl(st, stake_sol, tx_cost_sol) if st else None


LAUNCH_COLS = ("mint", "creator", "slot", "ts", "symbol", "graduated", "dev_sold_s", "peak", "n_trades", "n_buyers",
               "entry_vsol", "entry_vtok", "maker_vsol", "maker_vtok", "end_vsol", "end_vtok", "be_vsol", "be_vtok",
               "t20_vsol", "t20_vtok", "t50_vsol", "t50_vtok", "t100_vsol", "t100_vtok", "fee_in", "fee_out", "stake",
               "latency", "buyers", "dev_buy", "s30_vsol", "s30_vtok", "s60_vsol", "s60_vtok",
               *(f"{k}_{v}" for k in LATE_STATES for v in ("vsol", "vtok")), "late")


def _snipers_of(c: sqlite3.Connection, mint: int, cslot: int, creator: int, slots: int = 2, keep: int = 12) -> tuple[str, float]:
    """Who bought this launch in its first slots, and what the maker put into its own bag. The buyers are stored as
    our own wallet ids (that table is never pruned) because an operator's other wallets snipe its launches: that is
    what links its next wallet to this one."""
    buyers, dev = [], 0.0
    for wallet, buy, sol in c.execute("SELECT wallet, buy, sol FROM trades WHERE mint = ? AND slot <= ? ORDER BY slot, rowid LIMIT 60",
                                      (mint, cslot + slots)):
        if wallet == creator:
            dev += sol / LAMPORTS if buy else 0.0
        elif buy and wallet not in buyers:
            buyers.append(wallet)
    return ",".join(str(w) for w in buyers[:keep]), dev


def row_states(r: dict[str, Any]) -> dict[str, Any]:
    """A stored launch back into the states `launch_pnl` prices."""
    at = lambda k: (r[k + "_vsol"], r[k + "_vtok"]) if r.get(k + "_vsol") is not None else None   # noqa: E731
    return {"entry": at("entry"), "maker": at("maker"), "end": at("end"), "be": at("be"), "t20": at("t20"),
            "t50": at("t50"), "t100": at("t100"), "s30": at("s30"), "s60": at("s60"), **{k: at(k) for k in LATE_STATES},
            "dev_sold_s": r.get("dev_sold_s"), "fee_in": r["fee_in"], "fee_out": r["fee_out"]}


def settle_launches(db_path: str | Path, latency_slots: int | None = None, stake_sol: float = 0.1, hold_s: float = 900,
                    limit: int = 20_000) -> int:
    """Freeze every launch whose window has closed into `launches`, before its trades are pruned. This is what
    turns two days of trades into a maker history that keeps growing."""
    c = connect(db_path)
    try:
        measured = (get_meta(c, "stats", {}) or {}).get("lag_p50")
        if latency_slots is None:
            latency_slots = max(2, measured + 1) if measured is not None else 2
        c.execute("UPDATE launches SET graduated = 1 WHERE graduated = 0 AND mint IN (SELECT addr FROM mints WHERE pool IS NOT NULL)")
        todo = c.execute("""SELECT m.id, m.addr, m.slot, m.ts, m.symbol, w.addr, m.creator, m.pool IS NOT NULL
                            FROM mints m JOIN wallets w ON w.id = m.creator
                            WHERE m.ts <= ? AND NOT EXISTS (SELECT 1 FROM launches l WHERE l.mint = m.addr)
                            ORDER BY m.ts LIMIT ?""", (time.time() - hold_s - 120, limit)).fetchall()
        rows = []
        for mid, addr, slot, ts, symbol, maker, creator, graduated in todo:
            st = strategy_states(c, mid, slot, ts, creator, latency_slots, stake_sol, hold_s)
            flat: list[Any] = [addr, maker, slot, ts, symbol, int(graduated)]
            flat += [st["dev_sold_s"], st["peak"], st["n_trades"], st["n_buyers"]] if st else [None, None, None, None]
            for k in ("entry", "maker", "end", "be", "t20", "t50", "t100"):
                flat += list(st[k]) if st and st[k] else [None, None]
            flat += [st["fee_in"], st["fee_out"], stake_sol, latency_slots] if st else [None, None, stake_sol, latency_slots]
            flat += list(_snipers_of(c, mid, slot, creator))
            for k in ("s30", "s60", *LATE_STATES):
                flat += list(st[k]) if st and st[k] else [None, None]
            rows.append((*flat, LATE_DONE if st else None))
        if rows:
            c.executemany(f"INSERT OR IGNORE INTO launches ({','.join(LAUNCH_COLS)}) VALUES ({','.join('?' * len(LAUNCH_COLS))})", rows)
        # launches settled before the snipers were recorded, while their trades are still here
        late = c.execute("""SELECT l.mint, m.id, m.slot, m.creator FROM launches l JOIN mints m ON m.addr = l.mint
                            WHERE l.buyers IS NULL LIMIT ?""", (limit,)).fetchall()
        if late:
            c.executemany("UPDATE launches SET buyers = ?, dev_buy = ? WHERE mint = ?",
                          [(*_snipers_of(c, mid, slot, creator), addr) for addr, mid, slot, creator in late])
        c.commit()
        _backfill_late(c, latency_slots, stake_sol, hold_s, limit)
        return len(rows)
    finally:
        c.close()


LATE_DONE = 2                  # `launches.late`: 2 once the clock states (s30, s60) and the later entries are all stored


def _backfill_late(c: sqlite3.Connection, latency_slots: int, stake_sol: float, hold_s: float, limit: int) -> int:
    """The clock states and the later entries for launches settled before they existed, while their trades are
    still here: the 60 s state is also the late60 rules' entry, and launches settled before 276e76c never had it.
    Computed before anything is written, so the collector's own writes never wait on it."""
    keys = ("s30", "s60", *LATE_STATES)
    todo = c.execute("""SELECT l.mint, m.id, m.slot, m.ts, m.creator, l.latency, l.stake FROM launches l JOIN mints m ON m.addr = l.mint
                        WHERE COALESCE(l.late, 0) < ? AND l.entry_vsol IS NOT NULL LIMIT ?""", (LATE_DONE, limit)).fetchall()
    rows = []
    for addr, mid, slot, ts, creator, lat, stake in todo:
        st = strategy_states(c, mid, slot, ts, creator, lat or latency_slots, stake or stake_sol, hold_s)
        rows.append((*(v for k in keys for v in (st[k] if st and st[k] else (None, None))), addr))
    if rows:
        c.executemany(f"UPDATE launches SET {', '.join(f'{k}_vsol = ?, {k}_vtok = ?' for k in keys)}, late = {LATE_DONE} WHERE mint = ?", rows)
        c.commit()
    return len(rows)


def read_launches(c: sqlite3.Connection, days: float = 30, limit: int = 200_000) -> list[dict[str, Any]]:
    """Settled launches of the last `days`, newest first: the permanent history, not the trade window."""
    cur = c.execute(f"SELECT {', '.join(LAUNCH_COLS)} FROM launches WHERE ts >= ? ORDER BY ts DESC LIMIT ?",
                    (time.time() - days * 86_400, limit))
    return [dict(zip(LAUNCH_COLS, r)) for r in cur.fetchall()]


def operator_groups(rows: list[dict[str, Any]], min_shared: int = 3, max_reach: int = 20) -> dict[str, str]:
    """Maker wallets that look like one hand. A maker that rotates to a fresh wallet every few launches leaves one
    thing behind: its own other wallets snipe its launches, so the same early buyers turn up again. A buyer that
    shows up for many different makers is a sniper bot doing its rounds, so it links nothing.
    Returns wallet -> the group's name (the wallet in it with the most launches). Probable, never proven."""
    launched, seen = collections.Counter(), {}
    for r in rows:
        launched[r["creator"]] += 1
        if r.get("buyers"):
            seen.setdefault(r["creator"], set()).update(r["buyers"].split(","))
    reach: dict[str, set[str]] = {}                          # buyer -> the makers it sniped
    for maker, buyers in seen.items():
        for b in buyers:
            reach.setdefault(b, set()).add(maker)
    shared: collections.Counter = collections.Counter()
    for makers in reach.values():
        if 2 <= len(makers) <= max_reach:
            shared.update(itertools.combinations(sorted(makers), 2))
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for (a, b), n in shared.items():
        if n >= min_shared:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra
    groups: dict[str, list[str]] = {}
    for maker in seen:
        groups.setdefault(find(maker), []).append(maker)
    out = {}
    for members in groups.values():
        head = max(members, key=lambda w: (launched[w], w))   # the busiest wallet names the group
        out.update({w: head for w in members})
    return out


def _agg_launches(rows: list[dict[str, Any]], key_of, stake_sol: float, tx_cost_sol: float, mid: float,
                  min_launches: int) -> list[dict[str, Any]]:
    """The same summary per maker wallet or per operator, depending on the key."""
    by: dict[str, dict[str, Any]] = {}
    for r in rows:
        k = key_of(r)
        m = by.setdefault(k, {"addr": k, "wallets": set(), "launches": 0, "graduated": 0, "replayed": 0, "dumped": 0,
                              "pnl": 0.0, "wins": 0, "dumps": [], "peaks": [], "n_h1": 0, "pnl_h1": 0.0, "n_h2": 0,
                              "pnl_h2": 0.0, "first": r["ts"], "last": r["ts"]})
        m["wallets"].add(r["creator"])
        m["launches"] += 1
        m["graduated"] += bool(r["graduated"])
        m["first"], m["last"] = min(m["first"], r["ts"]), max(m["last"], r["ts"])
        if r["dev_sold_s"] is not None:
            m["dumped"] += 1
            m["dumps"].append(r["dev_sold_s"] / 60)
        if r["entry_vsol"] is None:
            continue                                         # nobody traded it before a copy could land
        p = launch_pnl(row_states(r), stake_sol, tx_cost_sol)["copy"]
        half = "h1" if r["ts"] < mid else "h2"
        m["replayed"] += 1
        m["pnl"] += p
        m["wins"] += p > 0
        m["peaks"].append(r["peak"] or 0.0)
        m["n_" + half] += 1
        m["pnl_" + half] += p
    out = []
    for m in by.values():
        if m["launches"] < min_launches or not m["replayed"]:
            continue
        m["dumps"].sort()
        m["peaks"].sort()
        med = lambda xs: xs[len(xs) // 2] if xs else None     # noqa: E731
        roi = lambda k: (m["pnl" + k] / (m["n" + k] * stake_sol) if m["n" + k] else None)   # noqa: E731
        out.append({"addr": m["addr"], "n_wallets": len(m["wallets"]), "launches": m["launches"], "graduated": m["graduated"],
                    "replayed": m["replayed"], "grad_share": m["graduated"] / m["launches"], "pnl_sol": m["pnl"],
                    "roi": m["pnl"] / (m["replayed"] * stake_sol), "roi_h1": roi("_h1"), "roi_h2": roi("_h2"),
                    "win_rate": m["wins"] / m["replayed"], "dump_share": m["dumped"] / m["launches"],
                    "dump_min": med(m["dumps"]), "peak_med": med(m["peaks"]), "first": m["first"], "last": m["last"]})
    return sorted(out, key=lambda r: r["roi"] if r["roi"] is not None else -9, reverse=True)


def maker_table(c: sqlite3.Connection, stake_sol: float = 0.1, tx_cost_sol: float = TX_COST_SOL, min_launches: int = 3,
                days: float = 30, top: int = 200) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Coin makers on their stored launches - how often they launch, how often and how fast they dump their own bag,
    and what buying every launch (out when they sell) would have paid - per wallet and per operator."""
    rows = read_launches(c, days)
    if not rows:
        return [], []
    mid = (min(r["ts"] for r in rows) + max(r["ts"] for r in rows)) / 2
    groups = operator_groups(rows)
    makers = _agg_launches(rows, lambda r: r["creator"], stake_sol, tx_cost_sol, mid, min_launches)
    for m in makers:
        m["operator"] = groups.get(m["addr"], m["addr"])
    ops = [o for o in _agg_launches(rows, lambda r: groups.get(r["creator"], r["creator"]), stake_sol, tx_cost_sol,
                                    mid, min_launches) if o["n_wallets"] > 1]
    return makers[:top], ops[:top]


def _rules_on(rows: list[dict[str, Any]], stake_sol: float, tx_cost_sol: float, mid: float,
              pnl_of=None) -> list[dict[str, Any]]:
    """Each exit rule over one set of launches, or of any rows `pnl_of` prices (pumpmature's entries)."""
    pnl_of = pnl_of or (lambda r: launch_pnl(row_states(r), stake_sol, tx_cost_sol))
    acc: dict[str, dict[str, Any]] = {}
    for r in rows:
        half = "h1" if r["ts"] < mid else "h2"
        for name, pnl in pnl_of(r).items():
            a = acc.setdefault(name, {"n": 0, "pnl": 0.0, "wins": 0, "n_h1": 0, "pnl_h1": 0.0, "n_h2": 0, "pnl_h2": 0.0, "each": []})
            a["n"] += 1
            a["pnl"] += pnl
            a["wins"] += pnl > 0
            a["n_" + half] += 1
            a["pnl_" + half] += pnl
            a["each"].append(pnl)
    out = []
    for name, a in acc.items():
        roi = {k: (a["pnl" + k] / (a["n" + k] * stake_sol) if a["n" + k] else None) for k in ("", "_h1", "_h2")}
        xs = sorted(a["each"])                              # is the average a few jackpots, or the typical trade?
        out.append({"rule": name, "n": a["n"], "pnl_sol": a["pnl"], "roi": roi[""], "roi_h1": roi["_h1"],
                    "roi_h2": roi["_h2"], "win_rate": a["wins"] / a["n"] if a["n"] else None,
                    "median": xs[len(xs) // 2] / stake_sol, "roi_ex2": sum(xs[:-2]) / ((len(xs) - 2) * stake_sol) if len(xs) > 2 else None})
    return sorted(out, key=lambda r: r["roi"] if r["roi"] is not None else -9, reverse=True)


def cohorts_of(rows: list[dict[str, Any]], seen_min: int = 4, max_makers: int = 3, min_hits: int = 2) -> dict[str, list]:
    """Label every launch with what was known just before it happened, walking forward in time so nothing leaks
    back from the future: was it sniped by a crew we already recognised, and had that wallet launched before?

    crew        a crew we had already seen on other launches sniped this one
    crew_2nd    and the wallet had launched before, so the crew's move to it was already confirmed
    first_coin  the crew is there but this is the wallet's first coin: the one that confirms the move

    A crew wallet comes back: it has sniped `seen_min`+ launches across at most `max_makers` maker wallets. A bot
    doing its rounds snipes each maker once, so its launches and its makers are the same number and it never counts,
    however long it runs."""
    seen: collections.Counter = collections.Counter()
    makers: dict[str, set[str]] = {}
    launched: collections.Counter = collections.Counter()
    out: dict[str, list] = {"all": [], "crew": [], "crew_2nd": [], "first_coin": []}
    for r in sorted(rows, key=lambda r: r["ts"]):
        buyers = (r["buyers"] or "").split(",") if r["buyers"] else []
        hits = sum(1 for b in buyers if seen[b] >= seen_min and len(makers.get(b, ())) <= max_makers)
        out["all"].append(r)
        if hits >= min_hits:
            out["crew"].append(r)
            out["crew_2nd" if launched[r["creator"]] else "first_coin"].append(r)
        for b in set(buyers):
            seen[b] += 1
            makers.setdefault(b, set()).add(r["creator"])
        launched[r["creator"]] += 1
    return out


def strategy_report(db_path: str | Path, stake_sol: float = 0.1, tx_cost_sol: float = TX_COST_SOL, min_launches: int = 3,
                    days: float = 30, hold_s: float = 900) -> dict[str, Any]:
    """Every exit rule on every launch of a repeat maker, one entry each, so the rules differ only in the exit,
    and the same rules again on the launches a known crew sniped. Read from the stored launches, so the sample
    grows for as long as the collector runs."""
    t_build = time.time()
    c = connect(db_path, readonly=True)
    try:
        rows = [r for r in read_launches(c, days) if r["entry_vsol"] is not None]
        made = collections.Counter(r["creator"] for r in rows)
        rows = [r for r in rows if made[r["creator"]] >= min_launches]
        if not rows:
            return {"generated": int(t_build), "empty": True, "params": {"min_launches": min_launches, "days": days}}
        t0, t_last = min(r["ts"] for r in rows), max(r["ts"] for r in rows)
        mid = (t0 + t_last) / 2
        cohorts = cohorts_of(rows)
        groups = {name: _rules_on(sub, stake_sol, tx_cost_sol, mid) for name, sub in cohorts.items() if sub}
        counts = {name: len(sub) for name, sub in cohorts.items()}
        return {"generated": int(time.time()), "build_s": round(time.time() - t_build, 1),
                "params": {"stake_sol": stake_sol, "tx_cost_sol": tx_cost_sol, "min_launches": min_launches, "days": days,
                           "hold_s": hold_s, "latency_slots": rows[0]["latency"]},
                "counts": {"launches": len(rows), "makers": sum(1 for n in made.values() if n >= min_launches),
                           "all_launches": len(made), "cohorts": counts},
                "window": {"start": t0, "end": t_last, "hours": (t_last - t0) / 3600},
                "cohorts": groups, "rules": groups.get("all", [])}
    finally:
        c.close()


def copy_sim(c: sqlite3.Connection, wallet: int, mid: float, latency_slots: int, stake_sol: float, tx_cost_sol: float,
             max_positions: int) -> dict[str, Any]:
    """Follow every buy of `wallet` on tokens it did not launch: enter `latency_slots` after its first buy,
    exit `latency_slots` after its first sell (or at the last price seen if it never sold)."""
    pos = c.execute("""SELECT t.mint, MIN(t.slot), m.ts FROM trades t JOIN mints m ON m.id = t.mint
                       WHERE t.wallet = ? AND t.buy = 1 AND m.creator != t.wallet
                       GROUP BY t.mint ORDER BY 2 DESC LIMIT ?""", (wallet, max_positions)).fetchall()
    out = {"n": 0, "pnl": 0.0, "n_h1": 0, "pnl_h1": 0.0, "n_h2": 0, "pnl_h2": 0.0, "wins": 0}
    for mint, fb, mts in pos:
        entry = _state_before(c, mint, fb + latency_slots)
        if not entry or entry[0] <= 0 or entry[1] <= 0:
            continue
        fs = c.execute("SELECT MIN(slot) FROM trades WHERE wallet=? AND mint=? AND buy=0 AND slot >= ?", (wallet, mint, fb)).fetchone()[0]
        exit_ = _state_before(c, mint, fs + latency_slots) if fs is not None else _state_before(c, mint, None)
        fee_in = _fee_rate(c, wallet, mint, 1, fb) or _fee_known(c, mint, fb + latency_slots)   # a PumpSwap buy logs ~0
        p = copy_trade(entry, exit_, stake_sol, tx_cost_sol, fee_in, _fee_rate(c, wallet, mint, 0, fs))
        half = "h1" if mts < mid else "h2"
        out["n"] += 1
        out["pnl"] += p
        out["wins"] += p > 0
        out["n_" + half] += 1
        out["pnl_" + half] += p
    for k in ("", "_h1", "_h2"):
        n = out["n" + k]
        out["roi" + k] = out["pnl" + k] / (n * stake_sol) if n else None
    return out


SWEEP_STAKES = (0.1, 0.25, 0.5, 1.0)


def stake_sweep(db_path: str | Path, stakes: tuple[float, ...] = SWEEP_STAKES, latency_slots: int | None = None) -> list[str]:
    """Every golden wallet's copy replayed at each stake on the trades still stored. A bigger copy pays the same fixed
    0.0015 SOL a transaction on more money, 3 % of a 0.1 SOL round trip, but moves the curve more going in and out."""
    c = connect(db_path, readonly=True)
    try:
        if latency_slots is None:
            measured = (get_meta(c, "stats", {}) or {}).get("lag_p50")
            latency_slots = max(2, measured + 1) if measured is not None else 2
        t0, t1 = c.execute("SELECT MIN(ts), MAX(ts) FROM mints").fetchone()
        out = []
        for addr, wid in c.execute("""SELECT f.wallet, w.id FROM follow f JOIN wallets w ON w.addr = f.wallet
                                      WHERE f.golden_ever = 1 ORDER BY f.wallet""").fetchall():
            runs = [(s, copy_sim(c, wid, (t0 + t1) / 2, latency_slots, s, TX_COST_SOL, 1000)) for s in stakes]
            if runs[0][1]["n"]:
                out.append(f"stake sweep {addr} ({runs[0][1]['n']} copies on the stored trades): " + ", ".join(
                    f"{s:g} SOL {_pct(r['roi'])} [{_pct(r['roi_h1'], 0)}/{_pct(r['roi_h2'], 0)}] won {r['wins'] / r['n']:.0%}"
                    for s, r in runs))
        return out
    finally:
        c.close()


def twins(c: sqlite3.Connection, wallet: int, max_positions: int = 1000) -> int:
    """Other wallets that bought the same tokens in the same slot as `wallet` on at least half of its tokens
    (and 3+): one operator behind several wallets, or a coordinated group trading its own launches."""
    n = min(c.execute("SELECT COUNT(DISTINCT mint) FROM trades WHERE wallet = ? AND buy = 1", (wallet,)).fetchone()[0], max_positions)
    rows = c.execute("""WITH p AS (SELECT mint, MIN(slot) AS fb FROM trades WHERE wallet = ? AND buy = 1
                                   GROUP BY mint ORDER BY fb DESC LIMIT ?)
                        SELECT COUNT(DISTINCT t.mint) FROM p
                        JOIN trades t ON t.mint = p.mint AND t.slot = p.fb AND t.buy = 1 AND t.wallet != ?
                        GROUP BY t.wallet""", (wallet, max_positions, wallet)).fetchall()
    return sum(1 for (shared,) in rows if shared >= max(3, n / 2))


def build_report(db_path: str | Path, snipe_slots: int = 2, min_tokens: int = 10, min_snipes: int = 5, latency_slots: int | None = None,
                 stake_sol: float = 0.1, tx_cost_sol: float = TX_COST_SOL, top: int = 100, max_positions: int = 1000,
                 min_hours: float = 12, min_launches: int = 3, hold_s: float = 300, maker_days: float = 30) -> dict[str, Any]:
    """`min_hours`: no wallet is called golden before the window spans this long. Golden wallets get followed
    for good, so a verdict from the first minutes of data would pin noise to the paper test.
    `latency_slots` None: the feed delay the collector measured (median) plus one slot to send, at least 2."""
    t_build = time.time()
    c = connect(db_path, readonly=True)
    try:
        measured = (get_meta(c, "stats", {}) or {}).get("lag_p50")
        if latency_slots is None:
            latency_slots = max(2, measured + 1) if measured is not None else 2
        params = {"snipe_slots": snipe_slots, "min_tokens": min_tokens, "min_snipes": min_snipes, "latency_slots": latency_slots,
                  "latency_measured_p50": measured, "stake_sol": stake_sol, "tx_cost_sol": tx_cost_sol, "fee": FEE, "min_hours": min_hours,
                  "tx_cost_parts": {"base_fee": BASE_FEE_SOL, "priority": PRIORITY_SOL, "tip": TIP_SOL},
                  "min_launches": min_launches, "hold_s": hold_s, "maker_days": maker_days}
        t0, t1, n_mints = c.execute("SELECT MIN(ts), MAX(ts), COUNT(*) FROM mints").fetchone()
        if not n_mints:
            return {"generated": int(t_build), "params": params, "empty": True}
        t_last = c.execute("SELECT MAX(ts) FROM trades").fetchone()[0] or t1
        mid = (t0 + t_last) / 2
        cur = c.execute(WALLETS_SQL, {"fee": FEE, "snipe": snipe_slots, "mid": mid, "min_n": min_tokens, "min_snipes": min_snipes})
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        launched = dict(c.execute("SELECT creator, COUNT(*) FROM mints GROUP BY creator"))
        for r in rows:
            r["launched"] = launched.get(r["wallet"], 0)
            r["pnl_sol"] = r["pnl"] / LAMPORTS
            r["cost_sol"] = r["cost"] / LAMPORTS
            r["roi"] = r["pnl"] / r["cost"] if r["cost"] else None
            r["win_rate"] = r["wins"] / r["n"] if r["n"] else None
            r["snipe_share"] = r["snipes"] / r["n"] if r["n"] else None
            r["top2_share"] = r["top2"] / r["gross_win"] if r["gross_win"] else None
            r["avg_hold_min"] = r["avg_hold_s"] / 60 if r["avg_hold_s"] is not None else None
            for k in ("snipe_pnl", "pnl_h1", "pnl_h2"):
                r[k + "_sol"] = r[k] / LAMPORTS
        traders = sorted((r for r in rows if r["n"] >= min_tokens), key=lambda r: r["pnl"], reverse=True)
        cands = [r for r in traders if r["pnl"] > 0][:top]
        snipers = sorted((r for r in rows if r["snipes"] >= min_snipes), key=lambda r: r["snipes"], reverse=True)[:top]
        for r in {id(r): r for r in cands + snipers}.values():
            r["twins"] = twins(c, r["wallet"], max_positions)
        for r in cands:
            sim = copy_sim(c, r["wallet"], mid, latency_slots, stake_sol, tx_cost_sol, max_positions)
            r.update({"copy_n": sim["n"], "copy_roi": sim["roi"], "copy_roi_h1": sim["roi_h1"], "copy_roi_h2": sim["roi_h2"],
                      "copy_win_rate": sim["wins"] / sim["n"] if sim["n"] else None})
            r["golden"] = bool(sim["roi_h1"] is not None and sim["roi_h2"] is not None and sim["roi_h1"] > 0 and sim["roi_h2"] > 0
                               and r["pnl_h1"] > 0 and r["pnl_h2"] > 0 and (r["top2_share"] or 1) <= 0.5
                               and r["launched"] == 0 and r["twins"] == 0 and (t_last - t0) / 3600 >= min_hours)
        # base rates over every wallet with enough tokens: is making money here common, and does it persist?
        both = [r for r in traders if r["n_h1"] >= min_tokens // 2 and r["n"] - r["n_h1"] >= min_tokens // 2]
        h1_win = [r for r in both if r["pnl_h1"] > 0]
        base = {"wallets": len(traders), "profitable_share": (sum(r["pnl"] > 0 for r in traders) / len(traders)) if traders else None,
                "both_halves": len(both), "h2_profitable_share": (sum(r["pnl_h2"] > 0 for r in both) / len(both)) if both else None,
                "h1_winners": len(h1_win), "h1_winners_h2_profitable_share": (sum(r["pnl_h2"] > 0 for r in h1_win) / len(h1_win)) if h1_win else None}
        keep = ("addr", "n", "cost_sol", "pnl_sol", "roi", "win_rate", "snipes", "block0", "snipe_share", "snipe_pnl_sol", "snipe_wins",
                "pnl_h1_sol", "pnl_h2_sol", "top2_share", "avg_hold_min", "launched", "twins", "copy_n", "copy_roi", "copy_roi_h1",
                "copy_roi_h2", "copy_win_rate", "golden")
        slim = lambda rs: [{k: r.get(k) for k in keep} for r in rs]  # noqa: E731
        # follow the coin maker: its next launch is a known event. From the stored launches, so this history keeps
        # growing after the trades behind it are pruned.
        creators, operators = maker_table(c, stake_sol, tx_cost_sol, min_launches, maker_days, top)
        stats = get_meta(c, "stats", {}) or {}
        return {"generated": int(time.time()), "build_s": round(time.time() - t_build, 1), "params": params,
                "window": {"start": t0, "end": t_last, "hours": (t_last - t0) / 3600},
                "counts": {"tokens": n_mints, "graduated": c.execute("SELECT COUNT(*) FROM mints WHERE pool IS NOT NULL").fetchone()[0],
                           "trades": stats.get("trades"), "wallets_ranked": len(rows)},
                "base": base, "traders": slim(sorted(cands, key=lambda r: r.get("copy_roi") or -9, reverse=True)), "snipers": slim(snipers),
                "creators": creators, "operators": operators}
    finally:
        c.close()


def save_meta(db_path: str | Path, key: str, value: Any) -> None:
    c = connect(db_path)
    try:
        set_meta(c, key, value)
        c.commit()
    finally:
        c.close()


def save_report(db_path: str | Path, rep: dict[str, Any]) -> None:
    save_meta(db_path, "report", rep)


def print_report(rep: dict[str, Any]) -> None:
    if rep.get("empty"):
        print("nothing collected yet: run `python -m hl_screener pump collect` first")
        return
    w, n, b = rep["window"], rep["counts"], rep["base"]
    print(f"{n['tokens']:,} tokens over {w['hours']:.1f} h, {n['wallets_ranked']:,} wallets ranked (report took {rep['build_s']}s)")
    if b["profitable_share"] is not None:
        print(f"profitable: {b['profitable_share']:.0%} of {b['wallets']:,} wallets with {rep['params']['min_tokens']}+ tokens")
    print("\ntop copy candidates (copier ROI per copied buy, latency", rep["params"]["latency_slots"], "slots):")
    for r in rep["traders"][:10]:
        roi = f"{r['copy_roi']:+.1%}" if r["copy_roi"] is not None else "n/a"
        print(f"  {r['addr']}  tokens {r['n']:>4}  pnl {r['pnl_sol']:+8.2f} SOL  win {r['win_rate']:.0%}  copier {roi}{'  GOLDEN' if r['golden'] else ''}")
    if rep.get("creators"):
        print(f"\ncoin makers (buy every launch {rep['params']['latency_slots']} slots after creation, sell on its first sell "
              f"or after {rep['params']['hold_s'] / 60:.0f} min):")
        for r in rep["creators"][:10]:
            roi = f"{r['roi']:+.1%}" if r["roi"] is not None else "n/a"
            print(f"  {r['addr']}  launches {r['launches']:>4}  graduated {r['grad_share']:>4.0%}  sells own {r['dump_share']:>4.0%}  buyer {roi}")
    print("\ntop snipers (first buy within", rep["params"]["snipe_slots"], "slots of creation):")
    for r in rep["snipers"][:10]:
        print(f"  {r['addr']}  snipes {r['snipes']:>5}  same-slot {r['block0']:>5}  snipe pnl {r['snipe_pnl_sol']:+8.2f} SOL")
