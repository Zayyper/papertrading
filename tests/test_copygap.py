import os
import uuid

import pytest

from hl_screener.copygap import (CopyCfg, Market, copy_gap, evaluate_holdout, leader_orders, max_drawdown, random_test,
                                 verdict, vol_matched)

BIDS_4S, ASKS_4S = [[100.5, 0.2], [100.4, 1.0]], [[100.7, 0.3], [100.8, 1.0]]    # mid 100.6
BIDS_13S, ASKS_13S = [[109.0, 1.0]], [[109.2, 1.0]]                              # mid 109.1


def fill(t, side, px, sz, start, fee, tid=None):
    return {"tid": tid or t, "time_ms": t, "coin": "BTC", "side": side, "px": px, "sz": sz, "start_pos": start, "fee": fee}


def round_trip():
    """A $10k wallet buys 5 BTC at 100 and sells them at 110 (3 bps fees); the $1k copier mirrors a tenth, 3 s later."""
    return [fill(1_000, "B", 100.0, 5.0, 0.0, 0.15), fill(10_000, "A", 110.0, 5.0, 5.0, 0.165)]


def test_copy_gap_adds_up_to_latency_slippage_fees_and_funding_by_hand():
    market = Market(books={"BTC": [(4_000, BIDS_4S, ASKS_4S), (13_000, BIDS_13S, ASKS_13S)]},
                    mids={"BTC": [(0, 100.0), (12_000, 110.0)]}, funding={"BTC": [(2_000, 0.0001), (11_000, 0.0003)]})
    r = copy_gap(round_trip(), [(0, 10_000.0)], market, CopyCfg(latency_ms=3_000), 0, 20_000)
    buy, sell = r["rows"]
    assert (buy["action"], buy["size"], buy["book_ms"], buy["levels"]) == ("open", 0.5, 4_000, 2)
    assert buy["copy_px"] == pytest.approx(100.74)            # 0.3 at 100.7 + 0.2 at 100.8
    assert sell["copy_px"] == 109.0 and sell["mid"] == pytest.approx(109.1)
    # leader leg: +0.5 x (110 - 100) = 5, minus its own 3 bps (0.015 + 0.0165), minus funding at 2 s: 0.5 x 100 x 0.0001
    assert r["leader"]["pnl"] == pytest.approx(5 - 0.0315 - 0.005)
    # copier: bought at 100.74, sold at 109.0, 4.5 bps each way, and it held over the 11 s funding instead: 0.5 x 100 x 0.0003
    assert r["copier"]["pnl"] == pytest.approx(0.5 * (109.0 - 100.74) - 0.00045 * 0.5 * (100.74 + 109.0) - 0.015)
    g = r["gap"]
    assert g["latency"] == pytest.approx(0.5 * (100.6 - 100) - 0.5 * (109.1 - 110))      # 0.30 + 0.45: the price ran away
    assert g["slippage"] == pytest.approx(0.5 * (100.74 - 100.6) - 0.5 * (109.0 - 109.1))  # 0.07 + 0.05: the book
    assert g["fees"] == pytest.approx(0.00045 * 0.5 * (100.74 + 109.0) - 0.0315)
    assert g["funding"] == pytest.approx(-0.005 + 0.015)
    assert g["usd"] == pytest.approx(g["latency"] + g["slippage"] + g["fees"] + g["funding"])
    assert r["copier"]["ret"] == pytest.approx(r["copier"]["pnl"] / 1000) and r["coverage"] == 1.0


def test_a_missing_book_drops_that_order_from_both_legs_and_from_coverage():
    market = Market(books={"BTC": [(4_000, BIDS_4S, ASKS_4S), (15_000, BIDS_13S, ASKS_13S)]},   # 2 s late: past the 1 s tolerance
                    mids={"BTC": [(0, 100.0), (12_000, 110.0)]})
    r = copy_gap(round_trip(), [(0, 10_000.0)], market, CopyCfg(latency_ms=3_000, funding=False), 0, 20_000)
    assert (r["copied"], r["missing_book"], r["coverage"]) == (1, 1, 0.5)
    assert r["open_notional"] == pytest.approx(0.5 * 110.0)   # both legs still hold, marked at the last mid
    assert r["leader"]["pnl"] == pytest.approx(0.5 * (110 - 100) - 0.015)
    assert r["copier"]["pnl"] == pytest.approx(0.5 * (110 - 100.74) - 0.00045 * 0.5 * 100.74)


def test_copies_under_the_exchange_minimum_are_not_made():
    market = Market(books={"BTC": [(4_000, BIDS_4S, ASKS_4S), (13_000, BIDS_13S, ASKS_13S)]})
    r = copy_gap(round_trip(), [(0, 1_000_000.0)], market, CopyCfg(), 0, 20_000)      # a 1/1000 copy: $0.50
    assert r["too_small"] == 1 and r["actions"] == 0 and r["copier"]["pnl"] == 0


def test_the_pieces_of_one_order_are_copied_as_one():
    fills = [fill(1_000, "B", 100.0, 2.0, 0.0, 0.06, tid=1), fill(1_000, "B", 101.0, 3.0, 2.0, 0.09, tid=2), fill(2_000, "A", 99.0, 1.0, 5.0, 0.03)]
    o = leader_orders(fills)
    assert len(o) == 2 and o[0]["sz"] == 5.0 and o[0]["px"] == pytest.approx(100.6) and o[0]["start_pos"] == 0.0


