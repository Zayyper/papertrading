import asyncio
import dataclasses
import json
import time
import types
from concurrent.futures import Future

import pytest

pytest.importorskip("solders")

from solders.hash import Hash  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from test_pump import Curve, b58decode, create_bytes, logs, trade_bytes  # noqa: E402

from hl_screener.pumpfun import FEE, GAP_LOOK_BACK_S, TX_COST_SOL, Collector, b58  # noqa: E402
from hl_screener.pumplive import MAX_AGE_S, MAX_HOLD_S, SELL_TRIES, SLOT_S, LiveCfg, LiveFollow, live_lines, load_keypair  # noqa: E402
from hl_screener.pumptx import AMM_GLOBAL_CONFIG, PUMP_GLOBAL, PUMP_PROGRAM, TOKEN_2022_PROGRAM, Refused, RpcError, sol_for  # noqa: E402

G, X, MINT, MINT2, MINT3 = bytes([80]) * 32, bytes([81]) * 32, bytes([9]) * 32, bytes([10]) * 32, bytes([11]) * 32
CREATOR, FEE_TO = bytes([7]) * 32, bytes([6]) * 32
T22 = b58decode(TOKEN_2022_PROGRAM)


class Now:
    """Runs each job at once; the executor picks its result up on the next tick, as it would a worker's."""
    def submit(self, fn, *args):
        f = Future()
        try:
            f.set_result(fn(*args))
        except Exception as e:  # noqa: BLE001
            f.set_exception(e)
        return f


class Chain:
    """The RPC, faked: simulations answer with a trade event, sends are kept here, nothing leaves the test."""
    def __init__(self):
        self.sims, self.sent, self.txs = [], [], {}
        self.bal, self.sim_err, self.sim_logs, self.fail_send = 1.0, None, [], False
        self.land_err, self.send_raises = None, False

    def account(self, addr):
        if addr in (PUMP_GLOBAL, AMM_GLOBAL_CONFIG):
            return PUMP_PROGRAM, bytes(range(256)) * 5                  # any 32 bytes make a recipient
        curve = bytearray(151)                                          # a bonding curve still trading, or a mint
        curve[8:16], curve[16:24], curve[49:81] = (10**15).to_bytes(8, "little"), (40 * 10**9).to_bytes(8, "little"), CREATOR
        return TOKEN_2022_PROGRAM, bytes(curve)

    def simulate(self, tx):
        self.sims.append(tx)
        return {"err": self.sim_err, "logs": self.sim_logs, "unitsConsumed": 90_000, "slot": 105}

    def send(self, tx):
        if self.fail_send:
            raise Refused("sendTransaction: HTTP 429")                  # every endpoint said no: nothing went out
        self.sent.append(tx)
        if self.land_err is not None:                                   # it lands, and fails there
            self.txs[str(tx.signatures[0])] = {"meta": {"err": self.land_err, "logMessages": []}}
        if self.send_raises:
            raise RpcError("https://sender.helius-rpc.com/fast: ReadTimeout")   # out, but its answer never came
        return str(tx.signatures[0])

    def blockhash(self, max_age_s=20.0):
        return Hash.new_unique()

    def balance(self, addr):
        return int(self.bal * 1e9)

    def call(self, method, params, url=None):
        assert method == "getTransaction"
        return self.txs.get(params[0])


def setup(tmp_path, mode, keypair=None, **cfg):
    col = Collector(tmp_path / "pump.db")
    col.c.execute("INSERT INTO follow(wallet, added_at, golden_now, golden_ever) VALUES (?, 0, 1, 1)", (b58(G),))
    chain = Chain()
    col.live = LiveFollow(col.c, LiveCfg(mode=mode, **cfg), rpc=chain, workers=Now(), keypair=keypair)
    col.flush()                                         # whom to follow; live: the balance is asked for ...
    col.flush()                                         # ... and read
    col.on_logs(10, logs(create_bytes(MINT, X, token_program=T22)))
    col.on_logs(10, logs(create_bytes(MINT2, X, token_program=T22)))
    return col, chain


def trade(col, slot, mint, who, buy, cv, sol=10**9, tok=None, ts=1_790_000_000):
    tok = cv.buy(sol) if buy else tok
    got = sol if buy else cv.sell(tok)
    col.on_logs(slot, logs(trade_bytes(mint, who, buy, got, tok, cv.vsol, cv.vtok, ts=ts, creator=CREATOR, fee_recipient=FEE_TO)))
    return tok


def held(tmp_path, **cfg):
    """A live copy of the wallet's buy of MINT, bought and open."""
    kp = Keypair()                                      # a throwaway key: never funded, nothing leaves the test
    me = bytes(kp.pubkey())
    col, chain = setup(tmp_path, "live", wallets=frozenset({b58(G)}), keypair=kp, **cfg)
    cv = Curve()
    trade(col, 11, MINT, G, True, cv)
    mine = trade(col, 12, MINT, me, True, cv, sol=246_913_580)
    col.flush()
    assert b58(MINT) in col.live.pos and len(chain.sent) == 1
    return col, chain, cv, me, mine


def test_a_live_copys_pool_stays_followed_without_a_paper_copy(tmp_path):
    col, chain, cv, me, mine = held(tmp_path)
    col.pools["POOL"] = b58(MINT)
    col.paper.pos.clear()
    col.paper.pending.clear()                           # the paper's copy closed, or never opened
    assert col._pinned_pools() == {"POOL"}              # the leader's sell, or a sale by hand, still reaches us
    col.c.close()


def refused(*a, **k):
    raise Refused("getLatestBlockhash: HTTP 429")


def sells(col):
    return col.c.execute("SELECT status, tries FROM lorders WHERE side = 'sell' ORDER BY id").fetchall()


