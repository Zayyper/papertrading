import sqlite3

from hl_screener.pumpfun import FALLBACK_PER_DAY, WAL_LIMIT, Collector, checkpoint, connect, paper_lines, set_meta


def test_a_feed_that_keeps_dropping_waits_longer_and_borrows_the_fallback_twice_a_day(tmp_path):
    col = Collector(tmp_path / "pump.db", fallback_url="wss://fallback.example")
    waits = [col._dropped(30, on_fallback=False) for _ in range(5)]   # connects, then dies within the minute, again and again
    assert waits == [2, 4, 8, 16, 32]                                 # not back in 2 s every time: that is what gets throttled
    assert col.fallback_used == FALLBACK_PER_DAY == 2                 # the fallback, but only twice a day
    assert col._dropped(600, on_fallback=False) == 2 and col.fails == 1   # a connection that held is a fresh start
    col.fallback_until = 1e12
    col._dropped(0, on_fallback=True)                                  # the fallback turns us away at the door ...
    assert col.fallback_until == 0.0                                   # ... so its window ends and the main feed is tried
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
    assert want[0.25] > want[0.1]                            # the fixed cost weighs less on a bigger copy ...
    assert want[1.0] < want[0.25]                            # ... until the copy moves the curve itself


def test_the_log_carries_the_forward_results_by_why_each_wallet_is_followed(tmp_path):
    db = tmp_path / "pump.db"
    c = connect(db)
    row = {"golden_ever": False, "sniper_now": False, "mature_ever": False, "wins": 0, "closed": 0, "roi": None, "own_roi": None}
    set_meta(c, "paper", {"stake_sol": 0.1, "wallets": [
        {**row, "wallet": "M1", "mature_ever": True, "copied": 4, "closed": 3, "total": -0.05, "wins": 1, "own_cost": 10.0,
         "own_pnl": 1.0, "roi": -0.125, "own_roi": 0.1},
        {**row, "wallet": "M2", "mature_ever": True, "copied": 0, "total": 0.0, "own_cost": 0.0, "own_pnl": 0.0},
        {**row, "wallet": "S1", "sniper_now": True, "copied": 10, "closed": 10, "total": 0.02, "wins": 5, "own_cost": 5.0,
         "own_pnl": -0.5, "roi": 0.02, "own_roi": -0.1}]})
    c.commit()
    c.close()
    assert paper_lines(db) == [
        "paper bought past $100k: 2 wallets, 4 copies (3 closed), -0.050 SOL = -12.5% per copy, won 33%; the wallets themselves +10.0%",
        "paper snipers: 1 wallets, 10 copies (10 closed), +0.020 SOL = +2.0% per copy, won 50%; the wallets themselves -10.0%",
        "paper bought past $100k M1: 4 copies (3 closed), -0.050 SOL = -12.5% per copy, won 33%; itself +10.0%"]
