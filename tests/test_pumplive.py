import dataclasses
import time
from concurrent.futures import Future

import pytest

pytest.importorskip("solders")

from solders.hash import Hash  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from test_pump import Curve, b58decode, create_bytes, logs, trade_bytes  # noqa: E402

from hl_screener.pumpfun import TX_COST_SOL, Collector, b58  # noqa: E402
from hl_screener.pumplive import SELL_TRIES, LiveCfg, LiveFollow, live_lines, load_keypair  # noqa: E402
from hl_screener.pumptx import AMM_GLOBAL_CONFIG, PUMP_GLOBAL, PUMP_PROGRAM, TOKEN_2022_PROGRAM  # noqa: E402

G, X, MINT, MINT2 = bytes([80]) * 32, bytes([81]) * 32, bytes([9]) * 32, bytes([10]) * 32
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
            raise RuntimeError("sendTransaction: HTTP 429")
        self.sent.append(tx)
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


def trade(col, slot, mint, who, buy, cv, sol=10**9, tok=None):
    tok = cv.buy(sol) if buy else tok
    got = sol if buy else cv.sell(tok)
    col.on_logs(slot, logs(trade_bytes(mint, who, buy, got, tok, cv.vsol, cv.vtok, creator=CREATOR, fee_recipient=FEE_TO)))
    return tok


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


def test_live_copies_stop_at_the_limits_and_a_sell_that_keeps_failing_is_left_to_the_owner(tmp_path):
    kp = Keypair()
    me = bytes(kp.pubkey())
    col, chain = setup(tmp_path, "live", wallets=frozenset({b58(G)}), keypair=kp, max_open=1)
    cv = Curve()
    trade(col, 11, MINT, G, True, cv)
    trade(col, 12, MINT, me, True, cv, sol=246_913_580)
    col.flush()
    trade(col, 13, MINT2, G, True, Curve())             # one copy open already
    assert col.c.execute("SELECT status, err FROM lorders WHERE mint = ?", (b58(MINT2),)).fetchone() == ("skipped", "1 copies open already")
    chain.fail_send = True
    trade(col, 20, MINT, G, False, cv, tok=10**12)
    for _ in range(SELL_TRIES + 1):
        col.flush()
    assert col.c.execute("SELECT COUNT(*) FROM lorders WHERE side = 'sell' AND status = 'failed'").fetchone() == (SELL_TRIES,)
    assert col.c.execute("SELECT stuck FROM lpos").fetchone() == (1,)
    col.flush()
    assert len(chain.sent) == 1                         # stuck: no more tries, the log asks for a hand sell
    col.c.commit()
    assert "1 STUCK: sell by hand" in live_lines(tmp_path / "pump.db")[-1]
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
