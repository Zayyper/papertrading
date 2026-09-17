"""Forward paper test: follow leaders live and fill at the real order book.

    python -m hl_screener paper --leaders out/shortlist_2026-09-17.csv
    python -m hl_screener paper --status

One virtual account per leader, with the simulator's sizing rule: a fixed equity base
(follower_equity_usd), position size = leader size x (base / leader equity), capped at
follower_max_leverage. Every leader fill arrives over Hyperliquid's public WebSocket
(`userFills`, no key). The follower's order is priced by walking the `l2Book` at the moment the
fill is seen (taker), pays taker + builder fees, and pays or receives hourly funding on open
positions. Nothing is ever sent to the exchange.

Everything is written to SQLite (data/paper/paper.db, WAL mode) so the web page's Paper tab and
`--status` can read it while the service runs. The service resumes from the database on restart
and reconciles fills it missed while disconnected (marked `late`, priced at the current book).
"""
from __future__ import annotations

import asyncio
import csv
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .api import HyperliquidAPI
from .config import Config

log = logging.getLogger(__name__)

WS_URL = "wss://api.hyperliquid.xyz/ws"
ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}")
EPS = 1e-12


# ---------------------------------------------------------------------------
# pure logic (unit-tested)
# ---------------------------------------------------------------------------
def plan_follow(leader_start: float, leader_delta: float, follower_size: float, ratio: float,
                cap_size: float) -> list[tuple[str, float]]:
    """What the follower does when the leader's position moves from `leader_start` by `leader_delta`.

    Returns [(action, signed_size)]. Opens/adds mirror the leader's delta scaled by `ratio`, capped so
    the follower never holds more than `cap_size` coins. Reduces/closes are proportional to the share
    of the leader's position that was closed, so a follower that joined late still exits in step.
    A flip closes everything and opens the new side.
    """
    leader_end = leader_start + leader_delta
    acts: list[tuple[str, float]] = []
    same_sign = leader_start * leader_end > 0

    def capped_add(size: float) -> float:
        new = follower_size + size
        if abs(new) > cap_size:
            new = cap_size if new > 0 else -cap_size
        return new - follower_size

    if abs(leader_start) <= EPS or (same_sign and abs(leader_end) > abs(leader_start)):
        size = capped_add(leader_delta * ratio)
        if abs(size) > EPS:
            acts.append(("open" if abs(follower_size) <= EPS else "add", size))
    elif same_sign or abs(leader_end) <= EPS:
        if abs(follower_size) > EPS and follower_size * leader_start > 0:
            frac = min(1.0, abs(leader_delta) / abs(leader_start))
            size = -follower_size * frac
            acts.append(("close" if frac >= 1.0 - 1e-9 else "reduce", size))
    else:  # flip
        if abs(follower_size) > EPS and follower_size * leader_start > 0:
            acts.append(("close", -follower_size))
            follower_size = 0.0
        size = capped_add(leader_end * ratio)
        if abs(size) > EPS:
            acts.append(("open", size))
    return acts


def walk_book(book: dict[str, Any], side: str, size: float) -> tuple[float, int, float]:
    """VWAP for taking `size` coins from the book ('B' buys the asks, 'A' sells into the bids).

    Returns (vwap, levels_used, unfilled). Unfilled size beyond the visible book is priced at the
    last visible level (an optimistic floor; it is reported so it can be judged).
    """
    levels = book.get("levels") or [[], []]
    side_levels = levels[1] if side == "B" else levels[0]
    remaining, cost, used, last_px = size, 0.0, 0, 0.0
    for lv in side_levels:
        px, sz = float(lv["px"]), float(lv["sz"])
        if px <= 0 or sz <= 0:
            continue
        take = min(remaining, sz)
        cost += take * px
        remaining -= take
        used += 1
        last_px = px
        if remaining <= EPS:
            break
    if remaining > EPS and last_px > 0:
        cost += remaining * last_px
    filled = size - max(remaining, 0.0) if last_px > 0 else 0.0
    if size <= EPS or last_px <= 0:
        return float("nan"), used, size
    return cost / size, used, max(remaining, 0.0) if filled < size else 0.0


