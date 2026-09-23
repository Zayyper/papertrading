import base64
import struct
import time

from hl_screener.pumpfun import (_B58, AMM_PROGRAM, D_BUY, _best, D_CREATE, D_POOL, D_SELL, D_TRADE, FEE, PUMP_PROGRAM, WSOL, Collector,
                                 BASE_FEE_SOL, PRIORITY_SOL, TIP_SOL, TX_COST_SOL, b58, build_report, copy_trade,
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


def amm_bytes(buy, pool, user, base, B, Q, q, lp, q_net, user_q, vq=0, ix="buy"):
    b = (D_BUY if buy else D_SELL) + struct.pack("<q", 1_790_000_000)
    b += struct.pack("<13Q", base, 0, 0, 0, B, Q, q, 20, lp, 5, 0, q_net, user_q) + pool + user + bytes(32 * 5) + struct.pack("<QQ", 30, 0)
    if buy:
        b += struct.pack("<?QQQqQ", False, 0, 0, 0, 0, 0) + s(ix)
    return b + struct.pack("<4Q", 0, 0, 0, 0) + vq.to_bytes(16, "little", signed=True) + struct.pack("<?QQQ", False, 0, 0, 0)


def pool_bytes(pool, base_mint, quote_mint):
    b = D_POOL + struct.pack("<qH", 0, 0) + bytes(32) + base_mint + quote_mint + struct.pack("<BB7QB", 6, 9, *([0] * 7), 255)
    return b + pool + bytes(32 * 4) + struct.pack("<?Q??", False, 0, False, False)


def s(x: str) -> bytes:
    return struct.pack("<I", len(x)) + x.encode()


def trade_bytes(mint, user, buy, sol, tok, vsol, vtok, ts=1_790_000_000, quote=bytes(32), ix="buy", shareholders=0):
    fee, cfee = int(sol * 0.0095), int(sol * 0.003)
    b = D_TRADE + mint + struct.pack("<QQ?", sol, tok, buy) + user + struct.pack("<qQQ", ts, vsol, vtok)
    b += struct.pack("<QQ", 0, 0) + bytes(32) + struct.pack("<QQ", 95, fee) + bytes(32) + struct.pack("<QQ", 30, cfee)
    b += struct.pack("<?QQQq", True, 0, 0, 0, 0) + s(ix)
    b += struct.pack("<?QQQQ", False, 0, 0, 0, 0) + struct.pack("<I", shareholders) + bytes(34 * shareholders)
    return b + quote + struct.pack("<5Q", 0, 0, 0, 0, 0)


def create_bytes(mint, user, ts=1_790_000_000, quote=bytes(32)):
    b = D_CREATE + s("Cat") + s("CAT") + s("https://x") + mint + bytes(32) + user + bytes(32) + struct.pack("<q", ts)
    return b + struct.pack("<4Q", 0, 0, 0, 0) + bytes(32) + struct.pack("<??", False, False) + quote + struct.pack("<QQ?", 0, 0, False)


def test_parse_exact_layout_or_nothing():
    mint, user = bytes([1]) * 32, bytes([2]) * 32
    e = parse_trade(trade_bytes(mint, user, True, 10**9, 5 * 10**12, 31 * 10**9, 10**15, shareholders=2))
    assert e["buy"] and e["sol"] == 10**9 and e["user"] == user and e["sol_quote"]
    assert e["fee"] == int(10**9 * 0.0095) + int(10**9 * 0.003)
    assert parse_trade(trade_bytes(mint, user, True, 1, 1, 1, 1) + b"\0") is None       # layout drift: refuse
    assert not parse_trade(trade_bytes(mint, user, True, 1, 1, 1, 1, quote=bytes([9]) * 32))["sol_quote"]
    c = parse_create(create_bytes(mint, user))
    assert c["mint"] == mint and c["user"] == user and c["symbol"] == "CAT" and c["sol_quote"]
    assert parse_create(create_bytes(mint, user)[:-1]) is None


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

    trade(100, G, True, 10**9)            # the followed wallet buys: the copy lands at slot 102 or later
    trade(101, X, True, 2 * 10**9)        # not due yet
    trade(103, Y, True, 10**9)            # due: the copy buys at the state X left
    trade(104, G, True, 10**9)            # a second buy of the same token is not copied
    trade(120, G, False, None)            # the wallet sells: the copy sells at slot 122 or later
    trade(125, Z, True, 10**9)            # due: the copy sells at the state G's sell left
    fills = col.c.execute("SELECT side, trigger_slot, land_slot, pnl, timed_out FROM pfills ORDER BY id").fetchall()
    assert [f[:3] for f in fills] == [("buy", 100, 102), ("sell", 120, 122)]
    assert abs(fills[1][3] - copy_trade(states[(mint, 101)], states[(mint, 120)], 0.1, TX_COST_SOL)) < 1e-6

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
    assert abs(series[1][2] - w["own_roi"]) < 1e-12 and series[1][1] == (w["total"] / 0.1 if w["copied"] else 0.0)
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
    assert parse_amm_trade(amm_bytes(False, pool, user, 1, 1, 1, 1, 0, 1, 1) + b"\0") is None


def test_collector_follows_graduated_tokens_onto_pumpswap(tmp_path):
    col = Collector(tmp_path / "pump.db")
    mint, dev, W, X, pool = bytes([50]) * 32, bytes([51]) * 32, bytes([52]) * 32, bytes([53]) * 32, bytes([54]) * 32
    col.c.execute("INSERT INTO follow(wallet, added_at, golden_now, report_copy_roi) VALUES (?, 0, 1, NULL)", (b58(W),))
    col.paper.reload()
    col.on_logs(400, logs(create_bytes(mint, dev)), "sig-create")
    col.on_logs(500, logs(pool_bytes(pool, mint, b58decode(WSOL)), AMM_PROGRAM), "sig-migrate")
    assert col.pools == {b58(pool): b58(mint)} and col.stats["graduated"] == 1
    B, Q = 10**15, 85 * 10**9
    w_buy = logs(amm_bytes(True, pool, W, 10**12, B, Q, 10**8, 2 * 10**5, 10**8 + 2 * 10**5, 10**8 + 5 * 10**5), AMM_PROGRAM)
    col.on_logs(510, w_buy, "sig-w")
    col.on_logs(510, w_buy, "sig-w")                     # the same transaction again, from the other subscription
    col.on_logs(511, logs(amm_bytes(True, bytes([99]) * 32, X, 5, B, Q, 5, 0, 5, 5), AMM_PROGRAM), "sig-other-pool")
    B2, Q2 = B - 10**12, Q + 10**8 + 2 * 10**5
    col.on_logs(513, logs(amm_bytes(True, pool, X, 10**11, B2, Q2, 10**7, 2 * 10**4, 10**7 + 2 * 10**4, 10**7 + 5 * 10**4), AMM_PROGRAM), "sig-x")
    col.flush()
    rows = col.c.execute("SELECT slot, sol, tok, fee, vsol, vtok FROM trades ORDER BY slot").fetchall()
    assert rows[0] == (510, 10**8, 10**12, 5 * 10**5, Q2, B2) and len(rows) == 2 and col.stats["amm_trades"] == 2
    copy = col.c.execute("SELECT side, trigger_slot, land_slot FROM pfills").fetchall()
    assert copy == [("buy", 510, 512)]                   # W's PumpSwap buy was copied at the pool's reserves
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
