"""checks/live_monitor.py: real transactions read and printed as the live monitor does, its book of open copies, P&L
and failures, and its loop against an RPC that answers late or not at all. The fixture holds getTransaction answers of
2026-10-07, unmodified: 8zkgFG's PumpSwap buy and sell of one coin, a curve buy through a router (a v1 transaction),
a failed curve sell, and a sell into a pool quoted in another token than SOL."""
import importlib.util
import json
from pathlib import Path

import pytest

from hl_screener.pumptx import RpcError

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("live_monitor", ROOT / "checks" / "live_monitor.py")
lm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lm)
TXS = json.loads((ROOT / "tests" / "fixtures" / "live_monitor_txs.json").read_text())
LEADER = "8zkgFGVZrDLieViwqiXFCydSX6WL5hsxmUu55yBdsNsZ"
COIN = "FvfX6xmM8UPmbmQJaFpDfuz5kqnbe2pB75c4xdKLpump"
T_BUY, T_SELL = 1791389614, 1791389757


def read(name: str) -> dict:
    return lm.read_tx(TXS[name]["tx"], TXS[name]["wallet"])


def test_a_real_pumpswap_buy_reads_as_its_trade_event_fee_tip_and_wallet_change():
    d = read("pool_buy")
    assert (d["kind"], d["side"], d["mint"], d["t"]) == ("BUY", "buy", COIN, T_BUY)
    assert d["sol"] == pytest.approx((2_251_170_264 + 27_014_045) / 1e9)      # the event's SOL in, plus its fees
    assert d["tok"] == d["left"] == 3_052_981_412_901
    assert d["net"] == pytest.approx(-2.330146901)                            # the wallet's own balance change
    assert d["paid"] == pytest.approx((130_000 + 301_566) / 1e9)              # network fee + tip to a Helius tip account
    assert lm.ours_line(d) == ("10-07 16:13:34 OURS   BUY    FvfX6xmM8UPmbmQJaFpDfuz5kqnbe2pB75c4xdKLpump 2.27818 SOL"
                               "     3,052,981 tok  fee+tip 0.000432  wallet -2.33015 | 5K1Px8FtqsTN95bK")
    assert lm.leader_line("8zkgFG", d) == "10-07 16:13:34 8zkgFG BUY    FvfX6xmM8UPmbmQJaFpDfuz5kqnbe2pB75c4xdKLpump 2.2782 SOL | 5K1Px8FtqsTN95bK"


def test_a_real_pumpswap_sell_closes_the_copy_with_the_wallets_real_pnl():
    book, buy, sell = lm.Book(), read("pool_buy"), read("pool_sell")
    assert book.ours(buy) == " | open 1"
    assert (sell["kind"], sell["mint"], sell["left"]) == ("SELL", COIN, 0)
    assert sell["sol"] == pytest.approx((3_104_833_495 - 37_258_003) / 1e9)    # the event's SOL out, less its fees
    note = book.ours(sell)
    assert lm.ours_line(sell, note) == (
        "10-07 16:15:57 OURS   SELL   FvfX6xmM8UPmbmQJaFpDfuz5kqnbe2pB75c4xdKLpump 3.06758 SOL     3,052,981 tok  fee+tip 0.000431"
        "  wallet +3.11868 | pnl +0.7885 SOL (+33.8%) held 143 s | 5q28QRCuR3JRMk7t")
    assert book.pos == {}
    assert book.closed == [(COIN, pytest.approx(3.118675798 - 2.330146901), pytest.approx(2.330146901))]


def test_a_real_curve_buy_through_a_router_in_a_v1_transaction():
    d = read("curve_buy")
    assert TXS["curve_buy"]["tx"]["version"] == 1
    assert (d["kind"], d["mint"], d["tok"]) == ("BUY", "5LFtCCUw2gPWj7w1BsnJKBYTyTb3ujoA6mqMZ3eq6ysQ", 1_437_758_306_576)
    assert d["sol"] == pytest.approx((250_476_759 + 3_130_961) / 1e9)
    assert d["paid"] == pytest.approx(105_000 / 1e9)        # its tip went to an account that is not Helius Sender's
    assert d["net"] == pytest.approx(-0.257888254)