def test_a_dry_run_simulates_each_first_buy_of_a_followed_wallet_and_sends_nothing(tmp_path):
    col, chain = setup(tmp_path, "dry")
    cv = Curve()
    chain.sim_logs = logs(trade_bytes(MINT, G, True, 246_913_580, 7_000_000_000_000, 1, 1, creator=CREATOR, fee_recipient=FEE_TO))
    trade(col, 11, MINT, G, True, cv)                   # the wallet's first buy: simulated
    trade(col, 12, MINT, G, True, cv)                   # its second: not a copy
    trade(col, 13, MINT, X, True, cv)                   # a wallet not followed
    trade(col, 14, MINT, G, False, cv, tok=10**12)      # a sell: the dry run only tries the buys
    col.flush()
    rows = col.c.execute("SELECT mode, side, venue, status, tok, units, slot, sol FROM lorders").fetchall()
    fee = int(246_913_580 * 0.0095) + int(246_913_580 * 0.003)
    assert rows == [("dry", "buy", "curve", "sim_ok", 7_000_000_000_000, 90_000, 105, (246_913_580 + fee) / 1e9)]
    assert len(chain.sims) == 1 and not chain.sent
    assert str(chain.sims[0].message.account_keys[0]) == b58(G)   # simulated as the wallet itself: no key of ours anywhere
    chain.sim_err, chain.sim_logs = {"InstructionError": [3, {"Custom": 6003}]}, [
        "Program log: AnchorError occurred. Error Code: TooLittleSolReceived. Error Number: 6003. Error Message: slippage."]
    trade(col, 20, MINT2, G, True, Curve())
    col.flush()
    assert col.c.execute("SELECT status, err FROM lorders WHERE mint = ?", (b58(MINT2),)).fetchone() == ("sim_err", "TooLittleSolReceived")
    col.c.commit()
    assert live_lines(tmp_path / "pump.db", min_per_wallet=1)[0].startswith("dry run (live copies simulated, nothing sent) since ")
    col.c.close()


def test_a_live_copy_buys_holds_and_sells_when_its_wallet_sells(tmp_path):
    kp = Keypair()                                      # a throwaway key: never funded, nothing leaves the test
    me = bytes(kp.pubkey())
    col, chain = setup(tmp_path, "live", wallets=frozenset({b58(G)}), keypair=kp)
    cv = Curve()
    trade(col, 11, MINT, G, True, cv)
    assert len(chain.sent) == 1 and chain.sent[0].message.account_keys[0] == kp.pubkey()   # our buy, signed by us
    mine = trade(col, 12, MINT, me, True, cv, sol=246_913_580)   # it lands before the send's result is picked up ...
    col.flush()                                         # ... and is settled with it
    ((tok, cost),) = col.c.execute("SELECT tok, cost FROM lpos").fetchall()
    fee = int(246_913_580 * 0.0095) + int(246_913_580 * 0.003)
    assert tok == mine and abs(cost - ((246_913_580 + fee) / 1e9 + TX_COST_SOL)) < 1e-12
    trade(col, 20, MINT, G, False, cv, tok=10**12)      # the wallet sells: so do we
    col.flush()
    assert len(chain.sent) == 2
    trade(col, 21, MINT, me, False, cv, tok=mine)
    ((status, pnl),) = col.c.execute("SELECT status, pnl FROM lorders WHERE side = 'sell'").fetchall()
    assert status == "filled" and pnl < 0 and not col.c.execute("SELECT * FROM lpos").fetchall()
    col.c.commit()
    assert live_lines(tmp_path / "pump.db")[-1].startswith("live: buys 1 filled, 0 failed, 0 skipped | sells 1 filled, 0 failed")
    col.c.close()


def test_a_wallet_that_sells_before_our_buy_lands_is_followed_out_and_a_quiet_fill_is_looked_up(tmp_path):
    kp = Keypair()
    me = bytes(kp.pubkey())
    col, chain = setup(tmp_path, "live", wallets=frozenset({b58(G)}), keypair=kp)
    cv = Curve()
    trade(col, 11, MINT, G, True, cv)
    col.flush()                                         # our buy is out ...
    trade(col, 12, MINT, G, False, cv, tok=10**12)      # ... and the wallet already sells
    assert len(chain.sent) == 1
    trade(col, 13, MINT, me, True, cv, sol=246_913_580)
    col.flush()
    assert len(chain.sent) == 2                         # out the moment our buy showed up
    cv2 = Curve()
    trade(col, 30, MINT2, G, True, cv2)
    col.flush()
    (o,) = [o for o in col.live.open.values() if o["mint"] == b58(MINT2)]
    o["done"] -= 20                                     # the feed never showed it: looked up on the chain ...
    real_call, chain.call = chain.call, lambda *a, **k: (_ for _ in ()).throw(RuntimeError("getTransaction: HTTP 429"))
    col.flush()
    col.flush()
    assert o["id"] in col.live.open                     # ... a lookup that fails settles nothing: asked again later
    chain.call = real_call
    chain.txs[o["sig"]] = {"meta": {"err": None, "logMessages": logs(trade_bytes(MINT2, me, True, 246_913_580, 5 * 10**12, 1, 1))}}
    o["next_look"] = 0
    col.flush()
    col.flush()
    assert col.c.execute("SELECT tok FROM lpos WHERE mint = ?", (b58(MINT2),)).fetchone() == (5 * 10**12,)
    col.c.close()


