import asyncio
import json
import sqlite3
import sys
import time
import types

from hl_screener.pumpfun import FALLBACK_PER_DAY, WAL_LIMIT, Collector, checkpoint, connect, get_meta, paper_lines, set_meta


class Silent:
    """A websocket that connects and never says a word; its close takes a moment, like a closing handshake."""
    def __init__(self, events):
        self.events = events

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await asyncio.sleep(0.05)
        self.events.append("closed")

    async def send(self, msg):
        self.events.append("sent")

    async def recv(self):
        await asyncio.Event().wait()

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.recv()


class Talking(Silent):
    """A websocket that says what it was given and what the test puts in its queue, and nothing else."""
    def __init__(self, events, *said):
        super().__init__(events)
        self.q = asyncio.Queue()
        for msg in said:
            self.q.put_nowait(msg)

    async def recv(self):
        return await self.q.get()


class Stream(Silent):
    """A websocket that says `msg` every 50 ms, `n` times, then is closed by the server."""
    def __init__(self, msg, n):
        super().__init__([])
        self.msg, self.n = msg, n

    async def recv(self):
        await asyncio.sleep(0.05)
        if self.n <= 0:
            raise ConnectionError("sent 1002 (protocol error) invalid status code; no close frame received")
        self.n -= 1
        return self.msg


SLOT = json.dumps({"method": "slotNotification", "params": {"result": {"slot": 7}}})
LOGS = json.dumps({"method": "logsNotification", "params": {"result": {"context": {"slot": 7},
                                                                      "value": {"err": None, "logs": [], "signature": "sig"}}}})


def test_a_feed_that_keeps_dropping_waits_longer_and_borrows_the_fallback_twice_a_day(tmp_path):
    col = Collector(tmp_path / "pump.db", fallback_url="wss://fallback.example")
    waits = [col._dropped(5, on_fallback=False) for _ in range(5)]    # connects, then dies within seconds, again and again
    assert waits == [2, 4, 8, 16, 32]                                 # not back in 2 s every time: that is what gets throttled
    assert col.fallback_used == FALLBACK_PER_DAY == 2                 # the fallback, but only twice a day
    assert col._dropped(600, on_fallback=False) == 2 and col.fails == 0   # a connection that held is a fresh start
    col.fallback_until = 1e12
    col._dropped(0, on_fallback=True)                                  # the fallback turns us away at the door ...
    assert col.fallback_until == 0.0                                   # ... so its window ends and the main feed is tried
    col.c.close()


def test_a_connection_the_server_closed_after_it_worked_is_back_in_2_s(tmp_path):
    col = Collector(tmp_path / "pump.db", fallback_url="wss://fallback.example")
    assert [col._dropped(90, on_fallback=False) for _ in range(4)] == [2, 2, 2, 2]   # 2026-09-29: closed every 1-5 min,
    assert col.fallback_used == 0                                     # and 60 s waits lost up to half the trades; nor do those
    assert col._dropped(0, on_fallback=False) == 4 and col.fallback_used == 0   # closes spend the fallback, nor does one
    assert col._dropped(0, on_fallback=False) == 8 and col.fallback_used == 1   # refusal after them: two in a row do
    col.c.close()
    from hl_screener.pumppools import PoolFeed, _Conn
    pf = PoolFeed("wss://public.example", lambda *a: None)

    def closed(*args, **kwargs):
        raise ConnectionError("sent 1002 (protocol error) invalid status code; no close frame received")

    def drop(lived):                                                  # the pool feed's next connection waits until
        c = _Conn()
        c.opened = time.time() - lived
        asyncio.run(pf._conn(c, types.SimpleNamespace(connect=closed)))
        return round(pf.wait_until - time.time())

    assert [drop(90) for _ in range(3)] == [2, 2, 2]
    assert [drop(0) for _ in range(3)] == [4, 8, 16]                  # refused at the door: longer each time