def slippage_bps(leader_px: float, fill_px: float, side: str) -> float:
    """Adverse move versus the leader's price, in bps; positive = follower got a worse price."""
    if leader_px <= 0 or fill_px != fill_px:
        return float("nan")
    adverse = (fill_px - leader_px) if side == "B" else (leader_px - fill_px)
    return adverse / leader_px * 1e4


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS leaders (
  address TEXT PRIMARY KEY, name TEXT, equity_base REAL, started_at INTEGER,
  leader_equity REAL, leader_equity_at INTEGER, leader_equity_start REAL,
  realized REAL DEFAULT 0, fees REAL DEFAULT 0, funding REAL DEFAULT 0,
  last_fill_time INTEGER DEFAULT 0, model_penalty_bps REAL, model_is_roi REAL, model_oos_roi REAL);
CREATE TABLE IF NOT EXISTS leader_fills (
  tid TEXT PRIMARY KEY, address TEXT, time INTEGER, coin TEXT, side TEXT, px REAL, sz REAL,
  start_pos REAL, dir TEXT, closed_pnl REAL, fee REAL, liquidation INTEGER, received_at INTEGER, late INTEGER);
CREATE TABLE IF NOT EXISTS paper_fills (
  id INTEGER PRIMARY KEY AUTOINCREMENT, address TEXT, tid TEXT, time INTEGER, coin TEXT, action TEXT,
  side TEXT, size REAL, leader_px REAL, fill_px REAL, notional REAL, slippage_bps REAL, latency_ms INTEGER,
  fee REAL, realized REAL, levels_used INTEGER, unfilled REAL, late INTEGER);
CREATE TABLE IF NOT EXISTS positions (
  address TEXT, coin TEXT, size REAL, entry_px REAL, opened_at INTEGER, updated_at INTEGER, PRIMARY KEY (address, coin));
CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT, address TEXT, time INTEGER, realized REAL, fees REAL, funding REAL,
  unrealized REAL, equity REAL, leader_equity REAL, n_open INTEGER);
