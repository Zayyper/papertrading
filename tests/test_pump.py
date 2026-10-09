import base64
import struct
import time

from hl_screener.pumpfun import (_B58, AMM_PROGRAM, D_BUY, _best, D_CREATE, D_POOL, D_SELL, D_TRADE, FEE, PUMP_PROGRAM, WSOL, Collector,
                                 BASE_FEE_SOL, PAPER_STAKE_SOL, PRIORITY_SOL, TIP_SOL, TX_COST_SOL, b58, build_report, copy_trade,
                                 cohorts_of, launch_pnl, maker_table, operator_groups, paper_series, parse_amm_trade, parse_create,
                                 parse_trade, settle_launches, strategy_sim, strategy_states, twins, update_snipers)


def logs(b: bytes, program: str = PUMP_PROGRAM) -> list[str]:
    """A transaction's logs as Solana prints them: the event sits inside its program's invocation."""
    return [f"Program {program} invoke [1]", "Program data: " + base64.b64encode(b).decode(), f"Program {program} success"]


def b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        n = n * 58 + _B58.index(ch)
    return n.to_bytes(32, "big")


def amm_bytes(buy, pool, user, base, B, Q, q, lp, q_net, user_q, vq=0, ix="buy", cashback=0):
    b = (D_BUY if buy else D_SELL) + struct.pack("<q", 1_790_000_000)
    b += struct.pack("<13Q", base, 0, 0, 0, B, Q, q, 20, lp, 5, 0, q_net, user_q) + pool + user + bytes(32 * 5) + struct.pack("<QQ", 30, 0)
    if buy:
        b += struct.pack("<?QQQqQ", False, 0, 0, 0, 0, 0) + s(ix)
    return b + struct.pack("<4Q", cashback, 0, 0, 0) + vq.to_bytes(16, "little", signed=True) + struct.pack("<?QQQ", False, 0, 0, 0)


def pool_bytes(pool, base_mint, quote_mint):
    b = D_POOL + struct.pack("<qH", 0, 0) + bytes(32) + base_mint + quote_mint + struct.pack("<BB7QB", 6, 9, *([0] * 7), 255)
    return b + pool + bytes(32 * 4) + struct.pack("<?Q??", False, 0, False, False)


def s(x: str) -> bytes:
    return struct.pack("<I", len(x)) + x.encode()


def trade_bytes(mint, user, buy, sol, tok, vsol, vtok, ts=1_790_000_000, quote=bytes(32), ix="buy", shareholders=0,
                fee_recipient=bytes(32), creator=bytes(32), mayhem=False, fees=True, cashback=0):
    fee, cfee = (int(sol * 0.0095), int(sol * 0.003)) if fees else (0, 0)
    b = D_TRADE + mint + struct.pack("<QQ?", sol, tok, buy) + user + struct.pack("<qQQ", ts, vsol, vtok)
    b += struct.pack("<QQ", 0, 0) + fee_recipient + struct.pack("<QQ", 95, fee) + creator + struct.pack("<QQ", 30, cfee)
    b += struct.pack("<?QQQq", True, 0, 0, 0, 0) + s(ix)
    b += struct.pack("<?QQQQ", mayhem, cashback, 0, 0, 0) + struct.pack("<I", shareholders) + bytes(34 * shareholders)
    return b + quote + struct.pack("<5Q", 0, 0, 0, 0, 0)


def create_bytes(mint, user, ts=1_790_000_000, quote=bytes(32), token_program=bytes(32)):
    b = D_CREATE + s("Cat") + s("CAT") + s("https://x") + mint + bytes(32) + user + bytes(32) + struct.pack("<q", ts)
    return b + struct.pack("<4Q", 0, 0, 0, 0) + token_program + struct.pack("<??", False, False) + quote + struct.pack("<QQ?", 0, 0, False)


def test_parse_the_layout_or_its_known_tail_and_refuse_the_rest():
    mint, user = bytes([1]) * 32, bytes([2]) * 32
    e = parse_trade(trade_bytes(mint, user, True, 10**9, 5 * 10**12, 31 * 10**9, 10**15, shareholders=2))
    assert e["buy"] and e["sol"] == 10**9 and e["user"] == user and e["sol_quote"]
    assert e["fee"] == int(10**9 * 0.0095) + int(10**9 * 0.003)
    b = trade_bytes(mint, user, True, 1, 1, 1, 1)
    assert parse_trade(b + bytes(8)) == parse_trade(b)                                    # the tail of 2026-10-02: read without it
    assert parse_trade(b + bytes(1)) is None and parse_trade(b + bytes(16)) is None      # any other length: refuse
    assert parse_trade(b[:-1]) is None
    assert parse_trade(trade_bytes(mint, user, True, 1, 1, 1, 1, ix="b\x00y")) is None   # fields moved: not a name, refuse
    assert not parse_trade(trade_bytes(mint, user, True, 1, 1, 1, 1, quote=bytes([9]) * 32))["sol_quote"]
    c = parse_create(create_bytes(mint, user))
    assert c["mint"] == mint and c["user"] == user and c["symbol"] == "CAT" and c["sol_quote"]
    assert parse_create(create_bytes(mint, user)[:-1]) is None
    assert parse_create(create_bytes(mint, user) + bytes(1)) == c                         # CreateV2's tail of 2026-10-08
    assert parse_create(create_bytes(mint, user) + bytes(2)) is None and parse_create(create_bytes(mint, user) + bytes(8)) is None


def test_a_create_v2_event_from_mainnet_parses():
    """pump.fun's CreateV2 coins (2026-10-08 ~16:20 UTC on) log a CreateEvent 1 byte longer than the published layout:
    every one was refused, and no new coin was followed."""
    import base64
    import json
    from pathlib import Path
    fx = json.loads((Path(__file__).parent / "fixtures" / "pump_create_v2_2026-10-08.json").read_text(encoding="utf-8"))
    c = parse_create(base64.b64decode(fx["event_b64"]))
    assert c is not None and b58(c["mint"]) == fx["mint"] and b58(c["user"]) == fx["user"]
    assert b58(c["token_program"]) == fx["token_program"] and c["sol_quote"]