def test_writes_are_committed_every_second_while_the_pump_feed_is_silent(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "websockets", types.SimpleNamespace(connect=lambda *a, **k: Silent([])))
    col = Collector(tmp_path / "pump.db")
    col.check_gap = lambda why: None

    async def main():
        stop = asyncio.Event()
        task = asyncio.create_task(col.run(stop))
        await asyncio.sleep(0.2)
        col.wallet_id("PoolTrader")                                   # a pool trade by a new wallet: a write, left open
        await asyncio.sleep(1.5)                                      # the pump.fun feed says nothing meanwhile
        other = sqlite3.connect(tmp_path / "pump.db", timeout=0.2)    # the maintenance thread's own connection
        try:
            set_meta(other, "report", {"ok": 1})                      # "database is locked" on 2026-09-29
            other.commit()
            assert "heartbeat" not in get_meta(other, "stats")        # written, yet the page still says the feed is silent
            return other.execute("SELECT COUNT(*) FROM wallets WHERE addr = 'PoolTrader'").fetchone()[0]
        finally:
            other.close()
            stop.set()
            await task

    assert asyncio.run(main()) == 1
    col.c.close()


def test_the_page_says_live_only_while_pump_funs_logs_arrive(tmp_path, monkeypatch):
    ws = Talking([])
    monkeypatch.setitem(sys.modules, "websockets", types.SimpleNamespace(connect=lambda *a, **k: ws))
    col = Collector(tmp_path / "pump.db")
    col.check_gap = lambda why: None

    async def main():
        stop = asyncio.Event()
        task = asyncio.create_task(col.run(stop))
        await ws.q.put(SLOT)
        await asyncio.sleep(1.3)                                      # a write goes by meanwhile
        slots_only = col.stats.get("heartbeat")                       # the slots come, pump.fun's logs do not: not live
        await ws.q.put(LOGS)
        await asyncio.sleep(0.1)
        logs = col.stats.get("heartbeat")
        stop.set()
        await task
        return slots_only, logs

    slots_only, logs = asyncio.run(main())
    assert slots_only is None and abs(logs - time.time()) < 5
    col.c.close()


def test_slots_without_logs_are_a_stalled_feed_and_only_the_logs_count_as_work(tmp_path, monkeypatch):
    from hl_screener import pumpfun
    monkeypatch.setattr(pumpfun, "SILENT_S", 0.3)                     # 30 s, scaled down
    refused = json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "too many subscriptions"}})
    conns = [Talking([], SLOT, refused), Stream(SLOT, 10**6), Stream(LOGS, 12)]   # logs refused; slots and never a log;
    monkeypatch.setitem(sys.modules, "websockets",                                # then 0.6 s of logs, and the server closes
                        types.SimpleNamespace(connect=lambda *a, **k: conns.pop(0) if conns else Silent([])))
    col = Collector(tmp_path / "pump.db")
    gaps, drops = [], []
    col.check_gap = gaps.append
    col._dropped = lambda worked, on_fallback: drops.append((worked, col.stats["last_error"])) or 0.01

    async def main():
        stop = asyncio.Event()
        task = asyncio.create_task(col.run(stop))
        while len(drops) < 3:
            await asyncio.sleep(0.05)
        stop.set()
        await task

    t0 = time.time()
    asyncio.run(asyncio.wait_for(main(), 10))
    (no_logs, why0), (stalled, why1), (closed, why2) = drops[:3]
    assert no_logs == 0 and "ConnectionError: subscription 1 refused" in why0   # at once, not after SILENT_S
    assert stalled == 0 and why1.endswith("TimeoutError: 0.3 s without pump.fun's logs")   # the slots kept coming: stalled all the same
    assert 0.2 < closed < 5 and "sent 1002" in why2                   # the 0.6 s of logs count as work, then the server's close
    assert gaps == ["restart", "reconnect"]                           # the gap ends at the first logs, not at a connect
    assert time.time() - t0 < 5
    col.c.close()


def test_a_full_disk_drops_the_batch_but_never_stops_the_collector(tmp_path):
    col = Collector(tmp_path / "pump.db")

    class Full:                                                       # a connection whose disk is full
        def __init__(self, real):
            self.real = real

        def __getattr__(self, name):
            return getattr(self.real, name)

        def executemany(self, *args):
            raise sqlite3.OperationalError("database or disk is full")

    col.buf = [(1, 2, 3, 4, 1, 5, 6, 7, 8, 9)] * 3
    real, col.c = col.c, Full(col.c)
    col.flush()                                                       # 17 restarts in a row on 2026-09-25: never again
    assert col.buf == [] and col.stats["skipped_low_disk"] == 3
    real.close()