CREATE TABLE IF NOT EXISTS funding_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, address TEXT, coin TEXT, time INTEGER, rate REAL, notional REAL, amount REAL);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, time INTEGER, level TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS ix_snapshots ON snapshots(address, time);
CREATE INDEX IF NOT EXISTS ix_paper_fills ON paper_fills(address, time);
"""


class Store:
    def __init__(self, path: str | Path, readonly: bool = False):
        p = Path(path)
        if readonly:
            self.conn = sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True, check_same_thread=False, timeout=5)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(p, check_same_thread=False, timeout=10)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
        self.conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self.conn.close()

    def exec(self, sql: str, *args: Any) -> None:
        self.conn.execute(sql, args)
        self.conn.commit()

    def rows(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, *args: Any) -> dict[str, Any] | None:
        r = self.conn.execute(sql, args).fetchone()
        return dict(r) if r else None

    def set_meta(self, key: str, value: Any) -> None:
        self.exec("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", key, json.dumps(value))

    def get_meta(self, key: str, default: Any = None) -> Any:
        r = self.one("SELECT value FROM meta WHERE key=?", key)
        return json.loads(r["value"]) if r else default

    def event(self, level: str, message: str) -> None:
        self.exec("INSERT INTO events(time, level, message) VALUES (?, ?, ?)", now_ms(), level, message)
        (log.warning if level == "warn" else log.error if level == "error" else log.info)(message)


def now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# the trader
# ---------------------------------------------------------------------------
@dataclass
class FPos:
    size: float
    entry_px: float
    opened_at: int


@dataclass
class Leader:
    address: str
    name: str | None = None
    equity: float | None = None
    equity_at: int = 0
    last_fill_time: int = 0


def read_leaders(path: Path) -> list[dict[str, Any]]:
    """Leaders from a shortlist/traders CSV (address + model columns) or a plain list of addresses."""
    text = path.read_text(encoding="utf-8")
    out: list[dict[str, Any]] = []
    if text.lstrip().lower().startswith("address"):
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                a = row.get("address", "").strip().lower()
                if not ADDRESS_RE.fullmatch(a):
                    continue
                out.append({"address": a, "name": row.get("display_name") or None,
                            "model_penalty_bps": _f(row.get("is_f_avg_penalty_bps")),
                            "model_is_roi": _f(row.get("is_f_roi")), "model_oos_roi": _f(row.get("oos_f_roi"))})
    else:
        for a in dict.fromkeys(m.lower() for m in ADDRESS_RE.findall(text)):
            out.append({"address": a, "name": None, "model_penalty_bps": None, "model_is_roi": None, "model_oos_roi": None})
    return out


def _f(x: Any) -> float | None:
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


class PaperTrader:
    def __init__(self, api: HyperliquidAPI, cfg: Config, leaders: list[dict[str, Any]], store: Store,
                 equity_base: float, max_leverage: float, ws_url: str = WS_URL):
        self.api, self.cfg, self.store = api, cfg, store
        self.equity_base, self.max_leverage, self.ws_url = equity_base, max_leverage, ws_url
        self.fee_bps = cfg.taker_fee_bps + cfg.builder_fee_bps
        self.mids: dict[str, float] = {}
        self.leaders: dict[str, Leader] = {}
        self.positions: dict[str, dict[str, FPos]] = {}
        self.totals: dict[str, dict[str, float]] = {}
        self._stop = asyncio.Event()
        t = now_ms()
        for L in leaders:
            a = L["address"]
            row = store.one("SELECT * FROM leaders WHERE address=?", a)
            if row is None:
                store.exec("INSERT INTO leaders(address, name, equity_base, started_at, model_penalty_bps, model_is_roi, model_oos_roi) VALUES (?,?,?,?,?,?,?)",
                           a, L.get("name"), equity_base, t, L.get("model_penalty_bps"), L.get("model_is_roi"), L.get("model_oos_roi"))
                row = store.one("SELECT * FROM leaders WHERE address=?", a)
            assert row is not None
            self.leaders[a] = Leader(address=a, name=row["name"], equity=row["leader_equity"], equity_at=row["leader_equity_at"] or 0,
                                     last_fill_time=row["last_fill_time"] or 0)
            self.totals[a] = {"realized": row["realized"] or 0.0, "fees": row["fees"] or 0.0, "funding": row["funding"] or 0.0}
            self.positions[a] = {p["coin"]: FPos(p["size"], p["entry_px"], p["opened_at"]) for p in store.rows("SELECT * FROM positions WHERE address=?", a)}
        store.set_meta("config", {"equity_base": equity_base, "max_leverage": max_leverage, "fee_bps": self.fee_bps, "leaders": list(self.leaders)})

    # ---- accounting ---------------------------------------------------------
    def unrealized(self, addr: str) -> float:
        tot = 0.0
        for coin, p in self.positions[addr].items():
            mark = self.mids.get(coin, p.entry_px)
            tot += (mark - p.entry_px) * p.size
        return tot

    def equity(self, addr: str) -> float:
        t = self.totals[addr]
        return self.equity_base + t["realized"] - t["fees"] + t["funding"] + self.unrealized(addr)

    def _save_position(self, addr: str, coin: str) -> None:
        p = self.positions[addr].get(coin)
        if p is None or abs(p.size) <= EPS:
            self.positions[addr].pop(coin, None)
            self.store.exec("DELETE FROM positions WHERE address=? AND coin=?", addr, coin)
        else:
            self.store.exec("INSERT INTO positions(address, coin, size, entry_px, opened_at, updated_at) VALUES (?,?,?,?,?,?) "
                            "ON CONFLICT(address, coin) DO UPDATE SET size=excluded.size, entry_px=excluded.entry_px, updated_at=excluded.updated_at",
                            addr, coin, p.size, p.entry_px, p.opened_at, now_ms())

    def _save_totals(self, addr: str) -> None:
        t, L = self.totals[addr], self.leaders[addr]
        self.store.exec("UPDATE leaders SET realized=?, fees=?, funding=?, last_fill_time=?, leader_equity=?, leader_equity_at=? WHERE address=?",
                        t["realized"], t["fees"], t["funding"], L.last_fill_time, L.equity, L.equity_at, addr)

    # ---- leader fills -------------------------------------------------------
    async def on_fill(self, addr: str, f: dict[str, Any], late: bool) -> None:
        tid = str(f.get("tid") or f.get("hash"))
        if self.store.one("SELECT 1 FROM leader_fills WHERE tid=?", tid):
            return
        coin, t = str(f.get("coin", "")), int(f["time"])
        px, sz, side = float(f["px"]), abs(float(f["sz"])), str(f.get("side", "B"))
        start = float(f.get("startPosition", 0.0))
        liq = f.get("liquidation")
        own_liq = bool(liq) and str((liq or {}).get("liquidatedUser", "")).lower() == addr
        self.store.exec("INSERT INTO leader_fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", tid, addr, t, coin, side, px, sz, start,
                        f.get("dir"), _f(f.get("closedPnl")) or 0.0, _f(f.get("fee")) or 0.0, int(own_liq), now_ms(), int(late))
        L = self.leaders[addr]
        L.last_fill_time = max(L.last_fill_time, t)
        if coin.startswith("@") or "/" in coin:
            self._save_totals(addr)
            return  # spot
        if own_liq:
            self.store.event("warn", f"{short(addr)} was LIQUIDATED on {coin} (leader fill {tid})")
        if not L.equity or L.equity <= 0:
            self.store.event("warn", f"{short(addr)}: no leader equity yet, fill on {coin} not followed")
            self._save_totals(addr)
            return
        delta = sz if side == "B" else -sz
        ratio = self.equity_base / L.equity
        pos = self.positions[addr].get(coin)
        f_size = pos.size if pos else 0.0
        cap_size = self.max_leverage * self.equity_base / px
        acts = plan_follow(start, delta, f_size, ratio, cap_size)
        if not acts:
            self._save_totals(addr)
            return
        try:
            book = await asyncio.to_thread(self.api.l2_book, coin)
        except Exception as e:  # noqa: BLE001
            self.store.event("error", f"l2Book {coin} failed ({e}); pricing at leader px + 10 bps")
            book = {"levels": [[{"px": px * (1 - 1e-3), "sz": 1e12}], [{"px": px * (1 + 1e-3), "sz": 1e12}]]}
        detected = now_ms()
        for action, size in acts:
            side_f = "B" if size > 0 else "A"
            fill_px, used, unfilled = walk_book(book, side_f, abs(size))
            if fill_px != fill_px:
                fill_px = self.mids.get(coin, px)
            notional = abs(size) * fill_px
            fee = notional * self.fee_bps / 1e4
            realized = 0.0
            pos = self.positions[addr].get(coin)
            if action in ("open", "add"):
                if pos is None or abs(pos.size) <= EPS:
                    self.positions[addr][coin] = FPos(size, fill_px, detected)
                else:
                    new_size = pos.size + size
                    pos.entry_px = (pos.entry_px * abs(pos.size) + fill_px * abs(size)) / abs(new_size)
                    pos.size = new_size
            else:
                assert pos is not None
                direction = 1.0 if pos.size > 0 else -1.0
                realized = (fill_px - pos.entry_px) * abs(size) * direction
                pos.size += size
            self.totals[addr]["realized"] += realized
            self.totals[addr]["fees"] += fee
            self.store.exec("INSERT INTO paper_fills(address, tid, time, coin, action, side, size, leader_px, fill_px, notional, slippage_bps, latency_ms, fee, realized, levels_used, unfilled, late) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", addr, tid, detected, coin, action, side_f, size, px, fill_px, notional,
                            slippage_bps(px, fill_px, side_f), detected - t, fee, realized, used, unfilled, int(late))
            self._save_position(addr, coin)
            log.info("%s %s %s %.6g %s @ %.6g (leader %.6g, %+.1f bps, %d ms%s) realized %+.2f fee %.2f",
                     short(addr), action, coin, size, side_f, fill_px, px, slippage_bps(px, fill_px, side_f), detected - t, " LATE" if late else "", realized, fee)
        self._save_totals(addr)

    # ---- periodic work --------------------------------------------------------
    async def refresh_leader_equity(self) -> None:
        for addr, L in self.leaders.items():
            try:
                st = await asyncio.to_thread(self.api.clearinghouse_state, addr)
                v = _f((st.get("marginSummary") or {}).get("accountValue"))
                if v is not None:
                    L.equity, L.equity_at = v, now_ms()
                    self.store.exec("UPDATE leaders SET leader_equity=?, leader_equity_at=?, leader_equity_start=COALESCE(leader_equity_start, ?) WHERE address=?", v, L.equity_at, v, addr)
            except Exception as e:  # noqa: BLE001
                self.store.event("warn", f"clearinghouseState {short(addr)} failed: {e}")

    async def refresh_mids(self) -> None:
        try:
            self.mids.update(await asyncio.to_thread(self.api.all_mids))
        except Exception as e:  # noqa: BLE001
            self.store.event("warn", f"allMids failed: {e}")

    def snapshot(self) -> None:
        t = now_ms()
        for addr, L in self.leaders.items():
            tot = self.totals[addr]
            self.store.exec("INSERT INTO snapshots(address, time, realized, fees, funding, unrealized, equity, leader_equity, n_open) VALUES (?,?,?,?,?,?,?,?,?)",
                            addr, t, tot["realized"], tot["fees"], tot["funding"], self.unrealized(addr), self.equity(addr), L.equity, len(self.positions[addr]))
        self.store.set_meta("heartbeat", t)

    async def apply_funding(self) -> None:
        try:
            rates = await asyncio.to_thread(self.api.live_funding_rates)
        except Exception as e:  # noqa: BLE001
            self.store.event("warn", f"funding rates failed: {e}")
            return
        t = now_ms()
        for addr, coins in self.positions.items():
            for coin, p in list(coins.items()):
                rate = rates.get(coin)
                if rate is None or rate != rate:
                    continue
                mark = self.mids.get(coin, p.entry_px)
                notional = abs(p.size) * mark
                amount = -(1.0 if p.size > 0 else -1.0) * rate * notional   # longs pay positive funding
                self.totals[addr]["funding"] += amount
                self.store.exec("INSERT INTO funding_events(address, coin, time, rate, notional, amount) VALUES (?,?,?,?,?,?)", addr, coin, t, rate, notional, amount)
            self._save_totals(addr)

    async def reconcile(self) -> None:
        """After a (re)connect: fetch fills we may have missed and process them as late."""
        for addr, L in self.leaders.items():
            if L.last_fill_time <= 0:
                continue
            try:
                fills = await asyncio.to_thread(self.api.fills_since, addr, L.last_fill_time + 1)
            except Exception as e:  # noqa: BLE001
                self.store.event("warn", f"reconcile {short(addr)} failed: {e}")
                continue
            n = 0
            for f in fills:
                if not self.store.one("SELECT 1 FROM leader_fills WHERE tid=?", str(f.get("tid") or f.get("hash"))):
                    await self.on_fill(addr, f, late=True)
                    n += 1
            if n:
                self.store.event("warn", f"{short(addr)}: {n} fills happened while disconnected, followed late")

    # ---- main loops -------------------------------------------------------------
    async def run(self) -> None:
        self.store.event("info", f"paper trader starting: {len(self.leaders)} leaders, base ${self.equity_base:,.0f}, max {self.max_leverage}x, fee {self.fee_bps} bps")
        await self.refresh_mids()
        await self.refresh_leader_equity()
        self.snapshot()
        tasks = [asyncio.create_task(self.ws_loop()), asyncio.create_task(self.periodic_loop())]
        await self._stop.wait()
        for t in tasks:
            t.cancel()
        self.snapshot()
        self.store.event("info", "paper trader stopped")

    def stop(self) -> None:
        self._stop.set()

    async def periodic_loop(self) -> None:
        last_snap = last_eq = 0.0
        next_funding = (time.time() // 3600 + 1) * 3600
        while not self._stop.is_set():
            now = time.time()
            if now - last_eq >= 600:
                await self.refresh_leader_equity()
                last_eq = now
            if now - last_snap >= 300:
                if not self.mids:
                    await self.refresh_mids()
                self.snapshot()
                last_snap = now
            if now >= next_funding:
                await self.refresh_mids()
                await self.apply_funding()
                next_funding += 3600
            await asyncio.sleep(5)

    async def ws_loop(self) -> None:
        import websockets  # imported here so --status works without the package
        backoff = 2.0
        first = True
        while not self._stop.is_set():
            try:
                async with websockets.connect(self.ws_url, ping_interval=None, max_size=2**24) as ws:
                    for addr in self.leaders:
                        await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "userFills", "user": addr}}))
                    await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "allMids"}}))
                    self.store.event("info", "websocket connected" if first else "websocket reconnected")
                    if not first:
                        await self.reconcile()
                    first = False
                    backoff = 2.0
                    last_ping = time.time()
                    while not self._stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=15)
                        except asyncio.TimeoutError:
                            raw = None
                        if time.time() - last_ping > 30:
                            await ws.send(json.dumps({"method": "ping"}))
                            last_ping = time.time()
                        if raw is None:
                            continue
                        msg = json.loads(raw)
                        ch = msg.get("channel")
                        if ch == "allMids":
                            for k, v in (msg.get("data", {}).get("mids") or {}).items():
                                fv = _f(v)
                                if fv is not None:
                                    self.mids[k] = fv
                        elif ch == "userFills":
                            data = msg.get("data", {})
                            addr = str(data.get("user", "")).lower()
                            fills = sorted(data.get("fills") or [], key=lambda f: (int(f["time"]), f.get("tid", 0)))
                            if addr not in self.leaders:
                                continue
                            if data.get("isSnapshot"):
                                # history, not new trades: only remember where the stream starts
                                L = self.leaders[addr]
                                if fills and L.last_fill_time == 0:
                                    L.last_fill_time = int(fills[-1]["time"])
                                    self._save_totals(addr)
                                continue
                            for f in fills:
                                await self.on_fill(addr, f, late=False)
                        self.store.set_meta("heartbeat", now_ms())
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.store.event("warn", f"websocket error: {type(e).__name__}: {e}; reconnecting in {backoff:.0f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)


def short(a: str) -> str:
    return a[:6] + "…" + a[-4:]


# ---------------------------------------------------------------------------
# status (read-only)
# ---------------------------------------------------------------------------
def status(db_path: str | Path, mids: dict[str, float] | None = None) -> dict[str, Any]:
    """Everything the Paper tab / --status shows, from the database only."""
    p = Path(db_path)
    if not p.exists():
        return {"exists": False}
    st = Store(p, readonly=True)
    try:
        cfg = st.get_meta("config", {})
        hb = st.get_meta("heartbeat", 0)
        leaders = []
        for L in st.rows("SELECT * FROM leaders ORDER BY started_at"):
            a = L["address"]
            pos = st.rows("SELECT * FROM positions WHERE address=?", a)
            unreal = 0.0
            for q in pos:
                mark = (mids or {}).get(q["coin"], q["entry_px"])
                q["mark"] = mark
                q["unrealized"] = (mark - q["entry_px"]) * q["size"]
                q["notional"] = abs(q["size"]) * mark
                unreal += q["unrealized"]
            f = st.one("SELECT COUNT(*) n, AVG(slippage_bps) slip, AVG(latency_ms) lat, SUM(late) late, SUM(unfilled > 0) thin, MAX(time) last FROM paper_fills WHERE address=?", a) or {}
            lf = st.one("SELECT COUNT(*) n, MAX(time) last, SUM(liquidation) liq FROM leader_fills WHERE address=?", a) or {}
            base = L["equity_base"] or 0.0
            equity = base + (L["realized"] or 0) - (L["fees"] or 0) + (L["funding"] or 0) + unreal
            leaders.append({**L, "unrealized": unreal, "equity": equity, "roi": (equity / base - 1) if base else None,
                            "leader_roi": ((L["leader_equity"] or 0) / L["leader_equity_start"] - 1) if L.get("leader_equity_start") else None,
                            "n_paper_fills": f.get("n") or 0, "avg_slippage_bps": f.get("slip"), "avg_latency_ms": f.get("lat"),
                            "n_late": f.get("late") or 0, "n_thin_book": f.get("thin") or 0, "last_paper_fill": f.get("last"),
                            "n_leader_fills": lf.get("n") or 0, "last_leader_fill": lf.get("last"), "leader_liquidations": lf.get("liq") or 0,
                            "positions": pos})
        snaps: dict[str, list[list[float]]] = {}
        for r in st.rows("SELECT address, time, equity, leader_equity FROM snapshots ORDER BY time"):
            snaps.setdefault(r["address"], []).append([r["time"], r["equity"], r["leader_equity"]])
        fills = st.rows("SELECT * FROM paper_fills ORDER BY time DESC LIMIT 200")
        events = st.rows("SELECT * FROM events ORDER BY time DESC LIMIT 60")
        return {"exists": True, "config": cfg, "heartbeat": hb, "heartbeat_age_s": (now_ms() - hb) / 1000 if hb else None,
                "leaders": leaders, "snapshots": snaps, "fills": fills, "events": events}
    finally:
        st.close()


def print_status(db_path: str | Path, api: HyperliquidAPI | None = None) -> int:
    mids = None
    if api is not None:
        try:
            mids = api.all_mids()
        except Exception:  # noqa: BLE001
            mids = None
    s = status(db_path, mids)
    if not s.get("exists"):
        print(f"no paper database at {db_path}; start the service first")
        return 1
    age = s["heartbeat_age_s"]
    print(f"paper trader: heartbeat {age:.0f}s ago" if age is not None else "paper trader: never ran", "| base per leader", s["config"].get("equity_base"))
    print(f"{'leader':16} {'equity':>9} {'return':>8} {'realized':>9} {'unreal':>8} {'fees':>7} {'funding':>8} {'open':>4} {'fills':>5} {'slip bps':>8} {'model':>6} {'latency':>8} {'leader ret':>10}")
    for L in s["leaders"]:
        print(f"{short(L['address']):16} {L['equity']:>9,.0f} {(L['roi'] or 0) * 100:>7.1f}% {L['realized'] or 0:>9,.0f} {L['unrealized']:>8,.0f} {L['fees'] or 0:>7,.0f} "
              f"{L['funding'] or 0:>8,.1f} {len(L['positions']):>4} {L['n_paper_fills']:>5} {(L['avg_slippage_bps'] or 0):>8.1f} {(L['model_penalty_bps'] or 0):>6.1f} "
              f"{(L['avg_latency_ms'] or 0) / 1000:>7.1f}s {((L['leader_roi'] or 0) * 100):>9.1f}%")
    for e in s["events"][:8]:
        print(f"  [{time.strftime('%m-%d %H:%M', time.localtime(e['time'] / 1000))}] {e['level']}: {e['message']}")
    return 0


def serve(api: HyperliquidAPI, cfg: Config, leaders_path: Path, db_path: Path, equity_base: float, max_leverage: float) -> int:
    leaders = read_leaders(leaders_path)
    if not leaders:
        print(f"no addresses in {leaders_path}")
        return 2
    store = Store(db_path)
    trader = PaperTrader(api, cfg, leaders, store, equity_base, max_leverage)
    logging.getLogger(__name__).setLevel(logging.INFO)  # fills are printed even without -v
    print(f"paper trader: {len(leaders)} leaders from {leaders_path}, ${equity_base:,.0f} each, max {max_leverage}x, db {db_path}. Ctrl+C to stop.", flush=True)

    async def main() -> None:
        loop = asyncio.get_running_loop()
        try:
            import signal
            loop.add_signal_handler(signal.SIGINT, trader.stop)
        except (NotImplementedError, AttributeError):
            pass  # Windows: KeyboardInterrupt below
        await trader.run()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        trader.snapshot()
        store.event("info", "paper trader stopped (Ctrl+C)")
    finally:
        store.close()
    return 0