def test_a_real_failed_sell_names_its_coin_its_error_and_the_sol_it_burned():
    d, book = read("curve_failed"), lm.Book()
    assert (d["kind"], d["side"], d["mint"]) == ("FAILED", "sell", "AvvCHqu34d7BSG2QhbKQTSYPo5WxDbLj3hqgjb6Bpump")
    other = TXS["curve_failed"]["tx"]["transaction"]["message"]["accountKeys"][1]
    assert lm.read_tx(TXS["curve_failed"]["tx"], other)["kind"] == "OTHER"   # not its payer: not its failure
    assert book.ours(d) == ""
    assert (book.failed, book.burned) == (1, pytest.approx(14_900 / 1e9))
    assert lm.ours_line(d) == ("10-07 16:22:38 OURS   FAILED AvvCHqu34d7BSG2QhbKQTSYPo5WxDbLj3hqgjb6Bpump sell fee+tip 0.000015"
                               "  wallet -0.000015 | AccountNotInitialized (associated_user) | 34UPeeKQKeAVuj9o")


def test_a_coin_not_quoted_in_sol_is_flagged_not_priced_in_sol():
    d = read("curve_not_sol")                       # 8zkgFG selling into a PumpSwap pool quoted in another token
    assert (d["kind"], d["mint"], d["sol_quoted"], d["left"]) == ("SELL", "DcqjTXZeE2nY6s2jXuQaeR1FEW4ZDziddhijxGPnmPsE", False, 0)
    assert d["net"] == pytest.approx(-0.000430654)   # no SOL came back: only the fee and tip went out
    assert lm.leader_line("8zkgFG", d) == ("10-07 16:19:02 8zkgFG SELL   DcqjTXZeE2nY6s2jXuQaeR1FEW4ZDziddhijxGPnmPsE"
                                           " not SOL-quoted: never copied | 38xc6KJVX5e5rtX2")
    assert "(not SOL-quoted)" in lm.ours_line(d) and read("pool_buy")["sol_quoted"] and read("curve_buy")["sol_quoted"]


def test_a_leader_selling_a_coin_we_hold_is_called_out_until_our_sell_lands():
    book = lm.Book()
    book.ours({"kind": "BUY", "mint": COIN, "tok": 10**9, "left": 10**9, "net": -0.2541, "t": T_BUY + 2})
    sell = read("pool_sell")                                    # the leader's real sell of the same coin
    assert lm.leader_line("8zkgFG", sell, book.leader("8zkgFG", sell)) == (
        "10-07 16:15:57 8zkgFG SELL   FvfX6xmM8UPmbmQJaFpDfuz5kqnbe2pB75c4xdKLpump 3.0676 SOL | WE HOLD IT | 5q28QRCuR3JRMk7t")
    assert book.alerts(T_SELL + 29) == []
    assert book.alerts(T_SELL + 31) == [f"10-07 16:16:28 ALERT  leader 8zkgFG sold {COIN}, we still hold it after 31 s (cost 0.2541 SOL)"]
    assert book.alerts(T_SELL + 60) == []                       # said once per step of ALERT_AFTER_S
    assert book.leader("8zkgFG", {"kind": "SELL", "mint": COIN, "t": T_SELL + 90}) == " | WE HOLD IT"   # its second sell
    assert [a[15:] for a in book.alerts(T_SELL + 121)] == [f"ALERT  leader 8zkgFG sold {COIN}, we still hold it after 121 s (cost 0.2541 SOL)"]
    note = book.ours({"kind": "SELL", "mint": COIN, "tok": 10**9, "left": 0, "net": 0.3, "t": T_SELL + 125})
    assert note == " | pnl +0.0459 SOL (+18.1%) held 266 s, 125 s after 8zkgFG sold"
    assert book.alerts(T_SELL + 700) == [] and book.pos == {}
    assert "open 0 | sold 1 (1 up), realized +0.0459 SOL (last: FvfX6xmM +0.0459) | failed 0" in book.summary(T_SELL + 700)


def test_a_partial_sell_realizes_its_share_and_a_coin_bought_before_the_start_has_no_pnl():
    book = lm.Book()
    book.ours({"kind": "BUY", "mint": "M", "tok": 1000, "left": 1000, "net": -0.2, "t": 0})
    assert book.ours({"kind": "SELL", "mint": "M", "tok": 250, "left": 750, "net": 0.1, "t": 10}).startswith(" | pnl +0.0500 SOL")
    assert book.pos["M"]["tok"] == 750 and book.pos["M"]["cost"] == pytest.approx(0.15)
    book.ours({"kind": "SELL", "mint": "M", "tok": 750, "left": 0, "net": 0.1, "t": 20})
    assert book.pos == {} and sum(x for _, x, _ in book.closed) == pytest.approx(0.0)
    assert "no P&L" in book.ours({"kind": "SELL", "mint": "N", "tok": 5, "left": 0, "net": 0.1, "t": 30})