def test_settling_launches_leaves_the_write_lock_free_while_it_computes(tmp_path, monkeypatch):
    """settle_launches held the write lock from its first statement to its commit, through every launch's states: the
    feed's next write waited on it all along (sells up to 630 slots late, 2026-10-07..09)."""
    from test_pump import create_bytes, logs, trade_bytes

    from hl_screener import pumpfun
    db, then = tmp_path / "pump.db", int(time.time()) - 3600     # an hour ago: its window has closed
    col = Collector(db)
    col.on_logs(1000, logs(create_bytes(bytes([90]) * 32, bytes([91]) * 32, ts=then)))
    col.on_logs(1001, logs(trade_bytes(bytes([90]) * 32, bytes([92]) * 32, True, 10**9, 10**12, 31 * 10**9, 10**15, ts=then)))
    col.flush()
    col.c.execute("PRAGMA busy_timeout = 50")                         # a feed write that has to wait fails here instead
    real, fed = pumpfun.strategy_states, []

    def computing(*args, **kwargs):                                   # meanwhile the feed brings a new coin by a new wallet
        col.on_logs(2000, logs(create_bytes(bytes([93]) * 32, bytes([94]) * 32)))
        col.flush()
        fed.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(pumpfun, "strategy_states", computing)
    assert pumpfun.settle_launches(db, latency_slots=2, hold_s=300) == 1 and fed
    assert col.c.execute("SELECT COUNT(*) FROM mints").fetchone()[0] == 2
    col.c.close()


def test_a_batch_the_database_stays_locked_for_is_kept_and_a_long_wait_is_said(tmp_path, monkeypatch, caplog):
    import threading

    from test_pump import create_bytes, logs, trade_bytes

    from hl_screener import pumpfun
    monkeypatch.setattr(pumpfun, "DB_SLOW_S", 0.2)                    # 1 s, scaled down
    db, mint, maker = tmp_path / "pump.db", bytes([95]) * 32, bytes([96]) * 32
    col = Collector(db)
    caplog.set_level("WARNING", logger="hl_screener.pumpfun")
    col.on_logs(10, logs(create_bytes(mint, maker)))
    col.flush()
    other = sqlite3.connect(db, isolation_level=None, check_same_thread=False)   # the maintenance thread, mid-write,
    other.execute("BEGIN IMMEDIATE")
    col.c.execute("PRAGMA busy_timeout = 50")                         # past the feed's timeout (30 s in life)
    col.on_logs(11, logs(trade_bytes(mint, maker, True, 10**9, 10**12, 31 * 10**9, 10**15)))   # a wallet known: no write
    col.flush()
    assert len(col.buf) == 1 and col.stats["db_locked"] == 1          # not dropped as a full disk's batch is
    assert "skipped_low_disk" not in col.stats
    col.c.execute("PRAGMA busy_timeout = 30000")
    threading.Timer(0.3, other.commit).start()                        # its write ends 0.3 s later
    col.flush()                                                       # waited out, written, and said
    assert col.buf == [] and col.c.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
    assert "s on the database since its last write" in caplog.text and col.stats["db_waits"] == 1
    other.close()
    col.c.close()


def test_the_log_warns_hourly_before_the_disk_fills_and_errs_at_once_when_pruning_starts(tmp_path, monkeypatch, caplog):
    from hl_screener import pumpfun
    disk = {"disk_free_gb": 8.0, "disk_total_gb": 40.0}
    monkeypatch.setattr(pumpfun, "host_resources", lambda path: dict(disk))
    col = Collector(tmp_path / "pump.db")

    def flush(free):
        disk["disk_free_gb"], col.last_paper = free, 0.0             # the next flush reads the disk again
        col.flush()
        return [(r.levelname, r.getMessage()) for r in caplog.records if r.getMessage().startswith("disk ")]

    assert flush(8.0) == []                                           # room enough: quiet
    warned = [("WARNING", "disk 5.2 of 40 GB free: under 3 GB the collector keeps 36 h of trades, under 1 GB it stops storing them")]
    assert flush(5.2) == warned
    assert flush(5.1) == warned                                       # once an hour, not every 10 s
    assert [level for level, _ in flush(2.9)] == ["WARNING", "ERROR"]   # the prune to 36 h begins: said at once, louder
    assert len(flush(2.8)) == 2
    col.disk_alarm = (col.disk_alarm[0] - 3600, col.disk_alarm[1])    # an hour later, still short
    assert [level for level, _ in flush(2.7)] == ["WARNING", "ERROR", "ERROR"]
    col.c.close()


