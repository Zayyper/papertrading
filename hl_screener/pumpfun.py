"""pump.fun wallets on Solana: who snipes new tokens, who trades them profitably, and could a copier keep it.

    python -m hl_screener pump collect    # stream pump.fun from Solana 24/7 into SQLite, re-rank every 30 min
    python -m hl_screener pump report     # rank wallets now from what was collected

Data: the pump.fun program's own CreateEvent / TradeEvent (layout: github.com/pump-fun/pump-public-docs,
idl/pump.json), read from Solana's public RPC with logsSubscribe. No key; SOLANA_WS_URL swaps in another
RPC (Helius, QuickNode...). Scope: tokens created while the collector runs, quoted in SOL, on the bonding
curve. A graduated token trades on PumpSwap afterwards, which is not tracked: positions held through
graduation are valued at the final curve price.

Copy cost is not modelled here, it is replayed: a copier that lands `latency_slots` after the wallet
buys at the curve state left by every trade before that slot, and sells the same way after the wallet's
first sell, paying the pump.fun fees and a fixed per-transaction cost.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import sqlite3
import struct
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUBLIC_WS = "wss://api.mainnet-beta.solana.com"
D_TRADE = hashlib.sha256(b"event:TradeEvent").digest()[:8]
D_CREATE = hashlib.sha256(b"event:CreateEvent").digest()[:8]
SOL_QUOTE = bytes(32)          # quote_mint = default pubkey: the curve is quoted in native SOL
FEE = 0.0125                   # 0.95 % protocol + 0.30 % creator per side, the common case (ponytail: per-token creator fees vary; read them from the events if it matters)
LAMPORTS = 1e9
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


def parse_trade(b: bytes) -> dict[str, Any] | None:
    """TradeEvent, or None when the bytes do not fit the layout exactly: a changed layout must stop the
    data, not silently corrupt it."""
    try:
        r = _Reader(b)
        mint = r.pk()
        sol, tok, is_buy = r.take("<QQ?")
        user = r.pk()
        ts = r.take("<q")
        vsol, vtok = r.take("<QQ")
        r.skip(16 + 32 + 8)                       # real reserves, fee_recipient, fee_basis_points
        fee = r.take("<Q")
        r.skip(32 + 8)                            # creator, creator_fee_basis_points
        fee += r.take("<Q")                       # creator_fee
        r.skip(1 + 8 * 4)                         # track_volume, (un)claimed tokens, current_sol_volume, last_update
        r.string()                                # ix_name
        r.skip(1 + 8 * 4)                         # mayhem_mode, cashback and buyback bps/amounts
        r.skip(34 * r.take("<I"))                 # shareholders: vec<(pubkey, u16)>
        quote = r.pk()
        r.skip(8 * 5)                             # quote amount/reserves, holder rewards
    except (struct.error, ValueError):
        return None
    if r.o != len(b):
        return None
    return {"mint": mint, "user": user, "buy": is_buy, "sol": sol, "tok": tok, "fee": fee, "ts": ts,
            "vsol": vsol, "vtok": vtok, "sol_quote": quote == SOL_QUOTE}


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
        r.skip(8 * 4 + 32 + 2)                    # reserves, supply, token_program, mayhem, cashback
        quote = r.pk()
        r.skip(8 + 8 + 1)                         # virtual_quote_reserves, creator_fee_bps, is_holder_reward
    except (struct.error, ValueError):
        return None
    if r.o != len(b):
        return None
    return {"mint": mint, "user": user, "name": name[:64], "symbol": symbol[:32], "ts": ts, "sol_quote": quote == SOL_QUOTE}


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
CREATE TABLE IF NOT EXISTS follow (wallet TEXT PRIMARY KEY, added_at INTEGER, golden_now INTEGER, report_copy_roi REAL);
CREATE TABLE IF NOT EXISTS pfills (id INTEGER PRIMARY KEY, wallet TEXT, mint TEXT, side TEXT, trigger_slot INTEGER,
                                   land_slot INTEGER, ts INTEGER, sol REAL, tok REAL, leader_px REAL, px REAL,
                                   slip_bps REAL, pnl REAL, timed_out INTEGER);
CREATE TABLE IF NOT EXISTS ppos (wallet TEXT, mint TEXT, tok REAL, cost REAL, opened INTEGER, PRIMARY KEY (wallet, mint));
"""


