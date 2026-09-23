"""Copy test, part 1: the tape. Hyperliquid's public WebSocket -> Postgres.

    HL_DATABASE_URL=postgresql://... python -m hl_screener copytest collect

What a copy test needs and the REST API cannot give after the fact is the order book a few seconds after a
leader traded. Hyperliquid's trade stream names both wallets of every trade, so subscribing to it for every perp
shows the moment a tracked wallet trades, however many wallets are tracked (the per-user stream stops at 10).
The book of that coin is then fetched at trade time + each delay of the plan (3 s, 30 s, 5 min, 1 h) and stored:
that is the price a copier at that speed would have met. The rest is cheap REST on a timer: each wallet's fills
(with the position they started from), account values, every mid price each 5 minutes, hourly funding.

Who is tracked is decided once, from copytest.toml, and stored: the screener's shortlist, the leaderboard's
top 5 and random wallets from the screener's pool. Nothing is ever sent to the exchange.
"""
from __future__ import annotations

import asyncio
import collections
import dataclasses
import hashlib
import heapq
import json
import logging
import math
import random
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .api import HyperliquidAPI
from .config import Config

log = logging.getLogger(__name__)

WS_URL = "wss://api.hyperliquid.xyz/ws"
LEAD_MS = 150                  # first guess of how far past the request the book's own timestamp lands; then measured
BOOK_BUDGET_PER_MIN = 60       # captures a minute at most (weight 2, ~1 KB each): a bot among the random wallets cannot fill the disk
GROUPS = ("shortlist", "top", "random")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracked (address TEXT NOT NULL, grp TEXT NOT NULL, added_ms BIGINT NOT NULL, info JSONB,
  PRIMARY KEY (address, grp));
CREATE TABLE IF NOT EXISTS fills (address TEXT NOT NULL, tid BIGINT NOT NULL, time_ms BIGINT NOT NULL, coin TEXT NOT NULL,
  side CHAR(1) NOT NULL, px DOUBLE PRECISION NOT NULL, sz DOUBLE PRECISION NOT NULL, start_pos DOUBLE PRECISION NOT NULL,
  fee DOUBLE PRECISION NOT NULL, closed_pnl DOUBLE PRECISION NOT NULL, crossed BOOLEAN, liquidation BOOLEAN NOT NULL, hash TEXT,
  PRIMARY KEY (address, tid));
CREATE INDEX IF NOT EXISTS fills_by_time ON fills (address, time_ms);
CREATE TABLE IF NOT EXISTS books (coin TEXT NOT NULL, taken_ms BIGINT NOT NULL, due_ms BIGINT NOT NULL,
  bids JSONB NOT NULL, asks JSONB NOT NULL, PRIMARY KEY (coin, taken_ms));
CREATE TABLE IF NOT EXISTS mids (coin TEXT NOT NULL, time_ms BIGINT NOT NULL, px DOUBLE PRECISION NOT NULL, PRIMARY KEY (coin, time_ms));
CREATE TABLE IF NOT EXISTS equity (address TEXT NOT NULL, time_ms BIGINT NOT NULL, value DOUBLE PRECISION NOT NULL,
  PRIMARY KEY (address, time_ms));
CREATE TABLE IF NOT EXISTS funding (coin TEXT NOT NULL, time_ms BIGINT NOT NULL, rate DOUBLE PRECISION NOT NULL, PRIMARY KEY (coin, time_ms));
CREATE TABLE IF NOT EXISTS trials (id BIGSERIAL PRIMARY KEY, at_ms BIGINT NOT NULL, source TEXT NOT NULL, config JSONB NOT NULL,
  config_hash TEXT NOT NULL, window_start BIGINT, window_end BIGINT, result JSONB);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value JSONB NOT NULL)