def test_live_copies_stop_at_the_limits_and_a_sell_that_keeps_failing_is_left_to_the_owner(tmp_path, monkeypatch, caplog):
    col, chain, cv, me, mine = held(tmp_path, max_open=1)
    trade(col, 13, MINT2, G, True, Curve())             # one copy open already
    assert col.c.execute("SELECT status, err FROM lorders WHERE mint = ?", (b58(MINT2),)).fetchone() == ("skipped", "1 copies open already")
    chain.land_err = {"InstructionError": [2, {"Custom": 6003}]}   # every sell lands, and fails there
    monkeypatch.setattr("hl_screener.pumplive.RETRY_WAIT_S", 0)   # the tries are two seconds apart in life,
    monkeypatch.setattr("hl_screener.pumplive.FILL_WAIT_S", 0)    # each looked up 15 s after it went out
    trade(col, 20, MINT, G, False, cv, tok=10**12)
    for _ in range(4 * SELL_TRIES):
        col.flush()
    assert sells(col) == [("failed", t) for t in range(1, SELL_TRIES + 1)]
    assert col.c.execute("SELECT stuck FROM lpos").fetchone() == (1,)
    assert caplog.text.count("could not sell") == 1                 # said once, as an error
    col.live.pos[b58(MINT)]["opened"] -= MAX_HOLD_S                 # nor sold again for being held too long
    trade(col, 21, MINT, G, False, cv, tok=10**12)                  # nor when its wallet sells again
    col.flush()
    assert len(chain.sent) == 1 + SELL_TRIES            # stuck: no more tries, the log asks for a hand sell
    col.c.commit()
    assert "1 STUCK: sell by hand" in live_lines(tmp_path / "pump.db")[-1]
    trade(col, 30, MINT, me, False, cv, tok=mine)       # its owner sells it by hand: the copy is closed, its slot free
    assert not col.live.pos and not col.c.execute("SELECT * FROM lpos").fetchall()
    trade(col, 31, MINT3, G, True, Curve())
    assert len(chain.sent) == 2 + SELL_TRIES
    col.c.close()


def test_a_day_of_losses_or_a_thin_wallet_stops_new_copies(tmp_path):
    kp = Keypair()
    col, chain = setup(tmp_path, "live", wallets=frozenset({b58(G)}), keypair=kp)
    col.c.execute("INSERT INTO lorders(mode, side, status, pnl, done) VALUES ('live', 'sell', 'filled', -0.6, ?)", (time.time(),))
    trade(col, 11, MINT, G, True, Curve())
    assert col.c.execute("SELECT err FROM lorders WHERE mint = ?", (b58(MINT),)).fetchone() == ("0.600 SOL lost today",)
    col.c.execute("DELETE FROM lorders WHERE pnl IS NOT NULL")
    col.live.copied.clear()
    col.live.balance = 0.5
    trade(col, 12, MINT, G, True, Curve())              # 0.5 SOL: this one goes out ...
    trade(col, 13, MINT2, G, True, Curve())             # ... and leaves 0.25 for the next, short of stake and reserve
    assert len(chain.sent) == 1
    assert col.c.execute("SELECT err FROM lorders WHERE mint = ? AND status = 'skipped'", (b58(MINT2),)).fetchone() == ("balance 0.250 SOL",)
    col.c.close()


def test_a_buy_ready_too_late_is_not_sent_and_a_sell_goes_out_however_late(tmp_path, monkeypatch, caplog):
    kp = Keypair()                                      # a throwaway key: never funded, nothing leaves the test
    me = bytes(kp.pubkey())
    col, chain = setup(tmp_path, "live", wallets=frozenset({b58(G)}), keypair=kp)
    now = [time.time()]
    monkeypatch.setattr("hl_screener.pumplive.time", types.SimpleNamespace(time=lambda: now[0]))
    caplog.set_level("INFO", logger="hl_screener.pumplive")
    read, latest = chain.account, chain.blockhash

    def slow_read(addr):                                # a coin born before this start: its token program read, in 4 s
        now[0] += 4
        return read(addr)

    chain.account = slow_read
    trade(col, 11, MINT3, G, True, Curve(), ts=int(now[0]))   # block times go with this clock
    col.flush()
    assert not chain.sent and "not copying" in caplog.text
    assert col.c.execute("SELECT status, err FROM lorders WHERE mint = ?", (b58(MINT3),)).fetchone() == (
        "skipped", "too late: 4.0 s after its wallet's buy")
    cv = Curve()
    trade(col, 12, MINT, G, True, cv, ts=int(now[0]))   # its token program known from its creation: out at once
    assert len(chain.sent) == 1
    trade(col, 13, MINT, me, True, cv, sol=246_913_580)
    col.flush()
    assert b58(MINT) in col.live.pos

    def slow_hash(max_age_s=20.0):                      # a minute on the way: a sell goes out all the same
        now[0] += 60
        return latest(max_age_s)

    chain.blockhash = slow_hash
    trade(col, 20, MINT, G, False, cv, tok=10**12)
    col.flush()
    assert len(chain.sent) == 2 and sells(col) == [("sent", 1)]
    col.c.close()


def test_a_dry_run_does_not_simulate_a_buy_that_reached_it_late(tmp_path):
    col, chain = setup(tmp_path, "dry")
    n = int(MAX_AGE_S / SLOT_S)                         # the most slots a buy may reach us late and still go out
    trade(col, 100, MINT3, X, True, Curve())            # the feed has shown slot 100 when the wallet's buys reach it:
    trade(col, 100 - n, MINT, G, True, Curve())         # n slots late, simulated
    trade(col, 99 - n, MINT2, G, True, Curve())         # one more, too late
    col.flush()
    assert len(chain.sims) == 1
    assert col.c.execute("SELECT mint, status, err FROM lorders ORDER BY id").fetchall() == [
        (b58(MINT), "sim_ok", None), (b58(MINT2), "skipped", f"too late: {(n + 1) * SLOT_S:.1f} s after its wallet's buy")]
    col.c.commit()
    assert "failed too late x1" in live_lines(tmp_path / "pump.db", min_per_wallet=1)[0]
    assert LiveCfg.from_env({}).max_age_s == MAX_AGE_S and LiveCfg.from_env({"PUMP_LIVE_MAX_AGE_S": "8"}).max_age_s == 8
    with pytest.raises(ValueError):
        LiveCfg.from_env({"PUMP_LIVE_MAX_AGE_S": "0"})
    exiting = {"PUMP_LIVE": "exit", "PUMP_LIVE_KEY": "k", "PUMP_LIVE_WALLETS": "G", "PUMP_LIVE_MAX_AGE_S": "0"}
    assert LiveCfg.from_env(exiting).mode == "exit"     # a bad buy setting does not keep exit from selling
    col.c.close()