def connect(path: str | Path, readonly: bool = False) -> sqlite3.Connection:
    p = Path(path)
    if readonly:
        return sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True, timeout=30, check_same_thread=False)
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(p, timeout=30, check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.executescript(SCHEMA)
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
    the copy is sold when the wallet first sells, the same way. Priced on the live curve at the wallet's own
    fee rate plus `tx_cost_sol` per transaction: the report's replay rules, so the two compare directly.
    Nothing is ever sent to Solana."""

    def __init__(self, c: sqlite3.Connection, latency_slots: int = 2, stake_sol: float = 0.1, tx_cost_sol: float = 0.0005):
        self.c, self.L, self.stake, self.tx = c, latency_slots, stake_sol, tx_cost_sol
        self.follow: set[str] = set()
        self.reload()
        self.pos = {(w, m): [tok, cost, opened] for w, m, tok, cost, opened in c.execute("SELECT wallet, mint, tok, cost, opened FROM ppos")}
        self.copied = set(c.execute("SELECT DISTINCT wallet, mint FROM pfills WHERE side = 'buy'"))
        self.pending: dict[str, list[dict[str, Any]]] = {}
        self.curve: dict[str, tuple[int, int, float]] = {}     # mint -> reserves after its last trade, when seen
        self.tip = 0                                            # newest slot the feed has shown
        for (_, m) in self.pos:                                 # after a restart, mark open copies at the last stored price
            row = c.execute("""SELECT t.vsol, t.vtok FROM trades t JOIN mints mm ON mm.id = t.mint WHERE mm.addr = ?
                               ORDER BY t.slot DESC, t.rowid DESC LIMIT 1""", (m,)).fetchone()
            if row:
                self.curve[m] = (row[0], row[1], time.time())

    def reload(self) -> None:
        self.follow = {w for (w,) in self.c.execute("SELECT wallet FROM follow")}

    def on_trade(self, slot: int, mint: str, user: str, e: dict[str, Any]) -> None:
        self.tip = max(self.tip, slot)
        acts = self.pending.get(mint)
        if acts:                                                # copies due: land at the state before this trade
            self._run_due(mint, acts, [slot >= a["land"] for a in acts])
        self.curve[mint] = (e["vsol"], e["vtok"], time.time())
        if user not in self.follow or not e["tok"]:
            return
        key, side = (user, mint), ("buy" if e["buy"] else "sell")
        mine = [a for a in self.pending.get(mint, ()) if a["wallet"] == user]
        if any(a["side"] == side for a in mine):
            return
        if side == "buy" and key in self.copied:
            return                                              # only its first buy of a token is copied
        if side == "sell" and key not in self.pos and not mine:
            return                                              # nothing copied to sell
        if side == "buy":
            self.copied.add(key)
        leader_px = ((e["sol"] + e["fee"]) if e["buy"] else (e["sol"] - e["fee"])) / e["tok"]
        self.pending.setdefault(mint, []).append({"wallet": user, "side": side, "trigger": slot, "land": max(slot + self.L, self.tip + 1),
                                                  "rate": e["fee"] / e["sol"] if e["sol"] else FEE, "leader_px": leader_px, "t": time.time()})

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
        keep = {m for (_, m) in self.pos} | set(self.pending)
        cutoff = time.time() - max_age_s
        self.curve = {m: v for m, v in self.curve.items() if v[2] >= cutoff or m in keep}

    def summary(self) -> dict[str, Any]:
        rows = {w: {"wallet": w, "added_at": added, "golden_now": bool(g), "report_copy_roi": roi, "copied": 0, "closed": 0,
                    "open": 0, "realized": 0.0, "unrealized": 0.0, "wins": 0, "delay_slots": None, "slip_bps": None}
                for w, added, g, roi in self.c.execute("SELECT wallet, added_at, golden_now, report_copy_roi FROM follow")}
        for w, copied, closed, realized, wins, delay, slip in self.c.execute(
                """SELECT wallet, SUM(side = 'buy'), SUM(side = 'sell'), COALESCE(SUM(pnl), 0), SUM(pnl > 0),
                          AVG(land_slot - trigger_slot), AVG(slip_bps) FROM pfills GROUP BY wallet"""):
            if w in rows:
                rows[w].update(copied=copied, closed=closed, realized=realized, wins=wins, delay_slots=delay, slip_bps=slip)
        open_ = []
        for (w, m), (tok, cost, opened) in self.pos.items():
            st = self.curve.get(m)
            value = (st[0] - st[0] * st[1] / (st[1] + tok)) * (1 - FEE) / LAMPORTS if st and st[1] > 0 else None
            pnl = value - cost - 2 * self.tx if value is not None else None
            if w in rows:
                rows[w]["open"] += 1
                rows[w]["unrealized"] += pnl or 0.0
            open_.append({"wallet": w, "mint": m, "cost": cost, "value": value, "pnl": pnl, "opened": opened})
        for r in rows.values():
            r["total"] = r["realized"] + r["unrealized"]
            r["roi"] = r["total"] / (r["copied"] * self.stake) if r["copied"] else None
            r["win_rate"] = r["wins"] / r["closed"] if r["closed"] else None
        cols = ("wallet", "mint", "side", "trigger_slot", "land_slot", "ts", "sol", "slip_bps", "pnl", "timed_out")
        recent = [dict(zip(cols, r)) for r in self.c.execute(f"SELECT {', '.join(cols)} FROM pfills ORDER BY id DESC LIMIT 50")]
        return {"at": int(time.time()), "stake_sol": self.stake, "latency_slots": self.L, "tx_cost_sol": self.tx,
                "pending": sum(len(v) for v in self.pending.values()),
                "wallets": sorted(rows.values(), key=lambda r: r["added_at"] or 0), "open": open_, "recent": recent}


def update_follow(db_path: str | Path, rep: dict[str, Any]) -> int:
    """Start following every wallet this report flags golden; remember which are still golden now."""
    golden = {r["addr"]: r for r in rep.get("traders", []) if r.get("golden")}
    c = connect(db_path)
    try:
        c.execute("UPDATE follow SET golden_now = 0")
        for addr, r in golden.items():
            c.execute("""INSERT INTO follow(wallet, added_at, golden_now, report_copy_roi) VALUES (?, ?, 1, ?)
                         ON CONFLICT(wallet) DO UPDATE SET golden_now = 1, report_copy_roi = excluded.report_copy_roi""",
                      (addr, int(time.time()), r.get("copy_roi")))
        c.commit()
        return len(golden)
    finally:
        c.close()


# ---------------------------------------------------------------------------
# collector
# ---------------------------------------------------------------------------
class Collector:
    def __init__(self, db_path: str | Path, ws_url: str = PUBLIC_WS, retention_days: float = 3.0):
        self.db_path = Path(db_path)
        self.c = connect(self.db_path)
        self.ws_url = ws_url
        self.retention_s = retention_days * 86_400
        self.wallets: dict[str, int] = dict(self.c.execute("SELECT addr, id FROM wallets"))
        cutoff = time.time() - self.retention_s
        self.mints: dict[str, tuple[int, int]] = {a: (i, ts) for a, i, ts in self.c.execute("SELECT addr, id, ts FROM mints WHERE ts >= ?", (cutoff,))}
        self.buf: list[tuple] = []
        self.stats: dict[str, Any] = {"since": int(time.time()), "trades": 0, "mints": 0, "non_sol": 0, "parse_errors": 0,
                                      "reconnects": 0, **(get_meta(self.c, "stats") or {})}
        self.stats["started"] = int(time.time())
        self.stats["ws"] = ws_url.split("?")[0]              # never store an API key that may sit in the query string
        self.paper = PaperFollow(self.c)
        self.last_paper = 0.0

    def wallet_id(self, addr: str) -> int:
        i = self.wallets.get(addr)
        if i is None:
            i = self.c.execute("INSERT INTO wallets(addr) VALUES (?)", (addr,)).lastrowid
            self.wallets[addr] = i
        return i

    def on_logs(self, slot: int, logs: list[str]) -> None:
        for line in logs:
            if not line.startswith("Program data: "):
                continue
            try:
                b = base64.b64decode(line[14:])
            except ValueError:
                continue
            if b[:8] == D_CREATE:
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
            elif b[:8] == D_TRADE:
                e = parse_trade(b)
                if e is None:
                    self.stats["parse_errors"] += 1
                    continue
                if not e["sol_quote"]:
                    continue
                mint, user = b58(e["mint"]), b58(e["user"])
                self.paper.on_trade(slot, mint, user, e)          # followed wallets are copied on any token
                m = self.mints.get(mint)
                if m is None:
                    continue                      # born before we started watching: not stored
                self.buf.append((slot, e["ts"], m[0], self.wallet_id(user), int(e["buy"]),
                                 e["sol"], e["tok"], e["fee"], e["vsol"], e["vtok"]))

    def flush(self) -> None:
        if self.buf:
            self.c.executemany("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?)", self.buf)
            self.stats["trades"] += len(self.buf)
            self.buf.clear()
        self.paper.tick()
        now = time.time()
        if now - self.last_paper >= 10:
            self.paper.reload()                                   # the report thread adds golden wallets
            set_meta(self.c, "paper", self.paper.summary())
            self.last_paper = now
        self.stats["heartbeat"] = int(now)
        set_meta(self.c, "stats", self.stats)
        self.c.commit()

    def forget_old_mints(self) -> None:
        cutoff = time.time() - self.retention_s
        self.mints = {a: v for a, v in self.mints.items() if v[1] >= cutoff}
        self.paper.forget()

    async def run(self, stop: asyncio.Event | None = None) -> None:
        import websockets  # here so `pump report` works without the package
        stop = stop or asyncio.Event()
        backoff, last_flush, last_line, last_forget = 2.0, time.time(), time.time(), time.time()
        seen = (self.stats["trades"], self.stats["mints"])
        while not stop.is_set():
            try:
                async with websockets.connect(self.ws_url, max_size=2**24, max_queue=4096, ping_interval=20, ping_timeout=30) as ws:
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                                              "params": [{"mentions": [PUMP_PROGRAM]}, {"commitment": "confirmed"}]}))
                    log.info("connected to %s", self.stats["ws"])
                    backoff = 2.0
                    while not stop.is_set():
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)   # 30 s of silence on pump.fun = a stalled feed
                        res = json.loads(raw).get("params", {}).get("result")
                        if res and not res["value"].get("err"):
                            self.on_logs(res["context"]["slot"], res["value"]["logs"])
                        now = time.time()
                        if now - last_flush >= 1:
                            self.flush()
                            last_flush = now
                        if now - last_line >= 60:
                            log.info("last minute: %d trades, %d new tokens (total %d / %d)", self.stats["trades"] - seen[0],
                                     self.stats["mints"] - seen[1], self.stats["trades"], self.stats["mints"])
                            seen, last_line = (self.stats["trades"], self.stats["mints"]), now
                        if now - last_forget >= 3600:
                            self.forget_old_mints()
                            last_forget = now
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - any feed failure: keep what we have, reconnect
                self.flush()
                self.stats["reconnects"] += 1
                self.stats["last_error"] = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {type(e).__name__}: {e}"[:300]
                log.warning("feed error (%s); reconnecting in %.0fs", self.stats["last_error"], backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
        self.flush()


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


def collect(db_path: str | Path, ws_url: str, retention_days: float, report_every_s: float = 1800) -> int:
    logging.getLogger(__name__).setLevel(logging.INFO)
    col = Collector(db_path, ws_url, retention_days)
    print(f"pump collector: {col.stats['ws']}, keeping {retention_days:g} days, db {db_path}, "
          f"ranking wallets every {report_every_s / 60:.0f} min. Ctrl+C to stop.", flush=True)
    halt = threading.Event()

    def maintenance() -> None:                                 # own thread and connection: never stalls the feed
        while not halt.wait(report_every_s):
            try:
                n = prune(db_path, col.retention_s)
                rep = build_report(db_path)
                save_report(db_path, rep)
                g = update_follow(db_path, rep)
                log.info("report: %s wallets ranked, %d golden (followed from now on), %s tokens pruned",
                         rep.get("counts", {}).get("wallets_ranked"), g, n)
            except Exception:  # noqa: BLE001
                log.exception("report failed")

    threading.Thread(target=maintenance, daemon=True).start()
    try:
        asyncio.run(col.run())
    except KeyboardInterrupt:
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


def copy_trade(entry: tuple[int, int], exit_: tuple[int, int], stake_sol: float, tx_cost_sol: float) -> float:
    """SOL PnL of buying `stake_sol` at curve state `entry` and selling everything at state `exit_`."""
    vsol, vtok = entry
    net = stake_sol * LAMPORTS / (1 + FEE)
    tokens = vtok - vsol * vtok / (vsol + net)
    vsol2, vtok2 = exit_
    out = (vsol2 - vsol2 * vtok2 / (vtok2 + tokens)) * (1 - FEE) if vtok2 > 0 else 0.0
    return out / LAMPORTS - stake_sol - 2 * tx_cost_sol


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
        p = copy_trade(entry, exit_, stake_sol, tx_cost_sol)
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


def build_report(db_path: str | Path, snipe_slots: int = 2, min_tokens: int = 10, min_snipes: int = 5, latency_slots: int = 2,
                 stake_sol: float = 0.1, tx_cost_sol: float = 0.0005, top: int = 100, max_positions: int = 1000,
                 min_hours: float = 12) -> dict[str, Any]:
    """`min_hours`: no wallet is called golden before the window spans this long. Golden wallets get followed
    for good, so a verdict from the first minutes of data would pin noise to the paper test."""
    params = {"snipe_slots": snipe_slots, "min_tokens": min_tokens, "min_snipes": min_snipes, "latency_slots": latency_slots,
              "stake_sol": stake_sol, "tx_cost_sol": tx_cost_sol, "fee": FEE, "min_hours": min_hours}
    t_build = time.time()
    c = connect(db_path, readonly=True)
    try:
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
        stats = get_meta(c, "stats", {}) or {}
        return {"generated": int(time.time()), "build_s": round(time.time() - t_build, 1), "params": params,
                "window": {"start": t0, "end": t_last, "hours": (t_last - t0) / 3600},
                "counts": {"tokens": n_mints, "trades": stats.get("trades"), "wallets_ranked": len(rows)},
                "base": base, "traders": slim(sorted(cands, key=lambda r: r.get("copy_roi") or -9, reverse=True)), "snipers": slim(snipers)}
    finally:
        c.close()


def save_report(db_path: str | Path, rep: dict[str, Any]) -> None:
    c = connect(db_path)
    try:
        set_meta(c, "report", rep)
        c.commit()
    finally:
        c.close()


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
    print("\ntop snipers (first buy within", rep["params"]["snipe_slots"], "slots of creation):")
    for r in rep["snipers"][:10]:
        print(f"  {r['addr']}  snipes {r['snipes']:>5}  same-slot {r['block0']:>5}  snipe pnl {r['snipe_pnl_sol']:+8.2f} SOL")
