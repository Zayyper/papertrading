"""PumpSwap trades of the pools we track, without the rest of PumpSwap.

Subscribing to the whole PumpSwap program brought ~78,000 transactions a minute (2.9 MB/s with pump.fun, measured
2026-09-25) into a collector that stores ~5 % of them, and the public RPC throttled the server for it: a drop every
30-60 s, ~15 % of trades through. So the collector now follows pump.fun on one connection and each of its own pools
with one logsSubscribe each. The public RPC allows 100 subscription attempts per connection ("please open a new
connection"), so the pools are spread over a few connections of up to POOL_PER_CONN, subscribed the moment their coin
migrates (the CreatePoolEvent comes in on the pump.fun feed) and kept while they trade or hold an open paper copy.
A connection whose attempts are used up is retired and its pools move to a fresh one.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable

log = logging.getLogger(__name__)

POOL_PER_CONN = 95              # subscription attempts a connection may make: the public RPC closes it at the 101st
POOL_CONNS = 8                  # connections for pools, on top of the pump.fun one (the public RPC allows 40 per IP)
POOL_IDLE_S = 3600              # a pool that has not traded for this long is dropped at the next retirement
SUB_PER_S = 8.0                 # subscribe requests a second over all connections (the public RPC: 100 per 10 s per IP)


class _Conn:
    def __init__(self) -> None:
        self.pools: set[str] = set()           # every pool this connection was asked to follow
        self.attempts = 0
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.task: asyncio.Task | None = None
        self.opened = time.time()


class PoolFeed:
    def __init__(self, url: str, on_logs: Callable[[int, list[str], str | None], None],
                 pinned: Callable[[], set[str]] = set) -> None:
        self.url, self.on_logs, self.pinned = url, on_logs, pinned
        self.seen: dict[str, float] = {}      # pool -> when it last traded, or was added
        self.conns: list[_Conn] = []
        self.next_send = 0.0
        self.fails, self.wait_until = 0, 0.0
        self.kick = asyncio.Event()
        self.stats: dict[str, Any] = {"pool_conns": 0, "pools_wanted": 0, "pool_drops": 0}

    def add(self, pool: str, t: float | None = None) -> None:
        """A pool to follow, from its migration (now) or from the store at start (its last trade). A new pool is
        subscribed within a second: the first minutes after a migration are the busiest."""
        self.seen[pool] = max(self.seen.get(pool, 0.0), time.time() if t is None else t)
        self.kick.set()

    def touch(self, pool: str) -> None:
        if pool in self.seen:
            self.seen[pool] = time.time()

    def wanted(self, now: float | None = None) -> list[str]:
        """The pools worth a subscription: traded within POOL_IDLE_S or holding a paper copy, most recent first."""
        now, pinned = time.time() if now is None else now, self.pinned()
        keep = [p for p, t in self.seen.items() if now - t < POOL_IDLE_S or p in pinned]
        keep.sort(key=lambda p: (p not in pinned, -self.seen[p]))
        return keep[:POOL_PER_CONN * POOL_CONNS]

    def place(self, want: list[str], open_conn: Callable[[], _Conn]) -> None:
        """Give every wanted pool a subscription: on a connection with attempts left, else on a new one, else on the
        one retired for being the least useful (its wanted pools come back here on the next pass)."""
        self.conns = [c for c in self.conns if c.task is None or not c.task.done()]   # a dropped one's pools go again
        wset = set(want)
        placed = set().union(*(c.pools for c in self.conns)) if self.conns else set()
        for p in want:
            if p in placed:
                continue
            c = next((c for c in self.conns if c.attempts < POOL_PER_CONN), None)
            if c is None and len(self.conns) >= POOL_CONNS:
                worst = min(self.conns, key=lambda c: len(c.pools & wset))
                if worst.task is not None:
                    worst.task.cancel()
                self.conns.remove(worst)
                placed -= worst.pools
            if c is None:
                if time.time() < self.wait_until:
                    break                              # connections keep dropping: try again after the pause
                c = open_conn()
                self.conns.append(c)
            c.attempts += 1
            c.pools.add(p)
            c.queue.put_nowait(p)
            placed.add(p)
        self.stats.update(pool_conns=len(self.conns), pools_wanted=len(want),
                          pools_followed=sum(len(c.pools & wset) for c in self.conns))

    async def run(self, stop: asyncio.Event) -> None:
        import websockets
        try:
            while not stop.is_set():
                self.kick.clear()
                self.place(self.wanted(), lambda: self._open(websockets))
                try:
                    await asyncio.wait_for(self.kick.wait(), timeout=2)
                except asyncio.TimeoutError:
                    pass
        finally:
            for c in self.conns:
                if c.task is not None:
                    c.task.cancel()

    def _open(self, websockets) -> _Conn:
        c = _Conn()
        c.task = asyncio.create_task(self._conn(c, websockets))
        return c

    async def _conn(self, c: _Conn, websockets) -> None:
        why = "closed by the server"
        try:
            async with websockets.connect(self.url, max_size=2**24, max_queue=4096, ping_interval=20, ping_timeout=30) as ws:
                sender = asyncio.create_task(self._send(c, ws))
                try:
                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("method") == "logsNotification":
                            res = msg["params"]["result"]
                            if not res["value"].get("err"):
                                self.on_logs(res["context"]["slot"], res["value"]["logs"], res["value"].get("signature"))
                        elif "error" in msg:
                            log.warning("pool subscription refused: %s", str(msg["error"])[:160])
                finally:
                    sender.cancel()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - the manager puts its pools on another connection
            why = f"{type(e).__name__}: {str(e)[:120]}"
        self.stats["pool_drops"] += 1                  # ended either way: its pools go to another connection
        lived = time.time() - c.opened
        self.fails = 0 if lived >= 300 else self.fails + 1
        self.wait_until = time.time() + min(2.0 * 2 ** self.fails, 60.0)
        log.warning("pool feed connection with %d pools ended after %.0fs (%s)", len(c.pools), lived, why)

    async def _send(self, c: _Conn, ws) -> None:
        n = 0
        while True:
            pool = await c.queue.get()
            now = time.time()
            self.next_send = max(now, self.next_send) + 1 / SUB_PER_S        # shared by every connection
            if self.next_send - 1 / SUB_PER_S > now:
                await asyncio.sleep(self.next_send - 1 / SUB_PER_S - now)
            n += 1
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": n, "method": "logsSubscribe",
                                      "params": [{"mentions": [pool]}, {"commitment": "confirmed"}]}))