class FakeRpc:
    """Serves the fixture: signatures per address, transactions (None the first time when asked to), a balance."""

    def __init__(self, sigs: dict, late: set = frozenset(), down: bool = False):
        self.sigs, self.late, self.down, self.calls = sigs, set(late), down, []

    def call(self, method, params):
        self.calls.append((method, params))
        if self.down:
            raise RpcError(f"{method}: HTTP 429")
        if method == "getSignaturesForAddress":
            if params[1].get("limit") == 1:
                return []                                       # no history: everything later is new
            return [s for s in self.sigs.pop(params[0], [])]
        if method == "getTransaction":
            if params[0] in self.late:
                self.late.discard(params[0])
                return None
            return next(v["tx"] for v in TXS.values() if v["sig"] == params[0])
        return {"value": 1_500_000_000}


class LaggingRpc(FakeRpc):
    """A public RPC node that does not know the cursor yet: asked for what came after it, it answers 'not found'."""

    def call(self, method, params):
        if method == "getSignaturesForAddress" and "until" in params[1]:
            self.calls.append((method, params))
            raise RpcError(f"getSignaturesForAddress: {{'code': -32020, 'message': 'Transaction {params[1]['until']} not found'}}")
        return super().call(method, params)


def test_a_node_that_does_not_know_the_cursor_yet_is_answered_from_the_newest_page(monkeypatch):
    monkeypatch.setattr(lm.time, "sleep", lambda s: None)
    newest = [{"signature": "C", "slot": 30}, {"signature": "B", "slot": 20}, {"signature": "A", "slot": 10}]
    assert [s["signature"] for s in lm.Chain("", LaggingRpc({LEADER: list(newest)})).signatures(LEADER, until="B", until_slot=20)] == ["C"]
    with pytest.raises(RuntimeError, match="not found"):                 # no slot to cut at: the error stands
        lm.Chain("", LaggingRpc({LEADER: list(newest)})).signatures(LEADER, until="B")


def sig_of(name: str) -> dict:
    tx = TXS[name]["tx"]
    return {"signature": TXS[name]["sig"], "slot": tx["slot"], "blockTime": tx["blockTime"], "err": tx["meta"]["err"]}


def test_the_loop_prints_every_new_transaction_in_slot_order_and_waits_for_a_late_one(monkeypatch):
    monkeypatch.setattr(lm.time, "sleep", lambda s: None)
    router = TXS["curve_buy"]["wallet"]
    rpc = FakeRpc({LEADER: [sig_of("pool_sell"), sig_of("pool_buy")], router: [sig_of("curve_buy")]},
                  late={TXS["curve_buy"]["sig"]})
    lines: list[str] = []
    w = lm.Watch(lm.Chain("", rpc), LEADER, [router], out=lines.append)  # 8zkgFG as if it were ours
    w.begin()
    w.round()
    assert [ln[15:29] for ln in lines] == ["OURS   BUY    ", "OURS   SELL   "]
    assert "pnl +0.7885 SOL" in lines[1]
    w.round()                                                   # the late transaction, asked again
    assert lines[2][15:29] == "9MoGp9 BUY    "
    assert w.cursors[LEADER] == TXS["pool_sell"]["sig"]
    assert ("getSignaturesForAddress", [LEADER, {"limit": 1000, "commitment": "confirmed", "until": TXS["pool_sell"]["sig"]}]) in rpc.calls
    assert "balance 1.5000 SOL (+0.0000 since start) | open 0 | sold 1 (1 up), realized +0.7885 SOL" in w.status()


def test_a_rate_limited_rpc_is_waited_out_and_never_stops_the_watch(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(lm.time, "sleep", slept.append)
    rpc = FakeRpc({}, down=True)
    with pytest.raises(RuntimeError, match="getBalance failed 4 times: RpcError: getBalance: HTTP 429"):
        lm.Chain("", rpc).ask("getBalance", ["x"])
    assert [s for s in slept if s >= 1] == list(lm.BACKOFF_S)
    lines: list[str] = []
    w = lm.Watch(lm.Chain("", rpc), LEADER, [], out=lines.append)
    w.begin()
    w.round()
    assert len(lines) == 3 and all(" WARN " in ln for ln in lines)   # start, retried start, balance: no exception
    assert w.status().split(" STATUS ")[1].startswith("balance ? | open 0")