def test_the_random_test_counts_how_often_luck_did_as_well():
    assert random_test(1.0, [0.0, 0.0, 0.0, 1.0], 1, draws=4000)["p"] == pytest.approx(0.25, abs=0.03)
    best = random_test(9.0, [0.0, 0.1, 0.2], 2, draws=1000)
    assert best["p"] == pytest.approx(1 / 1001) and best["percentile"] == 1.0
    assert random_test(0.5, [0.1], 3)["p"] is None                                    # not enough random wallets to draw from


def test_the_risk_matched_benchmark_is_btc_at_the_portfolios_volatility():
    btc = [100.0, 110.0, 99.0, 108.9]                  # +10%, -10%, +10%
    port = [1000.0, 1200.0, 960.0, 1152.0]             # twice as wild
    vm = vol_matched(port, btc)
    assert vm["k"] == pytest.approx(2.0) and vm["ret"] == pytest.approx(2 * 0.089)
    assert max_drawdown([100.0, 120.0, 90.0, 130.0]) == pytest.approx(0.25)


def test_the_verdict_needs_every_rule():
    rules = {"min_return": 0.0, "beat_btc_hold": True, "beat_risk_matched": True, "beat_top_n": True, "max_random_p": 0.05,
             "max_drawdown": 0.3, "min_actions": 30, "min_coverage": 0.9}
    good = {"shortlist": 0.05, "btc_hold": 0.01, "risk_matched": 0.02, "top": -0.01, "random_p": 0.01, "max_drawdown": 0.1,
            "actions": 40, "coverage": 0.95}
    assert verdict(good, rules)["pass"]
    assert not verdict({**good, "random_p": 0.2}, rules)["pass"]                     # as good as luck is not good enough
    assert not verdict({**good, "btc_hold": None}, rules)["pass"]                    # a missing benchmark fails, never passes


def test_the_collector_books_once_per_coin_second_and_only_what_is_still_ahead():
    from hl_screener.tape import Tape, now_ms

    class Conn:
        def execute(self, *a):
            return [("0xabc", "shortlist")]

    tape = Tape(Conn(), api=None, plan={"copy": {"delays_s": [3, 30, 300, 3600]}})
    t = 1_700_000_000_000
    tape.on_trades([{"coin": "BTC", "time": t, "users": ["0xABC", "0xother"]}, {"coin": "BTC", "time": t + 400, "users": ["0xother", "0xabc"]},
                    {"coin": "ETH", "time": t, "users": ["0xnobody", "0xother"]}])
    assert sorted(d for _, d, _ in tape.heap) == [3_000, 30_000, 300_000, 3_600_000]   # both BTC trades share each book; ETH is no one's
    tape.heap.clear()
    tape.keys.clear()
    tape.loop = type("Loop", (), {"call_soon_threadsafe": staticmethod(lambda f, *a: f(*a))})()
    tape.schedule_later([("SOL", now_ms() - 60_000)])                                     # a fill learned of a minute late
    assert sorted(d for _, d, _ in tape.heap) == [300_000, 3_600_000]                     # its 3 s and 30 s books are gone for good


@pytest.fixture
def pg():
    url = os.environ.get("HL_TEST_DATABASE_URL")
    if not url:
        pytest.skip("set HL_TEST_DATABASE_URL to run the Postgres tests")
    import psycopg
    from hl_screener.tape import connect
    schema = "t_" + uuid.uuid4().hex[:12]
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(f"CREATE SCHEMA {schema}")
    conn = connect(url + ("&" if "?" in url else "?") + f"options=-csearch_path%3D{schema}")
    yield conn
    conn.close()
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(f"DROP SCHEMA {schema} CASCADE")


def test_the_plan_freezes_once_and_the_holdout_is_judged_once(pg, tmp_path):
    from hl_screener.tape import day_ms, freeze_plan, get_meta
    plan = tmp_path / "copytest.toml"
    text = open(os.path.join(os.path.dirname(__file__), "..", "copytest.toml"), encoding="utf-8").read()
    plan.write_text(text, encoding="utf-8")
    first = freeze_plan(pg, plan, now=day_ms("2026-09-24"))
    plan.write_text(text.replace("min_return = 0.0", "min_return = -1.0"), encoding="utf-8")   # loosen a rule afterwards
    assert freeze_plan(pg, plan, now=day_ms("2026-09-25"))["sha256"] == first["sha256"]       # ignored: the frozen copy stands
    assert get_meta(pg, "plan_mismatch") is not None
    late = text.replace('id = "h1"', 'id = "h2"')
    plan.write_text(late, encoding="utf-8")
    assert freeze_plan(pg, plan, now=day_ms("2026-10-09"))["id"] == "h1"                     # a new id after the start: refused
    assert evaluate_holdout(pg, now=day_ms("2026-10-20"))["status"] == "waiting"             # no peeking before the end
    done = evaluate_holdout(pg, now=day_ms("2026-11-01"))
    assert done["status"] == "evaluated" and done["fresh"] and not done["verdict"]["pass"]   # no data here: it fails
    again = evaluate_holdout(pg, now=day_ms("2026-12-01"))
    assert not again["fresh"] and again["evaluated_at"] == done["evaluated_at"]              # the stored result, never recomputed