def test_a_buy_the_feeds_thread_got_to_late_is_not_sent_though_its_slot_looks_new(tmp_path):
    """Behind a stall (a database lock), the socket's backlog is handled late, slot clock and all: only the trade's own
    block time, against the usual lag of the others, says how old it is."""
    col, chain = setup(tmp_path, "dry")
    t = int(time.time())
    trade(col, 100, MINT3, X, True, Curve(), ts=t)      # the usual lag, about nothing
    trade(col, 100, MINT, G, True, Curve(), ts=t)       # fresh: simulated
    trade(col, 100, MINT2, G, True, Curve(), ts=t - 20) # its block 20 s ago, the same slot as far as the feed has shown
    col.flush()
    rows = col.c.execute("SELECT mint, status, err FROM lorders ORDER BY id").fetchall()
    assert [r[:2] for r in rows] == [(b58(MINT), "sim_ok"), (b58(MINT2), "skipped")] and rows[1][2].startswith("too late: 1")
    col.c.close()


class Holdings:
    """getTokenAccountsByOwner, faked: what each wallet holds of each coin, over two token accounts; a pair it does
    not know is answered like a rate-limited RPC. Accounts are Chain's: every coin's curve now at 40 SOL / 1e15."""
    account = Chain.account

    def __init__(self, held):
        self.held, self.asked = held, []

    def call(self, method, params, url=None):
        assert method == "getTokenAccountsByOwner" and params[2] == {"encoding": "jsonParsed", "commitment": "confirmed"}
        key = (params[0], params[1]["mint"])
        self.asked.append(key)
        if key not in self.held:
            raise RuntimeError("getTokenAccountsByOwner: HTTP 429")
        n = self.held[key]
        return {"context": {"slot": 1}, "value": [{"pubkey": "A", "account": {"data": {"parsed": {"info": {"tokenAmount": {"amount": str(a)}}}}}}
                                                  for a in (n // 2, n - n // 2) if a]}


def copied(tmp_path, monkeypatch):
    """The wallet's buy of MINT, copied on paper and live, both open; gap checks run at once, without pauses."""
    from hl_screener import pumpfun
    monkeypatch.setattr(pumpfun, "GAP_PACE_S", 0)
    kp = Keypair()
    me = bytes(kp.pubkey())
    col, chain = setup(tmp_path, "live", wallets=frozenset({b58(G)}), keypair=kp)
    col.gap_pool = Now()
    cv = Curve()
    bought = trade(col, 11, MINT, G, True, cv)
    trade(col, 12, MINT, me, True, cv, sol=246_913_580)    # our live buy lands
    trade(col, 15, MINT, X, True, cv)                      # and so does the paper copy, 4 slots behind the wallet
    col.flush()
    assert (b58(G), b58(MINT)) in col.paper.pos and b58(MINT) in col.live.pos and len(chain.sent) == 1
    return col, chain, bought


@pytest.mark.parametrize("left", [0.0, 0.98])
def test_a_wallet_that_sold_while_the_feed_was_down_is_followed_out_on_paper_and_live(tmp_path, monkeypatch, left):
    col, chain, bought = copied(tmp_path, monkeypatch)
    paper_tok, live_tok = col.paper.pos[(b58(G), b58(MINT))][0], col.live.pos[b58(MINT)]["tok"]
    col.rpc = Holdings({(b58(G), b58(MINT)): int(bought * left)})   # all of it, or 2 %: either way it sold unseen
    col.check_gap("reconnect")
    col.flush()
    assert col.rpc.asked == [(b58(G), b58(MINT))] * 2               # live first, then paper
    assert len(chain.sent) == 2                                     # the live copy's sell is out
    assert col.c.execute("SELECT side, status, trigger_slot FROM lorders ORDER BY id DESC LIMIT 1").fetchone() == ("sell", "sent", None)
    col.check_gap("reconnect")                                      # another gap before either sell is done: nothing twice
    col.flush()
    assert len(chain.sent) == 2 and col.stats["gap_sold"] == 2
    for a in col.paper.pending[b58(MINT)]:
        a["t"] -= 10                                                # a quiet coin: the paper copy sells at its last state
    col.flush()
    assert col.c.execute("SELECT side, leader_px, slip_bps, timed_out FROM pfills ORDER BY id DESC LIMIT 1").fetchone() == ("sell", 0.0, None, 1)
    assert (b58(G), b58(MINT)) not in col.paper.pos
    assert (col.stats["gap_checks"], col.stats["gap_sold"]) == (2, 2)
    vsol, vtok = 40 * 10**9, 10**15                                 # the coin as the chain has it now, not as it was before the gap
    ((paper_sol,),) = col.c.execute("SELECT sol FROM pfills WHERE side = 'sell'").fetchall()
    assert abs(paper_sol - (vsol - vsol * vtok / (vtok + paper_tok)) * (1 - FEE) / 1e9) < 1e-12
    ((live_want,),) = col.c.execute("SELECT want FROM lorders WHERE side = 'sell'").fetchall()
    assert live_want == sol_for({"vsol": vsol, "vtok": vtok, "fee": FEE}, live_tok)
    col.c.close()


def test_a_paper_copy_of_a_coin_without_a_price_is_not_sold_by_a_gap_check(tmp_path, monkeypatch, caplog):
    col, chain, bought = copied(tmp_path, monkeypatch)
    col.paper.curve.pop(b58(MINT))                                  # no trade stored, and the chain read fails too
    col.rpc = Holdings({(b58(G), b58(MINT)): 0})
    col.rpc.account = lambda addr: None
    caplog.set_level("INFO", logger="hl_screener.pumpfun")
    col.check_gap("restart")
    col.flush()
    assert (b58(G), b58(MINT)) in col.paper.pos and not col.paper.pending and col.stats["gap_sold"] == 1   # the live copy alone
    assert "the copy waits for its next trade" in caplog.text
    col.c.close()


def test_a_wallet_still_holding_after_a_gap_keeps_its_copies_and_so_does_one_the_rpc_will_not_read(tmp_path, monkeypatch, caplog):
    col, chain, bought = copied(tmp_path, monkeypatch)
    col.rpc = Holdings({(b58(G), b58(MINT)): bought})               # every token it bought, still there
    col.check_gap("restart")
    col.rpc = Holdings({})                                          # the RPC turns us away
    col.check_gap("reconnect")
    col.flush()
    assert len(chain.sent) == 1 and not col.paper.pending
    assert (b58(G), b58(MINT)) in col.paper.pos and b58(MINT) in col.live.pos
    assert (col.stats["gap_checks"], col.stats["gap_sold"]) == (2, 0)
    assert "2 of 2 wallets could not be read (RuntimeError: getTokenAccountsByOwner: HTTP 429): their copies stay open" in caplog.text
    col.paper.pos[(b58(G), b58(MINT))][2] -= GAP_LOOK_BACK_S        # both copies opened over three days ago: left alone,
    col.live.pos[b58(MINT)]["opened"] -= GAP_LOOK_BACK_S            # not read again on every reconnect
    col.rpc = Holdings({})
    col.check_gap("reconnect")
    assert col.rpc.asked == [] and col.stats["gap_checks"] == 2
    col.c.close()


class Subscribing:
    """A pool connection's socket: the server confirms the subscription it is sent, then closes the connection."""
    def __init__(self):
        self.q = asyncio.Queue()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, msg):
        await self.q.put(json.dumps({"jsonrpc": "2.0", "result": 7, "id": json.loads(msg)["id"]}))
        await self.q.put(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        msg = await self.q.get()
        if msg is None:
            raise ConnectionError("sent 1002 (protocol error) invalid status code; no close frame received")
        return msg


def test_a_pool_whose_connection_dropped_has_its_copies_read_once_it_is_subscribed_again(tmp_path, monkeypatch):
    """DJfNX864 (2026-10-08): its pool's connection dropped, its wallet sold meanwhile, and our copy sold 27 min later,
    when a check of everything happened to run. Now the pool's copies are read the moment it is subscribed again."""
    from hl_screener.pumppools import _Conn
    col, chain, bought = copied(tmp_path, monkeypatch)
    col.pools["POOL"] = b58(MINT)                                   # MINT trades on its PumpSwap pool now
    col.paper.pos[(b58(G), b58(MINT2))] = [10**12, 0.25, int(time.time())]   # and a copy of a coin on the main feed
    col.rpc = Holdings({(b58(G), b58(MINT)): 0})                    # the wallet sold all its MINT

    def connection():                                               # placed on a connection, as PoolFeed.place does
        c = _Conn()
        c.pools.add("POOL")
        c.queue.put_nowait("POOL")
        asyncio.run(col.pool_feed._conn(c, types.SimpleNamespace(connect=lambda *a, **k: Subscribing())))

    connection()                                                    # subscribed, then dropped: nothing to read yet
    assert col.rpc.asked == [] and col.stats["gap_checks"] == 0
    connection()                                                    # subscribed again: what we were blind to is read
    col.flush()
    assert col.rpc.asked == [(b58(G), b58(MINT))] * 2               # live, then paper; MINT2's copy is not read
    assert len(chain.sent) == 2 and col.stats["gap_sold"] == 2      # the live copy's sell is out, the paper's on its way
    assert [a["side"] for a in col.paper.pending[b58(MINT)]] == ["sell"]
    col.c.close()


def test_a_followed_wallet_whose_connection_dropped_has_its_copies_read_once_it_is_subscribed_again(tmp_path, monkeypatch):
    """The followed wallets ride the pool feed too, one subscription each: back after a drop, the coins its copies are
    on are read, and no one else's."""
    from hl_screener.pumppools import _Conn
    col, chain, bought = copied(tmp_path, monkeypatch)
    col.paper.pos[(b58(X), b58(MINT2))] = [10**12, 0.25, int(time.time())]   # another wallet's copy: not read
    col.rpc = Holdings({(b58(G), b58(MINT)): 0})                    # G sold all its MINT while we were blind

    def connection():
        c = _Conn()
        c.pools.add(b58(G))
        c.queue.put_nowait(b58(G))
        asyncio.run(col.pool_feed._conn(c, types.SimpleNamespace(connect=lambda *a, **k: Subscribing())))

    connection()
    assert col.rpc.asked == []
    connection()
    col.flush()
    assert col.rpc.asked == [(b58(G), b58(MINT))] * 2               # live, then paper
    assert len(chain.sent) == 2 and col.stats["gap_sold"] == 2
    col.c.close()


def test_drops_every_few_minutes_queue_each_copy_once_and_real_money_is_read_first(tmp_path, monkeypatch):
    """Connections drop every 1-5 min: a copy named by every drop and every reconnect would pile reads up faster than
    one reader gets through them, with the live copy's read behind the paper backlog."""
    from concurrent.futures import Future
    col, chain, bought = copied(tmp_path, monkeypatch)
    col.gap_pool = types.SimpleNamespace(submit=lambda *a, **k: Future())   # the reader is busy: nothing is read yet
    for _ in range(3):
        col.check_gap("pool feed drop", {b58(MINT)})
        col.check_gap("reconnect")
    order = [(e[3][0], e[1]) for e in sorted(col.gap_first.queue)]
    assert order == [("live", False), ("live", True), ("paper", False), ("paper", True)]   # each once a kind of check
    col.c.close()


def test_a_pools_copies_are_read_before_the_rest_of_a_check_already_running(tmp_path, monkeypatch):
    """A check of every copy reads one every GAP_PACE_S, minutes for hundreds of them, after each main-feed reconnect
    (every 1-5 min): a pool's few copies queued behind it would be read minutes late."""
    import threading
    from concurrent.futures import ThreadPoolExecutor
    col, chain, bought = copied(tmp_path, monkeypatch)
    col.pools["POOL"] = b58(MINT3)
    col.paper.pos[(b58(G), b58(MINT3))] = [10**12, 0.25, int(time.time())]
    col.gap_pool = ThreadPoolExecutor(max_workers=1)                # a real worker, one read at a time
    rpc, first, go = Holdings({(b58(G), b58(m)): bought for m in (MINT, MINT3)}), threading.Event(), threading.Event()
    read = rpc.call

    def slow(*args, **kwargs):                                      # the full check's first read takes a while
        if not rpc.asked:
            first.set()
            go.wait(5)
        return read(*args, **kwargs)

    rpc.call, col.rpc = slow, rpc
    col.check_gap("reconnect")                                      # every copy: the live one first, then the paper ones
    assert first.wait(5)
    col.check_gap("pool feed drop", {b58(MINT3)})                   # meanwhile a pool connection dropped and is back
    go.set()
    col.gap_pool.shutdown(wait=True)
    assert rpc.asked == [(b58(G), b58(m)) for m in (MINT, MINT3, MINT, MINT3)]   # the pool's copy second, not last
    col.c.close()


def test_the_settings_refuse_a_live_start_without_a_key_and_never_show_it():
    assert LiveCfg.from_env({}).mode == "dry"
    with pytest.raises(ValueError):
        LiveCfg.from_env({"PUMP_LIVE": "live", "PUMP_LIVE_WALLETS": "G"})
    with pytest.raises(ValueError):
        LiveCfg.from_env({"PUMP_LIVE": "sometimes"})
    with pytest.raises(ValueError):
        LiveCfg.from_env({"PUMP_LIVE_STAKE_SOL": "50"})
    cfg = LiveCfg.from_env({"PUMP_LIVE": "live", "PUMP_LIVE_KEY": "a-secret", "PUMP_LIVE_WALLETS": "G"})
    assert "a-secret" not in repr(cfg) and "a-secret" not in str(dataclasses.asdict(cfg))   # the settings never hold it
    with pytest.raises(ValueError) as e:
        load_keypair({"PUMP_LIVE_KEY": "a-secret"})
    assert "a-secret" not in str(e.value) and e.value.__cause__ is None
    kp = Keypair()
    assert load_keypair({"PUMP_LIVE_KEY": str(kp)}).pubkey() == kp.pubkey()
    with pytest.raises(ValueError):
        LiveFollow(None, cfg)                           # live without its keypair: refused before anything else


def test_a_copy_its_owner_sells_by_hand_is_closed_and_a_part_sold_leaves_the_rest_copied(tmp_path, caplog):
    col, chain, cv, me, mine = held(tmp_path, max_open=1)
    ((cost,),) = col.c.execute("SELECT cost FROM lpos").fetchall()
    part = mine // 4
    trade(col, 15, MINT, me, False, cv, tok=part)                  # a quarter sold by hand: the copy goes on with the rest
    assert col.c.execute("SELECT tok, cost FROM lpos").fetchone() == (mine - part, cost * (1 - part / mine))
    assert col.live.pos[b58(MINT)]["tok"] == mine - part
    col.live.retries.append((time.time() + 60, b58(MINT), 2))       # a retry waiting, as after a failed sell
    trade(col, 16, MINT, me, False, cv, tok=mine - part)            # then the rest
    assert not col.live.pos and not col.live.retries and not col.c.execute("SELECT * FROM lpos").fetchall()
    rows = col.c.execute("SELECT status, err, tok, pnl FROM lorders WHERE side = 'sell' ORDER BY id").fetchall()
    assert [r[:3] for r in rows] == [("filled", "sold outside the copies", part), ("filled", "sold outside the copies", mine - part)]
    assert all(r[3] is not None for r in rows)
    assert "outside the copies" in caplog.text
    trade(col, 20, MINT2, G, True, Curve())                         # its slot is free again
    assert len(chain.sent) == 2
    trade(col, 21, MINT, G, False, cv, tok=10**12)                  # and its wallet's sell finds nothing to sell
    col.flush()
    assert len(chain.sent) == 2
    col.c.close()


def test_a_copy_held_too_long_is_priced_on_the_chain_and_a_failing_sell_waits_for_its_retry(tmp_path, monkeypatch):
    col, chain, cv, me, mine = held(tmp_path)
    chain.land_err = {"InstructionError": [2, {"Custom": 6003}]}
    monkeypatch.setattr("hl_screener.pumplive.FILL_WAIT_S", 0)
    monkeypatch.setattr("hl_screener.pumplive.RETRY_WAIT_S", 60)
    col.live.pos[b58(MINT)]["opened"] -= MAX_HOLD_S
    for _ in range(12):
        col.flush()                                     # its try 1 failed on the chain: nothing more before try 2 is due
    assert sells(col) == [("failed", 1)] and len(chain.sent) == 2 and [r[1:] for r in col.live.retries] == [(b58(MINT), 2)]
    ((want,),) = col.c.execute("SELECT want FROM lorders WHERE side = 'sell'").fetchall()
    assert want == sol_for({"vsol": 40 * 10**9, "vtok": 10**15, "fee": FEE}, mine)   # the coin as the chain has it now
    monkeypatch.setattr("hl_screener.pumplive.RETRY_WAIT_S", 0)
    col.live.retries = [(0.0, *r[1:]) for r in col.live.retries]
    for _ in range(12):
        col.flush()
    assert sells(col) == [("failed", t) for t in range(1, SELL_TRIES + 1)] and len(chain.sent) == 1 + SELL_TRIES
    assert col.live.pos[b58(MINT)]["stuck"] == 1        # stuck, and said: not sent again every tick for ever
    col.c.close()


def test_a_sell_the_rpc_will_not_let_out_keeps_its_tries(tmp_path, monkeypatch):
    col, chain, cv, me, mine = held(tmp_path)
    monkeypatch.setattr("hl_screener.pumplive.RETRY_WAIT_S", 0)
    chain.blockhash = refused                                       # rate limited: no blockhash, nothing can go out
    trade(col, 20, MINT, G, False, cv, tok=10**12)
    for _ in range(3):
        col.flush()
    chain.fail_send = True                                          # then every send endpoint refuses it
    del chain.blockhash
    for _ in range(2):
        col.flush()
    assert set(sells(col)) == {("unsent", 1), ("pending", 1)} and len(chain.sent) == 1   # the next attempt on its way
    chain.fail_send = False
    col.flush()
    col.flush()
    assert sells(col)[-1] == ("sent", 1) and len(chain.sent) == 2 and not col.live.pos[b58(MINT)]["stuck"]
    lists = col.live.lists[False][1]
    col.live.lists[False] = (0.0, lists)                            # the hourly read of the recipient lists is due ...
    chain.account = refused
    assert col.live._lists(False) == lists                          # ... and refused: the last ones still name valid accounts
    col.c.close()


def test_a_sell_the_rpc_never_lets_out_ends_stuck_after_a_bounded_wait(tmp_path, monkeypatch, caplog):
    col, chain, cv, me, mine = held(tmp_path)
    monkeypatch.setattr("hl_screener.pumplive.RETRY_WAIT_S", 0)
    monkeypatch.setattr("hl_screener.pumplive.UNSENT_MAX", 2)
    chain.blockhash = refused
    trade(col, 20, MINT, G, False, cv, tok=10**12)
    for _ in range(4 * 3 * SELL_TRIES):
        col.flush()
    assert [r for r in sells(col) if r[0] == "failed"] == [("failed", t) for t in range(1, SELL_TRIES + 1)]
    assert col.live.pos[b58(MINT)]["stuck"] == 1 and len(chain.sent) == 1 and "could not sell" in caplog.text
    col.c.close()


def test_a_send_that_raised_but_went_out_is_looked_up_and_a_fill_seen_settles_its_order(tmp_path, monkeypatch):
    kp = Keypair()
    me = bytes(kp.pubkey())
    col, chain = setup(tmp_path, "live", wallets=frozenset({b58(G)}), keypair=kp)
    chain.send_raises = True                            # it went out, but its answer timed out
    cv = Curve()
    trade(col, 11, MINT, G, True, cv)
    mine = trade(col, 12, MINT, me, True, cv, sol=246_913_580)   # the feed shows it before the send's result is picked up
    col.flush()
    assert col.c.execute("SELECT tok FROM lpos WHERE mint = ?", (b58(MINT),)).fetchone() == (mine,)
    monkeypatch.setattr("hl_screener.pumplive.FILL_WAIT_S", 0)
    trade(col, 20, MINT2, G, True, Curve())
    col.flush()                                         # this one the feed never shows: the chain is asked
    (o,) = [o for o in col.live.open.values() if o["mint"] == b58(MINT2)]
    assert "ReadTimeout" in o["err"]
    chain.txs[o["sig"]] = {"meta": {"err": None, "logMessages": logs(trade_bytes(MINT2, me, True, 246_913_580, 5 * 10**12, 1, 1))}}
    col.flush()
    o["next_look"] = 0
    col.flush()
    col.flush()
    assert col.c.execute("SELECT tok FROM lpos WHERE mint = ?", (b58(MINT2),)).fetchone() == (5 * 10**12,)
    chain.send_raises, chain.fail_send = False, True    # every endpoint said no, and yet our fill shows: the fill decides
    cv3 = Curve()
    trade(col, 30, MINT3, G, True, cv3)
    trade(col, 31, MINT3, me, True, cv3, sol=246_913_580)
    col.flush()
    assert b58(MINT3) in col.live.pos
    col.c.close()


def test_a_live_start_does_not_copy_a_coin_the_paper_or_the_dry_run_copied_of_that_wallet(tmp_path):
    kp = Keypair()
    col, chain = setup(tmp_path, "live", wallets=frozenset({b58(G)}), keypair=kp)
    col.c.execute("INSERT INTO pfills(wallet, mint, side) VALUES (?, ?, 'buy')", (b58(G), b58(MINT)))
    col.c.execute("INSERT INTO lorders(mode, wallet, mint, side, status) VALUES ('dry', ?, ?, 'buy', 'sim_ok')", (b58(G), b58(MINT2)))
    col.live = LiveFollow(col.c, col.live.cfg, rpc=chain, workers=Now(), keypair=kp)   # the first live start, after the dry run
    col.live.balance = 1.0
    trade(col, 11, MINT, G, True, Curve())              # it adds to coins it bought before: no first buys
    trade(col, 12, MINT2, G, True, Curve())
    assert not chain.sent
    trade(col, 13, MINT3, G, True, Curve())             # a first buy is copied
    assert len(chain.sent) == 1
    col.c.close()


def test_exit_mode_sells_the_open_copies_and_copies_nothing_new(tmp_path):
    with pytest.raises(ValueError):
        LiveCfg.from_env({"PUMP_LIVE": "exit", "PUMP_LIVE_WALLETS": "G"})       # it sells: it needs the key
    assert LiveCfg.from_env({"PUMP_LIVE": "exit", "PUMP_LIVE_KEY": "k", "PUMP_LIVE_WALLETS": "G"}).mode == "exit"
    col, chain, cv, me, mine = held(tmp_path)
    col.live = LiveFollow(col.c, dataclasses.replace(col.live.cfg, mode="exit"), rpc=chain, workers=Now(), keypair=col.live.kp)
    col.flush()
    col.flush()
    trade(col, 20, MINT2, G, True, Curve())
    assert col.c.execute("SELECT mode, status, err FROM lorders WHERE mint = ?", (b58(MINT2),)).fetchone() == ("live", "skipped", "winding down")
    trade(col, 21, MINT, G, False, cv, tok=10**12)      # the open copy still follows its wallet out
    col.flush()
    assert len(chain.sent) == 2 and str(chain.sent[1].message.account_keys[0]) == b58(me)
    trade(col, 22, MINT, me, False, cv, tok=mine)
    assert not col.c.execute("SELECT * FROM lpos").fetchall()
    assert col.c.execute("SELECT mode, status FROM lorders WHERE side = 'sell'").fetchall() == [("live", "filled")]
    col.c.close()


def test_the_live_line_shows_the_wallet_and_live_against_paper_on_the_same_copies(tmp_path):
    col, chain, cv, me, mine = held(tmp_path)
    w = b58(G)
    for m, live_pnl, paper_pnl in ((b58(MINT2), 0.002, 0.03), (b58(MINT3), -0.01, -0.04)):
        col.c.execute("INSERT INTO lorders(mode, wallet, mint, side, status, sol) VALUES ('live', ?, ?, 'buy', 'filled', 0.05)", (w, m))
        col.c.execute("INSERT INTO lorders(mode, wallet, mint, side, status, pnl, done) VALUES ('live', ?, ?, 'sell', 'filled', ?, 0)",
                      (w, m, live_pnl))
        col.c.execute("INSERT INTO pfills(wallet, mint, side, sol) VALUES (?, ?, 'buy', 0.25)", (w, m))
        col.c.execute("INSERT INTO pfills(wallet, mint, side, pnl) VALUES (?, ?, 'sell', ?)", (w, m, paper_pnl))
    col.c.commit()
    lines = live_lines(tmp_path / "pump.db")
    assert "wallet 1.000 SOL at " in lines[-2]                       # as last read, and when
    # live 0.05 SOL a copy, paper 0.25: the 2 x 0.001505 SOL of fees a round weigh 6.0% on one and 1.2% on the other
    assert lines[-1] == ("live vs paper on the same 2 copies: live -0.0080 SOL, paper -0.0100 SOL, live minus paper per copy median -6.0%, "
                         "-1.2% with the paper at the live stake")
    col.c.close()


def test_every_live_setting_reaches_the_container_and_sends_go_to_helius_alone_by_default():
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    code = "".join((root / "hl_screener" / f).read_text(encoding="utf-8") for f in ("pumplive.py", "pumpfun.py"))
    names = set(re.findall(r'"(PUMP_[A-Z_]+)"', code))
    assert {"PUMP_LIVE_SEND", "PUMP_LIVE_BUY_SLIP", "PUMP_LIVE_SELL_SLIP", "PUMP_GAP_RPC"} <= names
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    assert [n for n in sorted(names) if f"{n}: ${{{n}:-" not in compose] == []     # Coolify passes only what is listed
    assert LiveCfg.from_env({}).send_urls == ("https://sender.helius-rpc.com/fast",)   # Jito alone wants its own tip
    assert LiveCfg.from_env({"PUMP_LIVE_SEND": "https://a, https://b"}).send_urls == ("https://a", "https://b")


def test_the_startup_line_says_what_the_live_copies_will_do(tmp_path, monkeypatch, capsys):
    """The go-live checklist reads this line: the mode, our address, whom it copies, with what."""
    import logging

    from hl_screener import pumpfun

    async def run(self, stop=None):
        pass

    monkeypatch.setattr(Collector, "run", run)
    kp = Keypair()                                      # a throwaway key: nothing is read, signed or sent
    monkeypatch.setenv("PUMP_LIVE_KEY", str(kp))
    monkeypatch.setenv("PUMP_LIVE_WALLETS", b58(G))
    monkeypatch.setenv("PUMP_LIVE_STAKE_SOL", "0.05")
    monkeypatch.setenv("PUMP_LIVE_MAX_OPEN", "1")
    levels = {n: logging.getLogger(n).level for n in ("hl_screener.pumpfun", "hl_screener.pumplive")}
    try:
        for mode in ("live", "exit"):
            monkeypatch.setenv("PUMP_LIVE", mode)
            pumpfun.collect(tmp_path / "pump.db", pumpfun.PUBLIC_WS, 2, sniper_every_s=3600)
        first = [line for line in capsys.readouterr().out.splitlines() if line.startswith("live copies")]
    finally:
        for n, level in levels.items():
            logging.getLogger(n).setLevel(level)
    assert first == [f"live copies: live from {kp.pubkey()}, copying {b58(G)} with 0.05 SOL, at most 1 open, new copies stop "
                     "after 0.5 SOL lost in a day",
                     f"live copies: exit from {kp.pubkey()}, winding down: no new copies, the 0 open ones sold as their wallets sell"]
