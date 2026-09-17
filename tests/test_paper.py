import asyncio

from hl_screener.paper import PaperTrader, Store, plan_follow, read_leaders, slippage_bps, status, walk_book
from hl_screener.config import Config


def test_plan_open_add_reduce_close_flip():
    # leader opens 10 long from flat, ratio 0.1 -> follower opens 1
    assert plan_follow(0, 10, 0, 0.1, 100) == [("open", 1.0)]
    # leader adds 5 (10 -> 15): follower adds 0.5
    assert plan_follow(10, 5, 1.0, 0.1, 100) == [("add", 0.5)]
    # leader closes half (15 -> 7.5): follower reduces half of what it holds, whatever its size
    acts = plan_follow(15, -7.5, 1.5, 0.1, 100)
    assert acts[0][0] == "reduce" and abs(acts[0][1] + 0.75) < 1e-12
    # leader goes flat: follower closes everything
    acts = plan_follow(7.5, -7.5, 0.75, 0.1, 100)
    assert acts[0][0] == "close" and abs(acts[0][1] + 0.75) < 1e-12
    # flip long 10 -> short 4: close, then open short 0.4
    acts = plan_follow(10, -14, 1.0, 0.1, 100)
    assert acts == [("close", -1.0), ("open", -0.4)]
    # follower flat while leader reduces: nothing to do (joined late)
    assert plan_follow(10, -5, 0.0, 0.1, 100) == []
    # cap: follower may not exceed cap_size coins
    assert plan_follow(0, 1000, 0, 0.1, 30) == [("open", 30.0)]
    assert plan_follow(0, 1000, 25, 0.1, 30) == [("add", 5.0)]


def test_walk_book_and_slippage():
    book = {"levels": [[{"px": "99", "sz": "1"}, {"px": "98", "sz": "5"}], [{"px": "101", "sz": "1"}, {"px": "102", "sz": "2"}]]}
    px, used, unfilled = walk_book(book, "B", 2.0)          # 1 @ 101 + 1 @ 102
    assert abs(px - 101.5) < 1e-9 and used == 2 and unfilled == 0
    px, used, unfilled = walk_book(book, "A", 0.5)          # 0.5 @ 99
    assert px == 99 and used == 1
    px, used, unfilled = walk_book(book, "B", 10.0)         # beyond the book: rest at last level, reported
    assert abs(px - (101 + 2 * 102 + 7 * 102) / 10) < 1e-9 and unfilled == 7
    assert abs(slippage_bps(100, 101, "B") - 100) < 1e-9
    assert abs(slippage_bps(100, 99, "A") - 100) < 1e-9
    assert slippage_bps(100, 100.5, "A") < 0                # sold above the leader: favourable


class FakeLiveAPI:
    def __init__(self):
        self.book = {"levels": [[{"px": "99.9", "sz": "100"}], [{"px": "100.1", "sz": "100"}]]}

    def l2_book(self, coin):
        return self.book

    def all_mids(self):
        return {"ETH": 100.0}

    def clearinghouse_state(self, user):
        return {"marginSummary": {"accountValue": "10000"}}

    def live_funding_rates(self):
        return {"ETH": 0.0001}

    def fills_since(self, user, start_ms):
        return []


def test_trader_follows_a_round_trip(tmp_path):
    cfg = Config()
    store = Store(tmp_path / "p.db")
    addr = "0x" + "a" * 40
    trader = PaperTrader(FakeLiveAPI(), cfg, [{"address": addr, "name": "t"}], store, equity_base=1000.0, max_leverage=10.0)
    asyncio.run(trader.refresh_mids())
    asyncio.run(trader.refresh_leader_equity())
    assert trader.leaders[addr].equity == 10000
    # leader buys 10 ETH at 100 from flat: ratio 0.1 -> follower buys 1 ETH at the ask 100.1
    asyncio.run(trader.on_fill(addr, {"tid": 1, "time": 1_000, "coin": "ETH", "px": "100", "sz": "10", "side": "B", "startPosition": "0"}, False))
    p = trader.positions[addr]["ETH"]
    assert abs(p.size - 1.0) < 1e-9 and abs(p.entry_px - 100.1) < 1e-9
    fee_open = 1.0 * 100.1 * cfg.taker_fee_bps / 1e4
    assert abs(trader.totals[addr]["fees"] - fee_open) < 1e-9
    # funding for an hour: long pays 1 bp of notional at mark 100
    asyncio.run(trader.apply_funding())
    assert abs(trader.totals[addr]["funding"] + 0.0001 * 100.0) < 1e-9
    # leader sells all 10 at 110: follower sells 1 at the bid 109.9
    trader.api.book = {"levels": [[{"px": "109.9", "sz": "100"}], [{"px": "110.1", "sz": "100"}]]}
    asyncio.run(trader.on_fill(addr, {"tid": 2, "time": 2_000, "coin": "ETH", "px": "110", "sz": "10", "side": "A", "startPosition": "10"}, False))
    assert "ETH" not in trader.positions[addr]
    assert abs(trader.totals[addr]["realized"] - (109.9 - 100.1)) < 1e-9
    # duplicate fill is ignored
    asyncio.run(trader.on_fill(addr, {"tid": 2, "time": 2_000, "coin": "ETH", "px": "110", "sz": "10", "side": "A", "startPosition": "10"}, False))
    assert store.one("SELECT COUNT(*) n FROM paper_fills")["n"] == 2
    trader.snapshot()
    s = status(tmp_path / "p.db", {"ETH": 100.0})
    L = s["leaders"][0]
    assert L["n_paper_fills"] == 2 and abs(L["equity"] - (1000 + (109.9 - 100.1) - trader.totals[addr]["fees"] - 0.01)) < 1e-6
    assert s["fills"][0]["slippage_bps"] > 0
    store.close()


def test_read_leaders_from_shortlist_and_plain_list(tmp_path):
    csv_path = tmp_path / "shortlist.csv"
    csv_path.write_text("address,display_name,is_f_avg_penalty_bps,is_f_roi,oos_f_roi\n0x" + "b" * 40 + ",bob,12.5,0.2,-0.1\n", encoding="utf-8")
    L = read_leaders(csv_path)
    assert L[0]["address"] == "0x" + "b" * 40 and L[0]["model_penalty_bps"] == 12.5 and L[0]["name"] == "bob"
    txt = tmp_path / "addr.txt"
    txt.write_text("0x" + "C" * 40 + "\n0x" + "c" * 40 + "\n", encoding="utf-8")
    assert [x["address"] for x in read_leaders(txt)] == ["0x" + "c" * 40]