def test_a_cancelled_pool_feed_returns_only_once_its_sockets_are_closed(monkeypatch):
    from hl_screener.pumppools import PoolFeed
    events, opened = [], []

    def connect(*args, **kwargs):
        opened.append(kwargs)
        return Silent(events)

    monkeypatch.setitem(sys.modules, "websockets", types.SimpleNamespace(connect=connect))

    async def main():
        pf = PoolFeed("wss://public.example", lambda *a: None)
        pf.add("POOL")
        task = asyncio.create_task(pf.run(asyncio.Event()))
        await asyncio.sleep(0.1)                                      # connected and subscribed
        task.cancel()                                                 # what the collector does on its way out
        await asyncio.gather(task, return_exceptions=True)
        return list(events)                                           # before asyncio.run cancels whatever is left

    assert asyncio.run(main()) == ["sent", "closed"]                  # a close frame, not a socket cut by the kill
    assert opened[0]["close_timeout"] == 5                            # and a close that fits the 15 s grace period


def test_a_stop_ends_the_collector_within_seconds_with_its_feed_closed_properly(tmp_path, monkeypatch):
    events, gaps, opened = [], [], []
    conns = iter([OSError("HTTP 429"), Talking(events, LOGS)])       # refused once, then a feed that speaks once

    def connect(*args, **kwargs):
        opened.append(kwargs)
        c = next(conns)
        if isinstance(c, Exception):
            raise c
        return c

    monkeypatch.setitem(sys.modules, "websockets", types.SimpleNamespace(connect=connect))
    col = Collector(tmp_path / "pump.db")
    col._dropped = lambda lived, on_fallback: 0.01                    # reconnect at once
    col.check_gap = gaps.append

    async def main():
        stop = asyncio.Event()
        task = asyncio.create_task(col.run(stop))
        await asyncio.sleep(0.3)
        stop.set()                                                    # what SIGTERM does
        t0 = time.time()
        await asyncio.wait_for(task, timeout=5)
        return time.time() - t0

    assert asyncio.run(main()) < 2                                    # not the 60 s a redeploy used to wait for its kill
    assert events == ["sent", "sent", "closed"]                       # both subscriptions, then a proper close
    assert [k["close_timeout"] for k in opened] == [5, 5]             # within the 15 s grace period even if the server stalls
    assert gaps == ["restart", "reconnect"]                           # the copies are checked after each gap
    col.c.close()


def test_the_write_ahead_log_is_capped_and_folded(tmp_path):
    db = tmp_path / "pump.db"
    c = connect(db)
    assert c.execute("PRAGMA journal_size_limit").fetchone()[0] == WAL_LIMIT
    c.executemany("INSERT INTO meta (key, value) VALUES (?, ?)", [(str(i), "x" * 1000) for i in range(2000)])
    c.commit()
    assert checkpoint(db)[0] == 0                                     # not busy: folded in and the log cut back
    assert (tmp_path / "pump.db-wal").stat().st_size == 0
    c.close()


