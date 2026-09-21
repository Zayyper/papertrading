import base64
import struct

from hl_screener.pumpfun import (D_CREATE, D_TRADE, Collector, b58, build_report, copy_trade, parse_create,
                                 parse_trade, twins)


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

    def line(b):
        return "Program data: " + base64.b64encode(b).decode()

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
        col.on_logs(slot, [line(b)])

    col.on_logs(1000, [line(create_bytes(mint, W["D"], ts=1_790_001_000)), "Program log: Instruction: CreateV2"])
    trade(1000, "D", True, 10**9)            # the dev's own buy: never counted as trading
    trade(1001, "S", True, 5 * 10**8)        # sniper: one slot after creation
    trade(1010, "T", True, 10**9)
    trade(1011, "X", True, 3 * 10**9)
    trade(1012, "Y", True, 2 * 10**9)
    trade(1030, "T", False, None)
    trade(1031, "Z", True, 10**9)
    trade(1050, "S", False, None)
    # a trade on a token born before the collector started is ignored
    col.on_logs(1060, [line(trade_bytes(other_mint, W["X"], True, 10**9, 10**12, 31 * 10**9, 10**15))])
    col.flush()
    assert col.c.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 8

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


def test_twins_are_wallets_buying_the_same_tokens_in_the_same_slot(tmp_path):
    col = Collector(tmp_path / "pump.db")
    A, B, C, dev = (bytes([i]) * 32 for i in (40, 41, 42, 43))
    for k in range(4):                                   # four launches; A and B always buy together, C never with them
        mint = bytes([60 + k]) * 32
        base = 2000 + 100 * k
        col.on_logs(base, ["Program data: " + base64.b64encode(create_bytes(mint, dev)).decode()])
        for who, slot in ((A, base + 5), (B, base + 5), (C, base + 9 + k)):
            col.on_logs(slot, ["Program data: " + base64.b64encode(trade_bytes(mint, who, True, 10**8, 10**12, 31 * 10**9, 10**15)).decode()])
    col.flush()
    ids = dict(col.c.execute("SELECT addr, id FROM wallets"))
    assert twins(col.c, ids[b58(A)]) == 1 and twins(col.c, ids[b58(B)]) == 1 and twins(col.c, ids[b58(C)]) == 0
    col.c.close()
