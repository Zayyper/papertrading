import sqlite3

from hl_screener.pumpfun import FALLBACK_PER_DAY, WAL_LIMIT, Collector, checkpoint, connect, paper_lines, set_meta


def test_a_feed_that_keeps_dropping_waits_longer_and_borrows_the_fallback_twice_a_day(tmp_path):
    col = Collector(tmp_path / "pump.db", fallback_url="wss://fallback.example")
    waits = [col._dropped(30, on_fallback=False) for _ in range(5)]   # connects, then dies within the minute, again and again
    assert waits == [2, 4, 8, 16, 32]                                 # not back in 2 s every time: that is what gets throttled
    assert col.fallback_used == FALLBACK_PER_DAY == 2                 # the fallback, but only twice a day
    assert col._dropped(600, on_fallback=False) == 2 and col.fails == 1   # a connection that held is a fresh start
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