def test_a_golden_wallet_is_copied_again_at_every_stake(tmp_path):
    import time
    from hl_screener.pumpfun import SWEEP_STAKES, TX_COST_SOL, _pct, copy_trade, stake_sweep
    db, k, sol = tmp_path / "pump.db", 30 * 10**9 * 1_073_000_000 * 10**6, 10**9
    c = connect(db)
    c.executemany("INSERT INTO wallets (id, addr) VALUES (?, ?)", [(1, "MAKER"), (2, "G"), (3, "X")])
    c.execute("INSERT INTO mints (id, addr, slot, ts, creator) VALUES (1, 'M1', 100, ?, 1)", (int(time.time()) - 3600,))
    c.execute("INSERT INTO follow (wallet, added_at, golden_ever) VALUES ('G', 0, 1)")
    state = {"v": 30 * sol}

    def trade(slot: int, wallet: int, v: int) -> None:
        amount = abs(v - state["v"])
        c.execute("INSERT INTO trades VALUES (?, 0, 1, ?, ?, ?, ?, ?, ?, ?)",
                  (slot, wallet, int(v > state["v"]), amount, abs(k // state["v"] - k // v), amount // 80, v, k // v))
        state["v"] = v

    trade(100, 3, 35 * sol)
    trade(110, 2, 36 * sol)                                  # G buys ...
    trade(200, 3, 45 * sol)
    trade(300, 2, 44 * sol)                                  # ... and sells into the move
    c.commit()
    c.close()
    want = {s: copy_trade((36 * sol, k // (36 * sol)), (44 * sol, k // (44 * sol)), s, TX_COST_SOL, 1 / 80, 1 / 80) / s
            for s in SWEEP_STAKES}
    assert stake_sweep(db, latency_slots=2) == ["stake sweep G (1 copies on the stored trades): " + ", ".join(
        f"{s:g} SOL {_pct(want[s])} [n/a/{_pct(want[s], 0)}] won 100%" for s in SWEEP_STAKES)]
    old = {s: copy_trade((36 * sol, k // (36 * sol)), (44 * sol, k // (44 * sol)), s, 0.001505, 1 / 80, 1 / 80) / s
           for s in SWEEP_STAKES}                            # Sender Max's 0.001 SOL tip + 0.0005 priority, until 2026-10-10:
    assert old[0.25] > old[0.1]                              # the fixed cost weighed less on a bigger copy ...
    assert want[0.1] > want[0.25] > want[1.0]                # ... SWQOS-only's few cents do not: the copy's own move counts


def test_a_pool_is_followed_while_it_trades_or_holds_a_copy():
    from hl_screener.pumppools import PoolFeed
    pf, now = PoolFeed("wss://public.example", lambda *a: None, pinned=lambda: {"P_OPEN"}), 1_000_000.0
    pf.seen = {"P_NEW": now, "P_OLD": now - 7200, "P_OPEN": now - 7200, "P_MID": now - 60}
    assert pf.wanted(now) == ["P_OPEN", "P_NEW", "P_MID"]            # quiet for 2 h: dropped, unless a copy is open


def test_pools_fill_connections_and_the_least_useful_is_retired(monkeypatch):
    from hl_screener import pumppools
    monkeypatch.setattr(pumppools, "POOL_PER_CONN", 3)             # the public RPC's 100 attempts, scaled down
    monkeypatch.setattr(pumppools, "POOL_CONNS", 2)
    pf = pumppools.PoolFeed("wss://public.example", lambda *a: None)
    pf.place(list("abcde"), pumppools._Conn)
    assert [sorted(c.pools) for c in pf.conns] == [list("abc"), list("de")]
    pf.place(list("cdef"), pumppools._Conn)                         # a and b went quiet; f fits on the second
    pf.place(list("cdefg"), pumppools._Conn)                        # g: every attempt used, so the first connection,
    pf.place(list("cdefg"), pumppools._Conn)                        # with one pool still wanted, is retired for a new one
    assert [sorted(c.pools) for c in pf.conns] == [list("def"), list("cg")]
    assert pf.stats["pools_followed"] == 5 and all(c.attempts <= 3 for c in pf.conns)


def test_the_log_carries_the_forward_results_by_why_each_wallet_is_followed(tmp_path):
    db = tmp_path / "pump.db"
    c = connect(db)
    row = {"golden_ever": False, "sniper_now": False, "mature_ever": False, "wins": 0, "closed": 0, "roi": None, "own_roi": None}
    set_meta(c, "paper", {"stake_sol": 0.1, "wallets": [
        {**row, "wallet": "M1", "mature_ever": True, "copied": 4, "closed": 3, "total": -0.05, "wins": 1, "own_cost": 10.0,
         "own_pnl": 1.0, "roi": -0.125, "own_roi": 0.1, "invested": 0.4},
        {**row, "wallet": "M2", "mature_ever": True, "copied": 0, "total": 0.0, "own_cost": 0.0, "own_pnl": 0.0, "invested": 0.0},
        {**row, "wallet": "S1", "sniper_now": True, "copied": 10, "closed": 10, "total": 0.02, "wins": 5, "own_cost": 5.0,
         "own_pnl": -0.5, "roi": 0.02, "own_roi": -0.1, "invested": 1.0}]})
    c.commit()
    c.close()
    assert paper_lines(db) == [
        "paper bought past $100k: 2 wallets, 4 copies (3 closed), -0.050 SOL = -12.5% per copy, won 33%; the wallets themselves +10.0%",
        "paper snipers: 1 wallets, 10 copies (10 closed), +0.020 SOL = +2.0% per copy, won 50%; the wallets themselves -10.0%",
        "paper bought past $100k M1: 4 copies (3 closed), -0.050 SOL = -12.5% per copy, won 33%; itself +10.0%"]


def test_copies_of_two_sizes_count_by_the_sol_put_in_and_the_new_size_is_logged_alone(tmp_path):
    from hl_screener.pumpfun import PAPER_STAKE_SOL, PaperFollow
    db = tmp_path / "pump.db"
    c = connect(db)
    c.execute("INSERT INTO follow (wallet, added_at, golden_ever) VALUES ('G', 0, 1)")
    c.executemany("INSERT INTO pfills (wallet, mint, side, sol, pnl) VALUES ('G', ?, ?, ?, ?)", [
        ("A", "buy", 0.1, None), ("A", "sell", 0.12, 0.017),                   # a copy from before the switch
        ("B", "buy", PAPER_STAKE_SOL, None), ("B", "sell", 0.2, -0.053),      # two after it, one still open
        ("C", "buy", PAPER_STAKE_SOL, None)])
    pf = PaperFollow(c)
    w = pf.summary()["wallets"][0]
    assert abs(w["roi"] - (0.017 - 0.053) / (0.1 + 2 * PAPER_STAKE_SOL)) < 1e-12   # not over 3 copies at today's size
    set_meta(c, "paper", pf.summary())
    c.commit()
    c.close()
    assert paper_lines(db) == [
        "paper golden: 1 wallets, 3 copies (2 closed), -0.036 SOL = -6.0% per copy, won 50%; the wallets themselves n/a",
        "paper golden G at 0.25 SOL: 2 copies (1 closed), -0.053 SOL = -21.2% per closed copy, won 0%"]


def test_the_collector_logs_the_live_copies_and_reads_gaps_on_the_public_rpc_whatever_the_live_one(tmp_path, monkeypatch):
    """The live copies' trades are logged at INFO, which the container's WARNING default dropped; and the gap checks
    (two reads a second) stay off PUMP_LIVE_RPC, where a keyed free plan would run out of credits in days."""
    import logging

    from hl_screener import pumpfun
    seen = {}

    async def run(self, stop=None):
        seen["gap_rpc"] = self.rpc.url
        seen["live_info"] = logging.getLogger("hl_screener.pumplive").isEnabledFor(logging.INFO)

    monkeypatch.setattr(Collector, "run", run)
    monkeypatch.setenv("PUMP_LIVE", "off")
    monkeypatch.setenv("PUMP_LIVE_RPC", "https://keyed.example/?api-key=k")
    monkeypatch.delenv("PUMP_GAP_RPC", raising=False)
    levels = {n: logging.getLogger(n).level for n in ("hl_screener.pumpfun", "hl_screener.pumplive")}
    try:
        pumpfun.collect(tmp_path / "pump.db", pumpfun.PUBLIC_WS, 2, sniper_every_s=3600)
        assert seen == {"gap_rpc": pumpfun.PUBLIC_RPC, "live_info": True}
        monkeypatch.setenv("PUMP_GAP_RPC", "https://gaps.example")
        pumpfun.collect(tmp_path / "pump.db", pumpfun.PUBLIC_WS, 2, sniper_every_s=3600)
        assert seen["gap_rpc"] == "https://gaps.example"
    finally:
        for n, level in levels.items():
            logging.getLogger(n).setLevel(level)