"""


def now_ms() -> int:
    return int(time.time() * 1000)


def tolerance_ms(delay_ms: int) -> int:
    """How far from its due time a captured book still prices a copy at that delay: 1 s, or a fifth of the delay."""
    return max(1000, delay_ms // 5)


def day_ms(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def is_perp(coin: str) -> bool:
    return not (coin.startswith("@") or "/" in coin or ":" in coin)    # spot pairs and builder-deployed dexes


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------
def connect(url: str):
    import psycopg
    conn = psycopg.connect(url, autocommit=True)
    for stmt in SCHEMA.split(";"):
        if stmt.strip():
            conn.execute(stmt)
    return conn


def get_meta(conn, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM meta WHERE key = %s", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn, key: str, value: Any) -> None:
    from psycopg.types.json import Jsonb
    conn.execute("INSERT INTO meta(key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, Jsonb(value)))


# ---------------------------------------------------------------------------
# the plan: frozen on the first start
# ---------------------------------------------------------------------------
def freeze_plan(conn, path: Path, now: int | None = None) -> dict[str, Any]:
    """Store the plan the first time; afterwards the stored copy is the plan. A file that differs is logged and
    ignored, unless it carries a new id whose holdout has not started yet: then the old plan is abandoned, unseen."""
    now = now or now_ms()
    raw = path.read_bytes()
    new = {"id": tomllib.loads(raw.decode("utf-8"))["id"], "sha256": hashlib.sha256(raw).hexdigest(),
           "frozen_at": now, "text": raw.decode("utf-8")}
    cur = get_meta(conn, "plan")
    if cur is None:
        set_meta(conn, "plan", new)
        log.info("copytest: plan %s frozen, sha256 %s", new["id"], new["sha256"][:16])
        return new
    if cur["sha256"] == new["sha256"]:
        return cur
    starts = day_ms(tomllib.loads(new["text"])["holdout"]["start"])
    if new["id"] != cur["id"] and starts > now and get_meta(conn, f"holdout:{cur['id']}") is None:
        set_meta(conn, f"abandoned:{cur['id']}", {**cur, "abandoned_at": now})
        set_meta(conn, "plan", new)
        log.warning("copytest: plan %s replaced by %s before its holdout began; %s will never be evaluated", cur["id"], new["id"], cur["id"])
        return new
    set_meta(conn, "plan_mismatch", {"file_sha256": new["sha256"], "seen_at": now})
    log.warning("copytest: %s differs from plan %s frozen %s: the frozen copy stays in force", path.name, cur["id"],
                datetime.fromtimestamp(cur["frozen_at"] / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    return cur


def plan_of(frozen: dict[str, Any]) -> dict[str, Any]:
    return tomllib.loads(frozen["text"])


def select_groups(conn, plan: dict[str, Any], api: HyperliquidAPI, cfg: Config, root: Path) -> dict[str, list[str]]:
    """Pick who is tracked, once: a group already in the database is never re-picked."""
    from psycopg.types.json import Jsonb
    have = {g: [a for (a,) in conn.execute("SELECT address FROM tracked WHERE grp = %s", (g,))] for g in GROUPS}
    if all(have.values()):
        return have
    g, t = plan["groups"], now_ms()
    add = lambda grp, a, info: conn.execute("INSERT INTO tracked VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",  # noqa: E731
                                            (a, grp, t, Jsonb(info)))
    if not have["shortlist"]:
        from .paper import read_leaders
        for r in read_leaders(root / g["shortlist"]):
            add("shortlist", r["address"], {"name": r.get("name"), "file": g["shortlist"]})
    lb = api.leaderboard() if not (have["top"] and have["random"]) else []
    taken = {a for (a,) in conn.execute("SELECT address FROM tracked")}
    if not have["top"]:
        pnl = lambda r: r["perf"].get(g["top_window"], {}).get("pnl", float("nan"))  # noqa: E731
        ranked = sorted((r for r in lb if r["address"].startswith("0x") and pnl(r) == pnl(r)), key=pnl, reverse=True)
        for r in ranked[: g["top_n"]]:
            add("top", r["address"], {"pnl": pnl(r), "window": g["top_window"], "account_value": r["account_value"]})
            taken.add(r["address"])
    if not have["random"]:
        from .walkforward import select_pool
        band = select_pool(lb, dataclasses.replace(cfg, pool_max_accounts=len(lb) + 1), random.Random(g["random_seed"]))
        active = sorted((r for r in band if (r["perf"].get("week", {}).get("vlm") or 0) > 0 and r["address"] not in taken),
                        key=lambda r: r["address"])
        for r in random.Random(g["random_seed"]).sample(active, min(g["random_n"], len(active))):
            add("random", r["address"], {"account_value": r["account_value"], "week_vlm": r["perf"]["week"]["vlm"]})
    return {grp: [a for (a,) in conn.execute("SELECT address FROM tracked WHERE grp = %s", (grp,))] for grp in GROUPS}


def _fill_row(addr: str, f: dict[str, Any]) -> tuple:
    liq = f.get("liquidation") or {}
    own = bool(liq) and str(liq.get("liquidatedUser", "")).lower() == addr
    return (addr, int(f["tid"]), int(f["time"]), f["coin"], f["side"], float(f["px"]), float(f["sz"]),
            float(f.get("startPosition") or 0), float(f.get("fee") or 0) + float(f.get("builderFee") or 0),
            float(f.get("closedPnl") or 0), f.get("crossed"), own, f.get("hash"))


# ---------------------------------------------------------------------------
# the collector
# ---------------------------------------------------------------------------
class Tape:
    def __init__(self, conn, api: HyperliquidAPI, plan: dict[str, Any]):
        self.conn, self.api, self.plan = conn, api, plan
        self.delays = [int(d * 1000) for d in plan["copy"]["delays_s"]]
        self.tracked: dict[str, set[str]] = collections.defaultdict(set)
        for a, g in conn.execute("SELECT address, grp FROM tracked"):
            self.tracked[a].add(g)
        self.heap: list[tuple[int, int, str]] = []          # (due_ms, delay_ms, coin)
        self.keys: set[tuple[str, int]] = set()            # (coin, due second): one book serves every copy due then
        self.tasks: set[asyncio.Task] = set()
        self.sem = asyncio.Semaphore(8)
        self.tokens, self.refill = float(BOOK_BUDGET_PER_MIN), time.monotonic()
        self.lead = float(LEAD_MS)                         # request this long before due: round trip + clock skew, measured
        self.stats: collections.Counter = collections.Counter()
        self.rr = 0
        self.last_error: str | None = None

    # ---- trade stream -> capture schedule ----------------------------------
    def on_trades(self, trades: list[dict[str, Any]]) -> None:
        for t in trades:
            if not any(str(u).lower() in self.tracked for u in t.get("users") or ()):
                continue
            self.stats["leader_trades"] += 1
            for d in self.delays:
                self.schedule(t["coin"], int(t["time"]) + d, d)

    def schedule(self, coin: str, due: int, delay: int) -> None:
        key = (coin, due // 1000)
        if key not in self.keys:
            self.keys.add(key)
            heapq.heappush(self.heap, (due, delay, coin))

    def schedule_later(self, trades: list[tuple[str, int]]) -> None:
        """Captures still ahead for trades learned of late (REST fills after a stream gap) or before a restart:
        the schedule lives in memory, the fills do not. Safe from any thread."""
        now = now_ms()
        for coin, t in trades:
            for d in self.delays:
                if t + d > now + 1000:
                    self.loop.call_soon_threadsafe(self.schedule, coin, t + d, d)

    def _token(self) -> bool:
        t = time.monotonic()
        self.tokens = min(BOOK_BUDGET_PER_MIN, self.tokens + (t - self.refill) * BOOK_BUDGET_PER_MIN / 60)
        self.refill = t
        if self.tokens < 1:
            return False
        self.tokens -= 1
        return True

    async def capture_loop(self) -> None:
        while True:
            if not self.heap:
                await asyncio.sleep(0.05)
                continue
            due, delay, coin = self.heap[0]
            wait = due - self.lead - now_ms()
            if wait > 0:
                await asyncio.sleep(min(wait, 200) / 1000)   # re-check: an earlier capture may have been queued
                continue
            heapq.heappop(self.heap)
            self.keys.discard((coin, due // 1000))
            if now_ms() - due > tolerance_ms(delay):
                self.stats["books_late"] += 1                 # e.g. history replayed on (re)connect: useless now
            elif not self._token():
                self.stats["books_dropped"] += 1
            else:
                task = asyncio.create_task(self.capture(coin, due, delay))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)

    async def capture(self, coin: str, due: int, delay: int) -> None:
        async with self.sem:
            fired = now_ms()
            try:
                book = await asyncio.to_thread(self.api.l2_book, coin)
            except Exception as e:  # noqa: BLE001 - a missed book is a coverage gap, never a crash
                self.stats["book_errors"] += 1
                self.last_error = f"l2Book {coin}: {type(e).__name__}: {e}"[:200]
                return
        levels = book.get("levels") or [[], []]
        side = lambda lv: [[float(x["px"]), float(x["sz"])] for x in lv]  # noqa: E731
        taken = int(book.get("time") or now_ms())
        self.lead = min(2000.0, max(-2000.0, 0.9 * self.lead + 0.1 * (taken - fired)))   # the exchange's clock, not ours
        await asyncio.to_thread(self._store_book, coin, taken, due, side(levels[0]), side(levels[1]))
        self.stats["books"] += 1
        if abs(taken - due) > tolerance_ms(delay):
            self.stats["books_off_time"] += 1

    def _store_book(self, coin: str, taken: int, due: int, bids: list, asks: list) -> None:
        from psycopg.types.json import Jsonb
        self.conn.execute("INSERT INTO books VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING", (coin, taken, due, Jsonb(bids), Jsonb(asks)))

    async def ws_loop(self) -> None:
        import websockets
        backoff = 2.0
        while True:
            try:
                meta, _ = await asyncio.to_thread(self.api.meta_and_ctxs)
                coins = [u["name"] for u in meta.get("universe", []) if not u.get("isDelisted") and is_perp(u["name"])]
                async with websockets.connect(WS_URL, ping_interval=None, max_size=2**24) as ws:
                    for c in coins:
                        await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "trades", "coin": c}}))
                    log.info("copytest: trade stream on %d perps, %d wallets tracked", len(coins), len(self.tracked))
                    backoff, last_ping = 2.0, time.time()
                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=15)
                        except asyncio.TimeoutError:
                            raw = None
                        if time.time() - last_ping > 30:
                            await ws.send(json.dumps({"method": "ping"}))
                            last_ping = time.time()
                        if raw is not None:
                            msg = json.loads(raw)
                            if msg.get("channel") == "trades":
                                self.on_trades(msg.get("data") or [])
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - reconnect; the REST fills fill the gap, the books of the gap are lost
                self.last_error = f"websocket: {type(e).__name__}: {e}"[:200]
                log.warning("copytest: %s; reconnecting in %.0fs", self.last_error, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    # ---- REST on a timer -----------------------------------------------------
    def store_fills(self, addr: str) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(f.time_ms), MIN(t.added_ms) - 1) FROM tracked t LEFT JOIN fills f "
                                "ON f.address = t.address WHERE t.address = %s", (addr,)).fetchone()
        start, n = int(row[0]) + 1, 0
        while True:
            page = self.api.fills_since(addr, start)
            rows = [_fill_row(addr, f) for f in page if is_perp(str(f.get("coin", "")))]
            if rows:
                with self.conn.cursor() as cur:
                    cur.executemany("INSERT INTO fills VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", rows)
                n += len(rows)
                self.schedule_later([(r[3], r[2]) for r in rows])
            if len(page) < 2000:
                return n
            top = max(int(f["time"]) for f in page)           # a full page: carry on from its last millisecond
            start = top if top > start else top + 1

    def store_fills_batch(self) -> None:
        addrs = sorted(self.tracked)
        k = max(1, math.ceil(len(addrs) / 10))               # every wallet about every 10 minutes
        for a in (addrs * 2)[self.rr:self.rr + k]:
            try:
                self.stats["fills"] += self.store_fills(a)
            except Exception as e:  # noqa: BLE001
                self.last_error = f"fills {a}: {type(e).__name__}: {e}"[:200]
        self.rr = (self.rr + k) % max(1, len(addrs))

    def store_equity(self) -> None:
        t = now_ms()
        for a in sorted(self.tracked):
            try:
                v = float((self.api.clearinghouse_state(a).get("marginSummary") or {}).get("accountValue"))
            except Exception as e:  # noqa: BLE001
                self.last_error = f"equity {a}: {type(e).__name__}: {e}"[:200]
                continue
            self.conn.execute("INSERT INTO equity VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", (a, t, v))

    def store_mids(self) -> None:
        t = now_ms()
        rows = [(c, t, px) for c, px in self.api.all_mids().items() if is_perp(c) and px == px]
        with self.conn.cursor() as cur:
            cur.executemany("INSERT INTO mids VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", rows)

    def store_funding(self) -> None:
        """Hourly funding of every coin a tracked wallet traded in the last two days, plus BTC."""
        since = now_ms() - 2 * 86_400_000
        coins = {c for (c,) in self.conn.execute("SELECT DISTINCT coin FROM fills WHERE time_ms >= %s", (since,))} | {"BTC"}
        for c in sorted(coins):
            last = self.conn.execute("SELECT MAX(time_ms) FROM funding WHERE coin = %s", (c,)).fetchone()[0]
            try:
                recs = self.api.funding_since(c, (last or since) + 1)
            except Exception as e:  # noqa: BLE001
                self.last_error = f"funding {c}: {type(e).__name__}: {e}"[:200]
                continue
            with self.conn.cursor() as cur:
                cur.executemany("INSERT INTO funding VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                                [(c, int(r["time"]), float(r["fundingRate"])) for r in recs])

    def reports(self) -> None:
        from .copygap import dev_report, evaluate_holdout
        rep = dev_report(self.conn, source="collector")
        log.info("copytest: %s", rep.get("line", "no report"))
        res = evaluate_holdout(self.conn)
        if res.get("status") == "evaluated" and res.get("fresh"):
            log.warning("copytest: HOLDOUT %s evaluated once: %s", res["id"], res["line"])

    def status_line(self) -> None:
        s, self.stats = self.stats, collections.Counter()
        books, fills, size = self.conn.execute("SELECT (SELECT COUNT(*) FROM books), (SELECT COUNT(*) FROM fills), "
                                               "pg_database_size(current_database())").fetchone()
        set_meta(self.conn, "heartbeat", {"at": now_ms(), "books": books, "fills": fills, "db_bytes": size, "last_error": self.last_error})
        log.info("copytest: last 10 min: %d leader trades, %d books (%d late, %d dropped, %d off time, %d errors), %d fills | "
                 "stored %s books, %s fills, %d wallets, database %.0f MB, request lead %.0f ms%s", s["leader_trades"], s["books"],
                 s["books_late"], s["books_dropped"], s["books_off_time"], s["book_errors"], s["fills"], books, fills, len(self.tracked),
                 size / 1e6, self.lead, f" | last error: {self.last_error}" if self.last_error else "")

    async def periodic_loop(self) -> None:
        now = time.time()
        due = {"mids": 0.0, "equity": 0.0, "fills": 0.0, "funding": now + 120, "status": now + 600, "reports": now + 3600}
        jobs = {"mids": (self.store_mids, lambda t: (t // 300 + 1) * 300), "equity": (self.store_equity, lambda t: t + 600),
                "fills": (self.store_fills_batch, lambda t: t + 60), "funding": (self.store_funding, lambda t: (t // 3600 + 1) * 3600 + 120),
                "status": (self.status_line, lambda t: t + 600), "reports": (self.reports, lambda t: t + 6 * 3600)}
        while True:
            for name, (job, nxt) in jobs.items():
                if time.time() >= due[name]:
                    try:
                        await asyncio.to_thread(job)
                    except Exception:  # noqa: BLE001
                        log.exception("copytest: %s failed", name)
                    due[name] = nxt(time.time())
            await asyncio.sleep(5)

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.schedule_later([(c, int(t)) for c, t in self.conn.execute("SELECT coin, time_ms FROM fills WHERE time_ms > %s",
                                                                          (now_ms() - max(self.delays),))])
        await asyncio.gather(self.ws_loop(), self.capture_loop(), self.periodic_loop())


def collect(db_url: str, plan_path: Path, cfg: Config, root: Path) -> int:
    logging.getLogger(__name__).setLevel(logging.INFO)
    logging.getLogger("hl_screener.copygap").setLevel(logging.INFO)
    conn = connect(db_url)
    frozen = freeze_plan(conn, plan_path)
    plan = plan_of(frozen)
    api = HyperliquidAPI(cfg.api_url, cfg.leaderboard_url, Path(cfg.data_dir) / "cache", 500, cfg.http_timeout_s)   # the paper trader shares the IP
    while True:
        try:
            groups = select_groups(conn, plan, api, cfg, root)
            break
        except Exception as e:  # noqa: BLE001 - the leaderboard is needed once; wait for it rather than start half-picked
            log.warning("copytest: picking the groups failed (%s); retrying in 60 s", e)
            time.sleep(60)
    print(f"copytest collector: plan {frozen['id']} (sha256 {frozen['sha256'][:16]}), "
          + ", ".join(f"{len(v)} {k}" for k, v in groups.items()) + f", delays {plan['copy']['delays_s']} s. Ctrl+C to stop.", flush=True)
    try:
        asyncio.run(Tape(conn, api, plan).run())
    except KeyboardInterrupt:
        pass
    finally:
        conn.close()
    return 0