def test_todays_events_parse_with_the_field_pump_fun_appended():
    """Three events from mainnet on 2026-10-05, each 8 bytes longer than the published layout, which stopped every
    trade from 2026-10-02 15:48 UTC. Each names its transaction's signer as the trader."""
    curve_buy = base64.b64decode(
        "vdt/007mYe56Dy5lvdCRFrxk8JtnES5kd6CtLk5zIT3sD+SUBuFqfwz8EAAAAAAAhnfZJAkAAAABEInemyEgse2GFTwHUjhVw7rZ5muUhkd3oy0uhI6dZKGv2c"
        "NqAAAAAD1lZggHAAAAEwPa+T3JAwA9uUIMAAAAABNrx62sygIA4ATIfOuY+lzkf4A4Bv0seUXSlSSVmuwA3tl4FPOPeEZfAAAAAAAAAE8pAAAAAAAAVUWwc0Zfly"
        "QFonzWh+WHbvFNu8cw16UrWpWZuwwit6geAAAAAAAAAAwNAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAwAAAGJ1eQAAAAAAAAAAAAAAAAAA"
        "AAAAiBMAAAAAAACnFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAz8EAAAAAAAPWVmCAcAAAA9uUIMAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAA==")
    pool_buy = base64.b64decode(
        "Z/RSHyz1d3ev2cNqAAAAAKlBjXAAAAAAIr+1AAAAAAAAAAAAAAAAACK/tQAAAAAA1ZIUSx3SBwDAwjBOlwwAACK/tQAAAAAAGQAAAAAAAAD5cwAAAAAAAAUAAAAA"
        "AAAAMhcAAAAAAADwp7UAAAAAAPcztQAAAAAAHD3Jo2HIJcySwVewP4g1/ApIEud7nFdr2WX8Ql2rVfIZtMP01xR31Od2E0TOKBEGV4evFIWpc7TxjyVm41ig2pQZ"
        "Owd2yAyWpNEa8DYc8ZbUZNRMXRWOArbIvN6iOQJ2mTrTrC7j0O6jraKrOap+4hZ7MpArlC6g9LK8QCM3qoTXqo+wYNgpG0xNR12v92LJa9wNrOs2wBLq0S7TqUhB"
        "YR1A0DnVpcb23YuZx5/4/yZMB7KjKc+LXfik7qAIH9htAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAQAAAAAAAAASAAAAYnV5X2V4YWN0X3F1b3RlX2luAAAAAAAAAAAAAAAAAAAAAIgTAAAAAAAAmQsAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAABZW/m8/9YMLAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    pool_sell = base64.b64decode(
        "Pi83CqUD3CrB2cNqAAAAAJB8pZ9kAAAAUDGyAAAAAADwHbRNqBEAAAAAAAAAAAAAvSF4CWNkMgB7+/hpWgAAAL+ItAAAAAAAGQAAAAAAAACLcwAAAAAAAAUAAAAAAA"
        "AAHBcAAAAAAAA0FbQAAAAAABj+swAAAAAAU4U+R4Zk5MQ8e4p4oSHfwBvHodQIviWOk1sZdRfLL3npNHrcpy5d8O0fq3SW53PQFppv2Sc64blaMqu8mWAl5lUKUxUw"
        "FHinLghRe4aY5+EPr4UoKcBiodag4rtvBpeNs5A75VOOkxh5PBYZESaDaMT/BsbqvU7YagMhaJutQLjgBMh865j6XOR/gDgG/Sx5RdKVJJWa7ADe2XgU8494RpwlJH"
        "x6/mh3nE583EE55WuEze54pO2bR06h+o7pPOIlAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAIgT"
        "AAAAAAAAjgsAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAK7/TY+0HceKAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    e = parse_trade(curve_buy)
    assert e and e["buy"] and e["ix"] == "buy" and e["sol_quote"] and b58(e["user"]) == "27ZTRxzCXD8rgjCPWxyFacppGReB5Vx4LcEiLd6WeD4C"
    assert e == parse_trade(curve_buy[:-8])                    # the appended field changes nothing we read
    for raw, buy, signer in ((pool_buy, True, "2jM4cg3nAXWKJaVMvHs2JFtDsg3uviwgP3tXDhZh4433"),
                             (pool_sell, False, "GhLQcTvJNfXNK2F6wjrJjGP9c4VDUY8jYceRmiXadaCM")):
        a = parse_amm_trade(raw)
        assert a and a["buy"] is buy and b58(a["user"]) == signer and a["sol"] > 0 and a["tok"] > 0 and a["vsol"] > 0
        assert a == parse_amm_trade(raw[:-8])


def test_a_cashback_coin_is_known_by_its_trades():
    """A cashback coin's sells name the seller's volume accumulator, and its PumpSwap buys that account's wrapped SOL:
    without them InvalidCashbackAccumulator (mainnet simulations, 2026-10-07). Its trade events carry the cashback fee,
    30 bps on the curve and 95 on PumpSwap, 0 on every other coin, in the first of the four u64 after the mayhem flag."""
    from hl_screener.pumptx import coin_of
    mint, user, pool = bytes([1]) * 32, bytes([2]) * 32, bytes([3]) * 32
    plain = trade_bytes(mint, user, False, 10**9, 5 * 10**12, 31 * 10**9, 10**15)
    cash = trade_bytes(mint, user, False, 10**9, 5 * 10**12, 31 * 10**9, 10**15, cashback=30)
    assert len(cash) == len(plain) and parse_trade(cash + bytes(8)) == parse_trade(cash)    # the same layout and tail
    assert coin_of(b58(mint), parse_trade(cash))["cashback"] is True
    assert coin_of(b58(mint), parse_trade(plain))["cashback"] is False
    for buy in (True, False):
        args = (buy, pool, user, 10**9, 10**15, 40 * 10**9, 10**9, 0, 10**9, 10**9)
        assert coin_of(b58(mint), parse_amm_trade(amm_bytes(*args, cashback=95)))["cashback"] is True
        assert coin_of(b58(mint), parse_amm_trade(amm_bytes(*args)))["cashback"] is False


class Curve:
    """Constant product on virtual reserves, like the pump.fun bonding curve."""
    def __init__(self):
        self.vsol, self.vtok = 30 * 10**9, 1_073_000_000 * 10**6

    def buy(self, sol):
        k = self.vsol * self.vtok
        tok = self.vtok - k // (self.vsol + sol)
        self.vsol, self.vtok = self.vsol + sol, self.vtok - tok
        return tok

    def sell(self, tok):
        k = self.vsol * self.vtok
        sol = self.vsol - k // (self.vtok + tok)
        self.vsol, self.vtok = self.vsol - sol, self.vtok + tok
        return sol


def test_collector_report_snipers_devs_and_copy_replay(tmp_path):
    db = tmp_path / "pump.db"
    col = Collector(db)
    mint, other_mint = bytes([7]) * 32, bytes([8]) * 32
    W = {n: bytes([i]) * 32 for i, n in enumerate("DSTXYZ", start=20)}
    curve, held, states = Curve(), {}, {}

    def trade(slot, who, buy, amount):
        if buy:
            tok = curve.buy(amount)
            held[who] = held.get(who, 0) + tok
            b = trade_bytes(mint, W[who], True, amount, tok, curve.vsol, curve.vtok, ts=1_790_000_000 + slot)
        else:
            tok = held.pop(who)
            sol = curve.sell(tok)
            b = trade_bytes(mint, W[who], False, sol, tok, curve.vsol, curve.vtok, ts=1_790_000_000 + slot)
        states[slot] = (curve.vsol, curve.vtok)
        col.on_logs(slot, logs(b))

    col.on_logs(1000, logs(create_bytes(mint, W["D"], ts=1_790_001_000)))
    trade(1000, "D", True, 10**9)            # the dev's own buy: never counted as trading
    trade(1001, "S", True, 5 * 10**8)        # sniper: one slot after creation
    trade(1010, "T", True, 10**9)
    trade(1011, "X", True, 3 * 10**9)
    trade(1012, "Y", True, 2 * 10**9)
    trade(1030, "T", False, None)
    trade(1031, "Z", True, 10**9)
    trade(1050, "S", False, None)
    # a trade on a token born before the collector started is ignored, and so is an event another program logs
    col.on_logs(1060, logs(trade_bytes(other_mint, W["X"], True, 10**9, 10**12, 31 * 10**9, 10**15)))
    col.on_logs(1061, logs(trade_bytes(mint, W["X"], True, 10**9, 10**12, 31 * 10**9, 10**15), program="SomeOtherLaunchpad111111111111111111111111"))
    col.flush()
    assert col.c.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 8 and col.stats["parse_errors"] == 0

    rep = build_report(db, min_tokens=1, min_snipes=1, latency_slots=2, stake_sol=0.1, tx_cost_sol=0.0005)
    traders = {r["addr"]: r for r in rep["traders"]}
    snipers = {r["addr"]: r for r in rep["snipers"]}
    d, s_, t = b58(W["D"]), b58(W["S"]), b58(W["T"])
    assert d not in traders and d not in snipers                 # the launcher's own position is excluded
    assert snipers[s_]["snipes"] == 1 and snipers[s_]["block0"] == 0
    assert traders[t]["pnl_sol"] > 0                             # bought before X and Y, sold after
    # the copier lands 2 slots after T: buys after X's buy at 1011, sells after Z's buy at 1031
    expected = copy_trade(states[1011], states[1031], 0.1, 0.0005)
    assert traders[t]["copy_n"] == 1 and abs(traders[t]["copy_roi"] - expected / 0.1) < 1e-9
    assert traders[t]["copy_roi"] < traders[t]["roi"]            # the copy gap is real
    col.c.close()


def test_paper_follow_copies_like_the_replay_and_lands_quiet_tokens(tmp_path):
    col = Collector(tmp_path / "pump.db")
    G, X, Y, Z = (bytes([i]) * 32 for i in (80, 81, 82, 83))
    col.c.execute("INSERT INTO follow(wallet, added_at, golden_now, report_copy_roi) VALUES (?, 0, 1, 0.5)", (b58(G),))
    col.paper.reload()
    mint, curve, held, states = bytes([9]) * 32, Curve(), {}, {}

    def trade(slot, who, buy, amount, m=mint, cv=curve):
        if buy:
            tok = cv.buy(amount)
            held[(who, m)] = held.get((who, m), 0) + tok
            b = trade_bytes(m, who, True, amount, tok, cv.vsol, cv.vtok)
        else:
            tok = held.pop((who, m))
            b = trade_bytes(m, who, False, cv.sell(tok), tok, cv.vsol, cv.vtok)
        states[(m, slot)] = (cv.vsol, cv.vtok)
        col.on_logs(slot, logs(b))

    trade(100, G, True, 10**9)            # the followed wallet buys: the copy lands at slot 104 or later, as live buys did
    trade(101, X, True, 2 * 10**9)        # not due yet
    trade(103, Y, True, 10**9)            # nor yet
    trade(104, G, True, 10**9)            # due: the copy buys at the state Y left; this second buy is not copied
    trade(120, G, False, None)            # the wallet sells: the copy sells at slot 123 or later
    trade(122, X, False, None)            # not due yet
    trade(125, Z, True, 10**9)            # due: the copy sells at the state X's sell left
    fills = col.c.execute("SELECT side, trigger_slot, land_slot, pnl, timed_out FROM pfills ORDER BY id").fetchall()
    assert [f[:3] for f in fills] == [("buy", 100, 104), ("sell", 120, 123)]
    assert abs(fills[1][3] - copy_trade(states[(mint, 103)], states[(mint, 122)], PAPER_STAKE_SOL, TX_COST_SOL)) < 1e-6

    quiet, cv2 = bytes([10]) * 32, Curve()
    trade(200, G, True, 10**9, m=quiet, cv=cv2)   # nothing trades after this: the timeout lands the copy
    col.paper.tick()
    assert col.c.execute("SELECT COUNT(*) FROM pfills").fetchone()[0] == 2           # too early
    for a in col.paper.pending[b58(quiet)]:
        a["t"] -= 10
    col.paper.tick()
    assert col.c.execute("SELECT side, timed_out FROM pfills ORDER BY id DESC LIMIT 1").fetchone() == ("buy", 1)
    s = col.paper.summary()["wallets"][0]
    assert (s["copied"], s["closed"], s["open"]) == (2, 1, 1) and s["golden_now"]
    col.c.close()


def test_followed_wallet_own_trades_since_following_and_chart_points(tmp_path):
    col = Collector(tmp_path / "pump.db")
    G, X, old, new = (bytes([i]) * 32 for i in (90, 91, 11, 12))
    c_old, c_new = Curve(), Curve()
    for m in (old, new):
        col.on_logs(10, logs(create_bytes(m, X)))
    tok_old = c_old.buy(10**9)
    col.on_logs(20, logs(trade_bytes(old, G, True, 10**9, tok_old, c_old.vsol, c_old.vtok, ts=1_790_000_020)))   # before it is followed
    col.flush()
    col.c.execute("INSERT INTO follow(wallet, added_at, golden_now, report_copy_roi) VALUES (?, ?, 1, 0.5)", (b58(G), 1_790_000_100))
    col.paper.reload()
    col.on_logs(30, logs(trade_bytes(old, G, False, c_old.sell(tok_old), tok_old, c_old.vsol, c_old.vtok, ts=1_790_000_130)))  # held from before: not its tracked trade
    tok = c_new.buy(10**9)
    col.on_logs(40, logs(trade_bytes(new, G, True, 10**9, tok, c_new.vsol, c_new.vtok, ts=1_790_000_140)))
    sold = c_new.sell(tok // 2)
    col.on_logs(50, logs(trade_bytes(new, G, False, sold, tok // 2, c_new.vsol, c_new.vtok, ts=1_790_000_150)))
    col.last_paper = col.last_snap = 0.0                 # the next flush writes a chart point
    col.flush()
    w = col.paper.summary()["wallets"][0]
    fee = lambda sol: int(sol * 0.0095) + int(sol * 0.003)   # noqa: E731
    left = tok - tok // 2
    held = (c_new.vsol - c_new.vsol * c_new.vtok / (c_new.vtok + left)) * (1 - FEE) / 1e9
    assert abs(w["own_cost"] - (10**9 + fee(10**9)) / 1e9) < 1e-12
    assert abs(w["own_pnl"] - ((sold - fee(sold)) / 1e9 - w["own_cost"] + held)) < 1e-12
    series = paper_series(col.c)[b58(G)]
    assert series[0] == [1_790_000_100, 0.0, 0.0] and len(series) == 2      # 0 when followed, then the 5-minute point
    assert abs(series[1][2] - w["own_roi"]) < 1e-12 and series[1][1] == (w["total"] / PAPER_STAKE_SOL if w["copied"] else 0.0)
    col.c.close()


def test_following_a_coin_maker_buys_every_launch_and_exits_on_its_sell_or_the_timeout(tmp_path):
    db = tmp_path / "pump.db"
    col = Collector(db)
    dev, X = bytes([120]) * 32, bytes([121]) * 32
    now, states = int(time.time()) - 3600, {}            # an hour ago: their windows have closed

    def launch(k, slot, dev_sells):
        m, cv = bytes([130 + k]) * 32, Curve()
        col.on_logs(slot, logs(create_bytes(m, dev, ts=now)))
        tok = cv.buy(10**9)                                  # a sniper buys in the creation slot: that is our entry price
        col.on_logs(slot, logs(trade_bytes(m, X, True, 10**9, tok, cv.vsol, cv.vtok, ts=now)))
        states[(k, "entry")] = (cv.vsol, cv.vtok)
        dev_tok = cv.buy(2 * 10**8)                          # the dev's own bag, bought two slots later
        col.on_logs(slot + 2, logs(trade_bytes(m, dev, True, 2 * 10**8, dev_tok, cv.vsol, cv.vtok, ts=now + 1)))
        states[(k, "beforesell")] = (cv.vsol, cv.vtok)
        if dev_sells:
            col.on_logs(slot + 20, logs(trade_bytes(m, dev, False, cv.sell(dev_tok), dev_tok, cv.vsol, cv.vtok, ts=now + 60)))
            states[(k, "afterdump")] = (cv.vsol, cv.vtok)
            col.on_logs(slot + 40, logs(trade_bytes(m, X, False, cv.sell(tok), tok, cv.vsol, cv.vtok, ts=now + 120)))
        return m

    launch(0, 1000, True)
    launch(1, 2000, True)
    launch(2, 3000, False)                                   # never sells: the copy has to time out
    col.flush()
    assert settle_launches(db, latency_slots=2, stake_sol=0.1, hold_s=300) == 3      # the window has closed on all three
    assert settle_launches(db, latency_slots=2, stake_sol=0.1, hold_s=300) == 0      # and they are frozen, not redone
    maker = maker_table(col.c, stake_sol=0.1, tx_cost_sol=TX_COST_SOL, min_launches=3)[0][0]
    assert (maker["addr"], maker["launches"], maker["replayed"]) == (b58(dev), 3, 3)
    assert maker["dump_share"] == 2 / 3 and maker["dump_min"] == 1.0                 # 60 s from launch to its first sell
    # it lands at the price the creation-slot sniper left, and gets out two slots after the dev's sell: after the dump
    dumped = sum(copy_trade(states[(k, "entry")], states[(k, "afterdump")], 0.1, TX_COST_SOL, 0.0125, 0.0125) for k in (0, 1))
    timed_out = copy_trade(states[(2, "entry")], states[(2, "beforesell")], 0.1, TX_COST_SOL, 0.0125, 0.0125)
    assert abs(maker["pnl_sol"] - (dumped + timed_out)) < 1e-9
    assert dumped / 2 < timed_out                              # selling into the dev's dump is the expensive exit
    col.c.close()


def test_each_exit_rule_leaves_at_its_own_price(tmp_path):
    col = Collector(tmp_path / "pump.db")
    dev, X = bytes([140]) * 32, bytes([141]) * 32
    now, m, cv, held = int(time.time()), bytes([150]) * 32, Curve(), {}

    def trade(slot, who, buy, sol=None):
        if buy:
            tok = cv.buy(sol)
            held[who] = held.get(who, 0) + tok
            col.on_logs(slot, logs(trade_bytes(m, who, True, sol, tok, cv.vsol, cv.vtok, ts=now)))
        else:
            tok = held.pop(who)
            col.on_logs(slot, logs(trade_bytes(m, who, False, cv.sell(tok), tok, cv.vsol, cv.vtok, ts=now)))
        return cv.vsol, cv.vtok

    A, B = bytes([142]) * 32, bytes([143]) * 32
    col.on_logs(500, logs(create_bytes(m, dev, ts=now)))
    entry = trade(500, X, True, 10**9)                   # the creation-slot sniper: the price our entry lands on
    at_505 = trade(505, A, True, 45 * 10**8)             # pushes us past +20%, not yet +50%
    at_510 = trade(510, B, True, 5 * 10**9)              # and past +50%
    trade(515, dev, True, 2 * 10**9)                     # the dev buys its own bag
    trade(520, dev, False)                               # and dumps it: our copy only gets out two slots later
    trade(521, B, False)
    after_dump = trade(521, A, False)                    # the holders follow it out: that is what the copy sells into
    at_end = trade(600, bytes([146]) * 32, True, 10**8)  # the last trade of the window: where "hold" ends up
    col.flush()

    mint = col.c.execute("SELECT id FROM mints WHERE addr = ?", (b58(m),)).fetchone()[0]
    dev_id = col.c.execute("SELECT id FROM wallets WHERE addr = ?", (b58(dev),)).fetchone()[0]
    res = strategy_sim(col.c, mint, 500, dev_id, latency_slots=2, stake_sol=0.1, tx_cost_sol=TX_COST_SOL, hold_s=300)
    tokens = entry[1] - entry[0] * entry[1] / (entry[0] + 0.1 * 1e9 / 1.0125)
    out = lambda st, n_tx=2, tok=tokens: (st[0] - st[0] * st[1] / (st[1] + tok)) * (1 - 0.0125) / 1e9 - 0.1 - n_tx * TX_COST_SOL  # noqa: E731

    assert abs(res["tp20"] - out(at_505)) < 1e-9         # triggered at 505, lands on the state that trade left
    assert abs(res["tp50"] - out(at_510)) < 1e-9
    assert abs(res["copy"] - out(after_dump)) < 1e-9     # out two slots after the dev's sell: into its dump
    assert abs(res["hold"] - out(at_end)) < 1e-9
    assert res["tp50"] > res["tp20"] > res["copy"]       # leaving before the dev did is what pays here
    assert res["copy"] < res["breakeven"] < res["tp50"]  # the stake came back early, the rest rode down with the dev
    col.c.close()


def test_a_later_entry_buys_at_60_s_or_8_sol_and_sells_two_minutes_later(tmp_path):
    db = tmp_path / "pump.db"
    col = Collector(db)
    dev, m, cv, held = bytes([160]) * 32, bytes([161]) * 32, Curve(), {}
    now = int(time.time()) - 3600                             # an hour ago: its window has closed

    def trade(slot, dt, who, buy, sol=None):
        if buy:
            tok = cv.buy(sol)
            held[who] = held.get(who, 0) + tok
            b = trade_bytes(m, who, True, sol, tok, cv.vsol, cv.vtok, ts=now + dt)
        else:
            tok = held.pop(who)
            b = trade_bytes(m, who, False, cv.sell(tok), tok, cv.vsol, cv.vtok, ts=now + dt)
        col.on_logs(slot, logs(b))
        return cv.vsol, cv.vtok

    X, A, B, C, D, F = (bytes([170 + i]) * 32 for i in range(6))
    col.on_logs(1000, logs(create_bytes(m, dev, ts=now)))
    trade(1000, 0, X, True, 10**9)                            # 31 SOL in the virtual curve
    trade(1100, 40, A, True, 3 * 10**9)                       # 34
    at_55s = trade(1150, 55, B, True, 5 * 10**9)              # 39: past 8 SOL bought, and the last price before 60 s
    at_100s = trade(1300, 100, C, True, 5 * 10**9)            # 44: the 60 s position is up a little over 20 %
    at_120s = trade(1350, 120, D, True, 12 * 10**9)           # 56: and over 50 %
    at_170s = trade(1500, 170, A, False)                      # the last trade before 60 s + 2 min
    trade(1700, 300, F, True, 10**8)
    col.flush()
    mint, dev_id = (col.c.execute(q, (b58(k),)).fetchone()[0] for q, k in
                    (("SELECT id FROM mints WHERE addr = ?", m), ("SELECT id FROM wallets WHERE addr = ?", dev)))
    st = strategy_states(col.c, mint, 1000, now, dev_id, latency_slots=2, stake_sol=0.1, hold_s=900)
    assert st["s60"] == st["c8"] == at_55s                    # the 60 s entry and the 8 SOL entry land on the same price here
    assert (st["e20"], st["e50"]) == (at_100s, at_120s)
    assert st["x180"] == st["c8x"] == at_170s
    tokens = at_55s[1] - at_55s[0] * at_55s[1] / (at_55s[0] + 0.1 * 1e9 / 1.0125)
    out = lambda s: (s[0] - s[0] * s[1] / (s[1] + tokens)) * (1 - 0.0125) / 1e9 - 0.1 - 2 * TX_COST_SOL  # noqa: E731
    res = launch_pnl(st, 0.1, TX_COST_SOL)
    assert abs(res["late60_2m"] - out(at_170s)) < 1e-9 and abs(res["sol8_2m"] - out(at_170s)) < 1e-9
    assert abs(res["late60_tp20"] - out(at_100s)) < 1e-9 and abs(res["late60_tp50"] - out(at_120s)) < 1e-9
    assert res["late60_2m_held"] == res["late60_2m"]         # the maker never sold
    assert "late60_2m_held" not in launch_pnl({**st, "dev_sold_s": 30}, 0.1, TX_COST_SOL)   # it had dumped by 60 s: no entry
    # a launch settled before these rules (or before the 60 s state, the late60 entry) gets them while its trades are here
    assert settle_launches(db, latency_slots=2, stake_sol=0.1, hold_s=900) == 1
    for late in (None, 1):                                    # never done, and done by the first backfill, without s60
        col.c.execute("UPDATE launches SET late = ?, s60_vsol = NULL, s60_vtok = NULL, x180_vsol = NULL, x180_vtok = NULL", (late,))
        col.c.commit()
        settle_launches(db, latency_slots=2, stake_sol=0.1, hold_s=900)
        assert col.c.execute("SELECT late, s60_vsol, s60_vtok, x180_vsol, x180_vtok FROM launches").fetchone() == (2, *at_55s, *at_170s)
    col.c.close()


def test_the_8_sol_entry_waits_for_sol_bought_not_a_mayhem_agents_reserve_shift(tmp_path):
    """On a mayhem coin pump.fun's agent trades at the curve's price, then moves its virtual SOL (mainnet 2026-09-30: a
    0.12 SOL sell took 3fHgqgak from 32.9 to 23.6 virtual SOL), so virtual SOL over 30 is not what was bought."""
    col = Collector(tmp_path / "pump.db")
    dev, m, agent, A, B, C = (bytes([190 + i]) * 32 for i in range(6))
    now = int(time.time()) - 3600

    def trade(slot, dt, who, buy, sol, vsol, vtok):
        col.on_logs(slot, logs(trade_bytes(m, who, buy, sol, 10**12, vsol, vtok, ts=now + dt, mayhem=True, fees=who != agent)))
        return vsol, vtok

    col.on_logs(1000, logs(create_bytes(m, dev, ts=now)))
    trade(1001, 1, A, True, 10**9, 31 * 10**9, 1_038 * 10**12)                    # 1 SOL bought
    pumped = trade(1010, 5, agent, True, 5 * 10**7, 45 * 10**9, 1_037 * 10**12)   # the agent's shift: 45 virtual, 1.05 bought
    trade(1020, 10, agent, False, 10**8, 12 * 10**9, 1_040 * 10**12)              # and down to 12: 0.95 bought
    trade(1100, 30, B, True, 4 * 10**9, 16 * 10**9, 780 * 10**12)                 # 4.95 bought
    bought8 = trade(1200, 50, C, True, 32 * 10**8, 19_200_000_000, 644 * 10**12)  # 8.15 bought, still under 38 virtual
    trade(1300, 80, A, False, 5 * 10**8, 18_700_000_000, 645 * 10**12)            # the first sell that pays a fee
    col.flush()
    mint, dev_id = (col.c.execute(q, (b58(k),)).fetchone()[0] for q, k in
                    (("SELECT id FROM mints WHERE addr = ?", m), ("SELECT id FROM wallets WHERE addr = ?", dev)))
    st = strategy_states(col.c, mint, 1000, now, dev_id, latency_slots=2, stake_sol=0.1, hold_s=900)
    assert pumped[0] >= 38 * 10**9 > bought8[0]
    assert st["c8"] == bought8                                # not the agent's pump, where virtual SOL first passed 38
    assert "sol8_2m" in launch_pnl(st, 0.1, TX_COST_SOL)
    assert abs(st["fee_in"] - 0.0125) < 1e-12 and abs(st["fee_out"] - 0.0125) < 1e-12   # the coin's fee, not the agent's 0
    col.c.close()


def test_a_launch_is_only_in_the_crew_cohort_once_the_crew_was_already_known():
    crew, bot = ["11", "22", "33"], "99"
    rows = [{"creator": "A", "buyers": ",".join(crew), "ts": t} for t in range(5)]        # the crew builds its record
    rows += [{"creator": "B", "buyers": ",".join(crew), "ts": 10},                        # it moves to a fresh wallet
             {"creator": "B", "buyers": ",".join(crew), "ts": 11},                        # this is the coin to trade
             {"creator": "C", "buyers": f"{bot},77", "ts": 12}]                           # a bot and a stranger: nothing
    co = cohorts_of(rows)
    assert [r["ts"] for r in co["first_coin"]] == [10]        # B's first coin only confirms the crew moved
    assert [r["ts"] for r in co["crew_2nd"]] == [4, 11]       # from the second coin of a known wallet on
    assert [r["ts"] for r in co["crew"]] == [4, 10, 11] and len(co["all"]) == 8
    assert not cohorts_of(rows, seen_min=9)["crew"]           # a crew that green does not count yet
    busy = [{"creator": f"X{i}", "buyers": "99,88", "ts": i} for i in range(40)]
    assert not cohorts_of(busy)["crew"]                       # one snipe per maker, forever: never a crew
    assert _best([{"rule": "tp50", "roi": 0.057}, {"rule": "hold", "roi": -0.1}]) == "tp50 +5.7%"   # the log line
    assert _best(None) == "n/a"                               # an empty cohort still gets its slot in the line


def test_a_dossier_puts_a_wallets_own_trades_next_to_its_copies(tmp_path):
    from hl_screener.pumpdossier import dossiers
    from hl_screener.pumpfun import connect
    db, L = tmp_path / "pump.db", 10**9
    c = connect(db)
    c.executemany("INSERT INTO wallets(id, addr) VALUES (?, ?)", [(1, "G"), (2, "M")])
    c.executemany("INSERT INTO mints(id, addr, slot, ts, creator) VALUES (?, ?, ?, ?, 2)", [(1, "coin1", 100, 1000), (2, "coin2", 200, 2000)])
    c.executemany("INSERT INTO trades(slot, ts, mint, wallet, buy, sol, tok, fee, vsol, vtok) VALUES (?,?,?,?,?,?,?,?,?,?)", [
        (101, 1000, 1, 1, 1, L, 1000, 0, 32 * L, 1),         # a slot after launch, 1 SOL already in the curve
        (110, 1004, 1, 1, 0, 2 * L, 1000, 0, 30 * L, 1),     # out 4 s later for 2 SOL: +1
        (260, 2030, 2, 1, 1, L, 500, 0, 40 * L, 1),          # 60 slots after launch
        (270, 2040, 2, 1, 0, L // 2, 500, 0, 39 * L, 1)])    # -0.5
    c.execute("INSERT INTO follow(wallet, added_at, golden_now, golden_ever) VALUES ('G', 0, 1, 1)")
    c.executemany("INSERT INTO pfills(wallet, mint, side, trigger_slot, land_slot, sol, slip_bps, pnl, timed_out) VALUES (?,?,?,?,?,?,?,?,?)",
                  [("G", "coin1", "buy", 101, 103, 0.1, 150.0, None, 0), ("G", "coin1", "sell", 110, 112, 0.18, 80.0, 0.07, 0)])
    c.commit()
    c.close()
    out = "\n".join(dossiers(db))
    assert "dossier G (golden now)" in out
    assert "2 closed +0.500 SOL on 2.00 SOL spent (+25.0%), won +50.0%" in out
    assert "0-1 slots after launch 50%" in out and "11-150 slots after launch 50%" in out
    assert "1 copies, 1 closed, +0.070 SOL on 0.10 SOL (+70.0%)" in out and "delay p50 2 slots" in out
    assert "the wallet on those same 1 coins: +1.000 SOL (+100.0%)" in out     # what the copy left on the table
    c = connect(db)
    c.execute("INSERT INTO trades(slot, ts, mint, wallet, buy, sol, tok, fee, vsol, vtok) VALUES (280, 2050, 2, 1, 0, ?, 500, 0, 38 * ?, 1)", (L, L))
    c.commit()
    c.close()
    out = "\n".join(dossiers(db))                                                # coin2: 500 more sold than bought
    assert "+0.750 SOL on 2.00 SOL spent" in out and "1 sold more than it bought" in out   # 1.5 SOL x 500/1000 it bought


def test_a_dossier_of_a_wallet_that_closed_nothing_yet_still_prints(tmp_path):
    from hl_screener.pumpdossier import dossiers
    from hl_screener.pumpfun import connect
    db, L = tmp_path / "pump.db", 10**9
    c = connect(db)
    c.executemany("INSERT INTO wallets(id, addr) VALUES (?, ?)", [(1, "G"), (2, "M")])
    c.execute("INSERT INTO mints(id, addr, slot, ts, creator) VALUES (1, 'coin1', 100, 1000, 2)")
    c.execute("INSERT INTO trades(slot, ts, mint, wallet, buy, sol, tok, fee, vsol, vtok) VALUES (101, 1000, 1, 1, 1, ?, 1000, 0, ?, 1)",
              (L, 31 * L))
    c.execute("INSERT INTO follow(wallet, added_at, golden_now, golden_ever) VALUES ('G', 0, 1, 1)")
    c.commit()
    c.close()
    assert "0 closed +0.000 SOL on 0.00 SOL spent (n/a), won n/a, median n/a SOL" in "\n".join(dossiers(db))   # 2026-09-26: it crashed the startup log


def test_a_dossier_counts_the_sol_bought_before_an_entry_not_virtual_sol_over_30(tmp_path):
    from hl_screener.pumpdossier import dossiers
    from hl_screener.pumpfun import connect
    db, L = tmp_path / "pump.db", 10**9
    c = connect(db)
    c.executemany("INSERT INTO wallets(id, addr) VALUES (?, ?)", [(1, "G"), (2, "M"), (3, "A"), (4, "agent")])
    c.execute("INSERT INTO mints(id, addr, slot, ts, creator) VALUES (1, 'coin1', 100, 1000, 2)")
    c.executemany("INSERT INTO trades(slot, ts, mint, wallet, buy, sol, tok, fee, vsol, vtok) VALUES (?,?,?,?,?,?,?,?,?,?)", [
        (101, 1000, 1, 3, 1, L, 1000, 12_500_000, 31 * L, 1),   # 1 SOL bought
        (102, 1001, 1, 4, 1, L // 20, 10, 0, 45 * L, 1),        # a mayhem coin's agent lifts virtual SOL to 45 on 0.05 SOL
        (103, 1002, 1, 4, 0, L // 10, 10, 0, 12 * L, 1),        # and drops it to 12: 0.95 SOL really in the curve
        (110, 1005, 1, 3, 1, L // 2, 500, 6_250_000, 25 * L // 2, 1),   # 0.5 more, earlier in G's own slot: 1.45
        (110, 1005, 1, 1, 1, L, 1000, 12_500_000, 27 * L // 2, 1)])     # G's entry: virtual SOL over 30 says -17.5
    c.execute("INSERT INTO follow(wallet, added_at, golden_now, golden_ever) VALUES ('G', 0, 1, 1)")
    c.commit()
    c.close()
    assert "SOL already in the curve p50 1.45" in "\n".join(dossiers(db))


def test_maker_wallets_are_linked_by_the_crew_that_snipes_them():
    def launch(creator, buyers, ts=0):
        return {"creator": creator, "buyers": ",".join(buyers), "ts": ts}

    crew, bot = ["11", "22", "33"], "99"                     # its own wallets, and a sniper bot buying everything
    rows = [launch("A", crew), launch("A", crew), launch("B", crew), launch("B", crew + ["44"])]
    rows += [launch(f"X{i}", [bot, str(1000 + i)]) for i in range(25)]      # the bot's rounds link nothing
    g = operator_groups(rows, min_shared=3)
    assert g["A"] == g["B"]                                  # three shared snipers: one hand behind both wallets
    assert len({g[f"X{i}"] for i in range(25)}) == 25        # the bot is too busy to mean anything
    assert g["A"] in ("A", "B")                              # the group is named after one of its own wallets

    thin = [launch("C", ["11", "22"]), launch("D", ["11", "22", "55"])]     # only two in common
    assert operator_groups(thin, min_shared=3)["C"] != operator_groups(thin, min_shared=3)["D"]


def test_a_copy_pays_signature_priority_and_tip_on_both_transactions():
    entry, exit_ = (31 * 10**9, 10**15), (36 * 10**9, 9 * 10**14)
    assert abs(TX_COST_SOL - (BASE_FEE_SOL + PRIORITY_SOL + TIP_SOL)) < 1e-12 and TX_COST_SOL >= 0.001
    free = copy_trade(entry, exit_, 0.1, 0.0)
    assert abs(free - copy_trade(entry, exit_, 0.1, TX_COST_SOL) - 2 * TX_COST_SOL) < 1e-12   # in and out
    assert 2 * TX_COST_SOL / 0.1 > 0.02                          # over 2% of the stake: it has to show up in the ranking


def test_the_fee_free_mayhem_agent_is_not_a_sniper_to_copy(tmp_path):
    db = tmp_path / "pump.db"
    col = Collector(db)
    A, agent, dev = (bytes([i]) * 32 for i in (104, 105, 106))
    now = int(time.time())
    for k in range(3):                                   # the agent snipes three tokens without a fee, A two with one
        m, cv = bytes([120 + k]) * 32, Curve()
        col.on_logs(1000 + 100 * k, logs(create_bytes(m, dev, ts=now)))
        for who, fees in ((agent, False), (A, True))[:2 if k < 2 else 1]:
            tok = cv.buy(10**8)
            col.on_logs(1001 + 100 * k, logs(trade_bytes(m, who, True, 10**8, tok, cv.vsol, cv.vtok, ts=now, mayhem=True, fees=fees)))
    col.flush()
    assert [a for a, _ in update_snipers(db, top_n=2, window_h=24)] == [b58(A)]


def test_top_snipers_rotate_and_a_dropped_one_still_exits(tmp_path):
    db = tmp_path / "pump.db"
    col = Collector(db)
    A, B, C, dev = (bytes([i]) * 32 for i in (100, 101, 102, 103))
    now, curves, held = int(time.time()), {}, {}

    def token(k, slot):
        m = bytes([110 + k]) * 32
        curves[m] = Curve()
        col.on_logs(slot, logs(create_bytes(m, dev, ts=now)))
        return m

    def buy(m, who, slot, sol=10**8):
        tok = curves[m].buy(sol)
        held[(who, m)] = held.get((who, m), 0) + tok
        col.on_logs(slot, logs(trade_bytes(m, who, True, sol, tok, curves[m].vsol, curves[m].vtok, ts=now)))

    def sell(m, who, slot):
        tok = held.pop((who, m))
        col.on_logs(slot, logs(trade_bytes(m, who, False, curves[m].sell(tok), tok, curves[m].vsol, curves[m].vtok, ts=now)))

    for k in range(4):                                   # A snipes four tokens, B two; the creator never counts
        m = token(k, 1000 + 100 * k)
        buy(m, A, 1000 + 100 * k + 1)
        if k < 2:
            buy(m, B, 1000 + 100 * k + 2)
    col.flush()
    assert [a for a, _ in update_snipers(db, top_n=2, window_h=24)] == [b58(A), b58(B)]
    col.paper.reload()

    m7 = token(10, 1300)                                 # B is in the set: its buy is copied
    buy(m7, B, 1301)
    buy(m7, dev, 1305)                                   # a later trade lands the copy
    assert col.c.execute("SELECT COUNT(*) FROM pfills").fetchone()[0] == 1

    for k in range(4, 9):                                # C takes the top, B drops out of it
        m = token(k, 1400 + 10 * k)
        buy(m, C, 1400 + 10 * k + 1)
    col.flush()
    assert [a for a, _ in update_snipers(db, top_n=2, window_h=24)] == [b58(C), b58(A)]
    col.paper.reload()
    assert b58(B) not in col.paper.follow and col.c.execute("SELECT COUNT(*) FROM follow").fetchone()[0] == 3

    m8 = token(20, 1500)
    buy(m8, B, 1501)                                     # dropped: no new copy
    buy(m8, dev, 1505)
    sell(m7, B, 1510)                                    # the copy it already opened still exits
    buy(m7, dev, 1515)
    assert col.c.execute("SELECT side, mint FROM pfills WHERE wallet = ? ORDER BY id", (b58(B),)).fetchall() \
        == [("buy", b58(m7)), ("sell", b58(m7))]
    col.c.close()


def test_pumpswap_events_decode_to_post_trade_reserves():
    pool, user = bytes([3]) * 32, bytes([4]) * 32
    B, Q, vq = 10**15, 85 * 10**9, 7 * 10**9
    buy = parse_amm_trade(amm_bytes(True, pool, user, 10**12, B, Q, 10**8, 2 * 10**5, 10**8 + 2 * 10**5, 10**8 + 5 * 10**5, vq, "buy_exact_quote_in"))
    assert (buy["vtok"], buy["vsol"], buy["fee"], buy["sol"]) == (B - 10**12, Q + 10**8 + 2 * 10**5 + vq, 5 * 10**5, 10**8)
    sell = parse_amm_trade(amm_bytes(False, pool, user, 10**12, B, Q, 10**8, 2 * 10**5, 10**8 - 2 * 10**5, 10**8 - 6 * 10**5, vq))
    assert (sell["vtok"], sell["vsol"], sell["fee"]) == (B + 10**12, Q - 10**8 + 2 * 10**5 + vq, 6 * 10**5)
    assert not sell["buy"] and buy["buy"] and buy["pool"] == pool
    small = amm_bytes(False, pool, user, 1, 1, 1, 1, 0, 1, 1)
    assert parse_amm_trade(small + bytes(8)) == parse_amm_trade(small) and parse_amm_trade(small[:-1]) is None
    moved = small[:8 + 8 + 13 * 8] + bytes(1) + small[8 + 8 + 13 * 8:] + bytes(8)       # a byte before the pool, and the tail:
    assert parse_amm_trade(moved) is None                                                # 9 bytes over is no known length


def test_collector_follows_graduated_tokens_onto_pumpswap(tmp_path):
    col = Collector(tmp_path / "pump.db")
    mint, dev, W, X, pool = bytes([50]) * 32, bytes([51]) * 32, bytes([52]) * 32, bytes([53]) * 32, bytes([54]) * 32
    col.c.execute("INSERT INTO follow(wallet, added_at, golden_now, report_copy_roi) VALUES (?, 0, 1, NULL)", (b58(W),))
    col.paper.reload()
    col.on_logs(400, logs(create_bytes(mint, dev)), "sig-create")
    col.on_logs(500, logs(pool_bytes(pool, mint, b58decode(WSOL)), AMM_PROGRAM), "sig-migrate")
    assert col.pools == {b58(pool): b58(mint)} and col.stats["graduated"] == 1
    assert b58(pool) in col.pool_feed.seen               # subscribed on its own from now on, not all of PumpSwap
    B, Q = 10**15, 85 * 10**9
    w_buy = logs(amm_bytes(True, pool, W, 10**12, B, Q, 10**8, 2 * 10**5, 10**8 + 2 * 10**5, 10**8 + 5 * 10**5), AMM_PROGRAM)
    col.on_logs(510, w_buy, "sig-w")
    col.on_logs(510, w_buy, "sig-w")                     # the same transaction again, from the other subscription
    col.on_logs(511, logs(amm_bytes(True, bytes([99]) * 32, X, 5, B, Q, 5, 0, 5, 5), AMM_PROGRAM), "sig-other-pool")
    B2, Q2 = B - 10**12, Q + 10**8 + 2 * 10**5
    col.on_logs(514, logs(amm_bytes(True, pool, X, 10**11, B2, Q2, 10**7, 2 * 10**4, 10**7 + 2 * 10**4, 10**7 + 5 * 10**4), AMM_PROGRAM), "sig-x")
    col.flush()
    rows = col.c.execute("SELECT slot, sol, tok, fee, vsol, vtok FROM trades ORDER BY slot").fetchall()
    assert rows[0] == (510, 10**8, 10**12, 5 * 10**5, Q2, B2) and len(rows) == 2 and col.stats["amm_trades"] == 2
    copy = col.c.execute("SELECT side, trigger_slot, land_slot FROM pfills").fetchall()
    assert copy == [("buy", 510, 514)]                   # W's PumpSwap buy was copied at the pool's reserves
    assert col._pinned_pools() == {b58(pool)}            # a pool with an open copy keeps its subscription
    col.c.close()


def test_a_followed_wallets_trade_in_a_pool_we_do_not_follow_is_copied_and_the_pool_followed(tmp_path):
    """The live test's first hours: 3 of the leader's 4 PumpSwap first buys were in pools the collector did not follow
    (quiet for over an hour, migrated while the feed was down, or a coin older than the tracked window): never seen.
    The wallet's own subscription brings them; the pool's account names the coin."""
    import threading
    import types

    from hl_screener.pumptx import canonical_pool
    col = Collector(tmp_path / "pump.db")
    W, X, mint, other = bytes([56]) * 32, bytes([57]) * 32, bytes([58]) * 32, bytes([59]) * 32
    pool, odd = canonical_pool(b58(mint)), bytes([60]) * 32                 # an old coin's pool, and someone else's pool
    col.c.execute("INSERT INTO follow(wallet, added_at, golden_now, report_copy_roi) VALUES (?, 0, 1, NULL)", (b58(W),))
    col.paper.reload()
    reads, slow = [], threading.Event()

    def account(addr):                                    # the pool's account: its base mint at byte 43
        reads.append(addr)
        slow.wait(5)
        return AMM_PROGRAM, bytes(43) + (mint if addr == pool else other) + b58decode(WSOL) + bytes(186)

    col.rpc = types.SimpleNamespace(account=account)
    B, Q = 10**15, 85 * 10**9
    B2, Q2 = B - 10**12, Q + 10**8 + 2 * 10**5            # what W's buy leaves
    B3, Q3 = B2 - 10**11, Q2 + 10**7 + 2 * 10**4          # and X's after it
    w_buy = logs(amm_bytes(True, b58decode(pool), W, 10**12, B, Q, 10**8, 2 * 10**5, 10**8 + 2 * 10**5, 10**8 + 5 * 10**5), AMM_PROGRAM)
    x_buy = lambda slot, b, q, sig: col.on_logs(slot, logs(amm_bytes(True, b58decode(pool), X, 10**11, b, q, 10**7, 2 * 10**4,   # noqa: E731
                                                                     10**7 + 2 * 10**4, 10**7 + 5 * 10**4), AMM_PROGRAM), sig)
    col.on_logs(510, w_buy, "sig-w")                     # on W's own subscription: a pool nobody follows
    col.on_logs(511, logs(amm_bytes(True, odd, X, 5, B, Q, 5, 0, 5, 5), AMM_PROGRAM), "sig-x-odd")   # X is not followed: not read
    x_buy(512, B2, Q2, "sig-x1")                         # the pool's next trade, while its coin is read: it waits, in order
    assert list(col.finding) == [pool] and len(col.finding[pool][1]) == 2 and col.pools == {}
    slow.set()
    col.finding[pool][0].result(timeout=5)
    col.on_logs(510, w_buy, "sig-w")                     # the same transaction again, from the pool's own subscription
    assert col.pools == {pool: b58(mint)} and col.stats["pools_found"] == 1 and col.stats["amm_trades"] == 2
    assert pool in col.pool_feed.wanted() and pool not in col.pool_feed.seen   # an old coin's pool: there while a copy is open
    x_buy(514, B3, Q3, "sig-x2")
    (side, trigger, land, tok), = col.c.execute("SELECT side, trigger_slot, land_slot, tok FROM pfills").fetchall()
    assert (side, trigger, land) == ("buy", 510, 514)    # W's first buy, copied once, at the reserves X's buy left after it
    assert abs(tok - (B3 - Q3 * B3 / (Q3 + PAPER_STAKE_SOL * 10**9 / 1.005))) < 1
    assert col.c.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0   # a coin born before the window: not stored
    col.on_logs(520, logs(amm_bytes(True, odd, W, 5, B, Q, 5, 0, 5, 5), AMM_PROGRAM), "sig-w-odd")   # not pump.fun's pool
    col.finding[b58(odd)][0].result(timeout=5)
    col.on_logs(521, logs(amm_bytes(True, odd, W, 5, B, Q, 5, 0, 5, 5), AMM_PROGRAM), "sig-w-odd2")
    assert reads == [pool, b58(odd)] and b58(odd) not in col.pools and not col.finding   # read once, then left alone
    assert col.c.execute("SELECT COUNT(*) FROM pfills").fetchone()[0] == 1
    col.c.close()


def test_a_wallet_we_hold_a_copy_of_is_read_past_the_cap_and_a_failed_read_checks_its_copies(tmp_path, monkeypatch):
    """A coin we hold migrated while the feed was down: its wallet's sell comes in a pool we do not follow. It must not
    be the trade the read cap turns away, and a read that fails must not leave the copy open until MAX_HOLD_S."""
    import types

    from hl_screener import pumpfun
    monkeypatch.setattr(pumpfun, "POOL_READS", 0)          # the cap reached
    monkeypatch.setattr(pumpfun, "GAP_PACE_S", 0.01)
    col = Collector(tmp_path / "pump.db")
    W, V, pool = bytes([66]) * 32, bytes([67]) * 32, bytes([68]) * 32
    for w in (W, V):
        col.c.execute("INSERT INTO follow(wallet, added_at, golden_now, report_copy_roi) VALUES (?, 0, 1, NULL)", (b58(w),))
    col.paper.reload()
    col.paper.pos[(b58(W), "COIN")] = [10**12, 0.25, int(time.time())]   # a copy of W is open

    def account(addr):
        raise RuntimeError("HTTP 429")

    col.rpc, checks = types.SimpleNamespace(account=account), []
    col.check_gap = lambda why, mints=None, wallet=None: checks.append((why, mints, wallet))
    B, Q = 10**15, 85 * 10**9
    sell = lambda who, sig: col.on_logs(530, logs(amm_bytes(False, pool, who, 10**12, B, Q, 10**8, 2 * 10**5,   # noqa: E731
                                                            10**8 - 2 * 10**5, 10**8 - 6 * 10**5), AMM_PROGRAM), sig)
    sell(V, "sig-v")                                       # nothing of V's held: turned away by the cap
    assert not col.finding and col.stats["pool_reads_full"] == 1
    sell(W, "sig-w")                                       # W's, maybe the sell of our copy: read all the same
    assert col.finding[b58(pool)][0].exception(timeout=5) is not None
    col.on_logs(531, [], "sig-next")                       # the failed read is collected: W's copies are checked
    assert checks == [("pool feed drop", None, b58(W))] and not col.finding
    col.c.close()


def test_a_pool_migrated_while_the_feed_was_down_is_read_through_a_lagging_node_and_stored(tmp_path, monkeypatch):
    from hl_screener import pumpfun
    from hl_screener.pumptx import canonical_pool

    class Rpc:                                            # answers in turn: an account, None (unknown yet) or an error
        def __init__(self, *answers):
            self.answers = list(answers)

        def account(self, addr):
            a = self.answers.pop(0)
            if isinstance(a, Exception):
                raise a
            return a

    monkeypatch.setattr(pumpfun, "GAP_PACE_S", 0.01)
    col = Collector(tmp_path / "pump.db")
    W, dev, mint, unknown = bytes([61]) * 32, bytes([62]) * 32, bytes([63]) * 32, bytes([65]) * 32
    pool = canonical_pool(b58(mint))
    col.c.execute("INSERT INTO follow(wallet, added_at, golden_now, report_copy_roi) VALUES (?, 0, 1, NULL)", (b58(W),))
    col.paper.reload()
    col.on_logs(400, logs(create_bytes(mint, dev)), "sig-create")   # a coin we track; its CreatePoolEvent came in a gap
    other = lambda slot: col.on_logs(slot, logs(trade_bytes(bytes([64]) * 32, dev, True, 1, 1, 1, 1)), f"sig-{slot}")   # noqa: E731
    col.rpc = Rpc(None, OSError("HTTP 429"), (AMM_PROGRAM, bytes(43) + mint + b58decode(WSOL) + bytes(186)))
    col.on_logs(600, logs(amm_bytes(False, b58decode(pool), W, 10**12, 10**15, 85 * 10**9, 10**8, 2 * 10**5, 10**8 - 2 * 10**5,
                                    10**8 - 6 * 10**5), AMM_PROGRAM), "sig-w")
    col.finding[pool][0].result(timeout=5)              # not there yet, then refused, then read: the third time
    other(601)                                           # any next message hands the read over
    col.flush()
    assert col.rpc.answers == [] and col.pools == {pool: b58(mint)} and pool in col.pool_feed.seen   # followed, stored
    assert col.c.execute("SELECT pool FROM mints WHERE addr = ?", (b58(mint),)).fetchone()[0] == pool
    assert col.c.execute("SELECT slot, buy FROM trades").fetchall() == [(600, 0)]
    col.rpc = Rpc(None, None, None)                      # a pool no node knows: its trade is said and left
    col.on_logs(602, logs(amm_bytes(True, unknown, W, 5, 10**15, 85 * 10**9, 5, 0, 5, 5), AMM_PROGRAM), "sig-w2")
    assert isinstance(col.finding[b58(unknown)][0].exception(timeout=5), ValueError)
    other(603)
    assert not col.finding and not col.not_ours           # read again on the wallet's next trade there
    col.c.close()


def test_the_followed_wallets_are_subscribed_while_followed_or_holding_a_copy(tmp_path):
    import types
    col = Collector(tmp_path / "pump.db")
    col.c.executemany("INSERT INTO follow(wallet, added_at, golden_ever, sniper_now) VALUES (?, 0, ?, ?)",
                      [("G", 1, 0), ("S", 0, 1), ("S2", 0, 1)])
    col.paper.reload()
    col.paper.pos[("S2", "M")] = [1.0, 0.25, 0]           # S2 has a copy open
    assert {"G", "S", "S2"} <= set(col.pool_feed.wanted())   # one subscription each, though none ever traded in a pool
    col.c.execute("UPDATE follow SET sniper_now = 0")    # the snipers rotate out
    col.paper.reload()
    wanted = col.pool_feed.wanted()
    assert "G" in wanted and "S" not in wanted and "S2" in wanted   # S2 still has its copy to exit
    col.live = types.SimpleNamespace(targets={"L"}, pos={"M2": {"wallet": "L2"}}, _orders=lambda: [])
    assert {"L", "L2"} <= set(col.pool_feed.wanted())    # the live copies' wallets too
    col.live = None
    col.c.close()


def test_collector_stops_storing_while_the_disk_is_nearly_full(tmp_path):
    col = Collector(tmp_path / "pump.db")
    mint, dev, A = bytes([70]) * 32, bytes([71]) * 32, bytes([72]) * 32
    col.on_logs(3000, logs(create_bytes(mint, dev)))
    col.stats["disk_free_gb"] = 0.2
    col.on_logs(3001, logs(trade_bytes(mint, A, True, 10**8, 10**12, 31 * 10**9, 10**15)))
    col.flush()
    count = lambda: col.c.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    assert count() == 0 and col.stats["skipped_low_disk"] == 1 and col.stats["paused_low_disk"]
    col.stats["disk_free_gb"] = 5.0                      # space is back: storing resumes
    col.on_logs(3002, logs(trade_bytes(mint, A, False, 10**8, 10**12, 31 * 10**9, 10**15)))
    col.flush()
    assert count() == 1 and not col.stats["paused_low_disk"]
    col.c.close()


def test_twins_are_wallets_buying_the_same_tokens_in_the_same_slot(tmp_path):
    col = Collector(tmp_path / "pump.db")
    A, B, C, dev = (bytes([i]) * 32 for i in (40, 41, 42, 43))
    for k in range(4):                                   # four launches; A and B always buy together, C never with them
        mint = bytes([60 + k]) * 32
        base = 2000 + 100 * k
        col.on_logs(base, logs(create_bytes(mint, dev)))
        for who, slot in ((A, base + 5), (B, base + 5), (C, base + 9 + k)):
            col.on_logs(slot, logs(trade_bytes(mint, who, True, 10**8, 10**12, 31 * 10**9, 10**15)))
    col.flush()
    ids = dict(col.c.execute("SELECT addr, id FROM wallets"))
    assert twins(col.c, ids[b58(A)]) == 1 and twins(col.c, ids[b58(B)]) == 1 and twins(col.c, ids[b58(C)]) == 0
    col.c.close()
