from hl_screener.pumpexits import MAX_POSITIONS, exit_pnls, exit_sweep, reentries
from hl_screener.pumpfun import FEE, PAPER_STAKE_SOL, TX_COST_SOL, connect, copy_sim, copy_trade

SOL = 10**9
W, X, D = 1, 2, 3                 # the followed wallet, everyone else, the coins' maker
T0 = 1_790_000_000


class Coin:
    """One coin on a constant-product curve, trades 2.5 slots a second after slot 1000, each paying 1.25 %."""
    def __init__(self, c, mint: int, ts: int):
        self.c, self.mint, self.ts, self.vsol, self.vtok, self.at = c, mint, ts, 30 * SOL, 1_073_000_000 * 10**6, {}
        c.execute("INSERT INTO mints(id, addr, slot, ts, creator) VALUES (?, ?, 1000, ?, ?)", (mint, f"mint{mint}", ts, D))

    def to(self, sec: int, vsol: float, who: int = X) -> None:
        """A trade `sec` s after the wallet's buy that leaves `vsol` SOL in the curve."""
        vsol = int(vsol * SOL)
        vtok, buy, sol = self.vsol * self.vtok // vsol, vsol > self.vsol, abs(vsol - self.vsol)
        self.c.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?)", (1000 + sec * 5 // 2, self.ts + sec, self.mint, who,
                       int(buy), sol, abs(self.vtok - vtok), sol * 125 // 10_000, vsol, vtok))
        self.vsol, self.vtok = vsol, vtok
        self.at[sec] = (vsol, vtok)


def coins(tmp_path):
    c = connect(tmp_path / "pump.db")
    c.executemany("INSERT INTO wallets(id, addr) VALUES (?, ?)", [(W, "Wfollowed"), (X, "Xothers"), (D, "Dmaker")])
    up, down = Coin(c, 1, T0), Coin(c, 2, T0 + 100)
    up.to(0, 31, W)               # the wallet buys: the copy lands 4 slots later on this state, +0 % gross of the -4 % fees
    for sec, vsol in ((4, 32), (8, 33), (12, 34), (16, 36), (20, 40), (24, 25)):
        up.to(sec, vsol)          # worth +2, +9, +15 (tp10), +29 (tp20, first), +59 (tp20 again), -37 %
    up.to(30, 24, W)              # the wallet sells
    down.to(0, 31, W)
    for sec, vsol in ((4, 30.5), (8, 29.5), (12, 28), (16, 26)):
        down.to(sec, vsol)        # worth -7, -13 (sl10, first), -22 (sl20, 15 % under the bought worth), -32 %
    down.to(40, 22, W)
    down.to(100, 23)              # the last trade inside the 30 min
    c.commit()
    return c, up, down


def test_each_exit_rule_leaves_on_its_own_trade(tmp_path):
    c, up, down = coins(tmp_path)
    at = lambda coin, sec: copy_trade(coin.at[0], coin.at[sec], PAPER_STAKE_SOL, TX_COST_SOL, FEE, FEE)   # noqa: E731
    u, d = exit_pnls(c, W, 1, 1000, T0), exit_pnls(c, W, 2, 1000, T0 + 100)
    near = lambda a, b: abs(a - b) < 1e-9                                                                # noqa: E731
    # take-profit: the first trade over the target, not a later, higher one
    assert near(u["tp10_sl10"], at(up, 12)) and near(u["tp20_sl10"], at(up, 16)) and near(u["tp30_sl15"], at(up, 20))
    assert near(u["tp100_sl50"], at(up, 30))                    # never +100 % nor -50 %: out on the clock, after the last trade
    # stop-loss: the first trade under the stop
    assert near(d["tp20_sl10"], at(down, 8)) and near(d["tp20_sl20"], at(down, 12))
    # on the clock: the last trade at or before it
    assert near(u["hold10s"], at(up, 8)) and near(u["hold30s"], at(up, 30)) and near(d["hold1m"], at(down, 40))
    assert near(d["hold10m"], at(down, 100))
    # the trail exits off the best worth; armed at +10 % it never arms on the falling coin and leaves at the end
    assert near(u["trail15"], at(up, 24)) and near(d["trail15"], at(down, 12)) and near(d["trail15@10"], at(down, 100))
    # the wallet's own exit, and the stop under it
    assert near(u["wallet"], at(up, 30)) and near(d["wallet"], at(down, 40))
    assert near(d["wallet_sl20"], at(down, 12)) and near(u["wallet_sl20"], at(up, 24))


def test_the_wallet_rule_is_the_copy_replay(tmp_path):
    c, _, _ = coins(tmp_path)
    for late in (2, 3):
        sim = copy_sim(c, W, T0 + 50, late, PAPER_STAKE_SOL, TX_COST_SOL, MAX_POSITIONS)
        ours = [exit_pnls(c, W, m, 1000, T0, late, late)["wallet"] for m in (1, 2)]
        assert sim["n"] == 2 and abs(sim["pnl"] - sum(ours)) < 1e-12 and abs(sim["pnl_h1"] - ours[0]) < 1e-12


def test_the_sweep_logs_each_followed_wallet_and_all_pooled_best_rule_first(tmp_path):
    c, _, _ = coins(tmp_path)
    c.execute("INSERT INTO follow(wallet, added_at, golden_now, golden_ever) VALUES ('Wfollowed', 0, 1, 1)")
    c.commit()
    c.close()
    lines = exit_sweep(tmp_path / "pump.db")
    assert len(lines) == 2 and lines[0].startswith("exit sweep all followed (2 copies of 1 wallets")
    assert lines[1].startswith("exit sweep Wfollowed (2 copies): ") and " won " in lines[1] and "wallet_sl20" in lines[1]
    rois = [float(r.split(" ")[1].rstrip("%")) for r in lines[1].split(": ", 1)[1].split(", ")]
    assert rois == sorted(rois, reverse=True)


def test_a_buy_back_into_a_coin_sold_out_of_is_a_reentry_an_add_is_not(tmp_path):
    c = connect(tmp_path / "pump.db")
    c.executemany("INSERT INTO wallets(id, addr) VALUES (?, ?)", [(W, "Wfollowed"), (X, "Xothers"), (D, "Dmaker")])
    for mint, maker in ((1, D), (2, D), (3, W)):
        c.execute("INSERT INTO mints(id, addr, slot, ts, creator) VALUES (?, ?, 1000, ?, ?)", (mint, f"mint{mint}", T0, maker))

    def trade(mint, slot, buy, tok, who=W):
        c.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (slot, T0 + (slot - 1000) * 2 // 5, mint, who, buy, SOL, tok, SOL // 100, 30 * SOL, 1_073_000_000 * 10**6))

    # coin 1: bought, sold down to 40 (the next buy adds), sold down to 1 of its top 100 (1 %: the next buy is back in),
    # sold out again, back in again; the others' trades in between change nothing
    for slot, buy, tok in ((1000, 1, 100), (1050, 0, 60), (1100, 1, 50), (1200, 0, 89), (1300, 1, 70), (1400, 0, 71), (1500, 1, 10)):
        trade(1, slot, buy, tok)
        trade(1, slot + 1, 1 - buy, 5, X)
    for slot, buy in ((1000, 0), (1100, 1)):      # coin 2: sold first, bought before the stored trades: position unknown
        trade(2, slot, buy, 50)
    for slot, buy in ((1000, 1), (1100, 0), (1200, 1)):   # coin 3: its own coin
        trade(3, slot, buy, 50)
    c.execute("INSERT INTO follow(wallet, added_at, golden_now, golden_ever) VALUES ('Wfollowed', 0, 1, 1)")
    c.commit()
    assert reentries(c, W) == [(1, 1500, T0 + 200), (1, 1300, T0 + 120)]
    c.close()
    lines = exit_sweep(tmp_path / "pump.db", reentry=True)
    assert lines[0].startswith("reentry sweep all followed (2 copies of 1 wallets") and lines[1].startswith("reentry sweep Wfollowed (2 copies): ")


def test_hold2m_or_wallet_leaves_with_a_wallet_that_sells_within_2_min_and_on_the_clock_otherwise(tmp_path):
    c, _, _ = coins(tmp_path)                                         # both wallets sell within 40 s
    late = Coin(c, 3, T0 + 500)
    late.to(0, 31, W)
    late.to(60, 33)
    late.to(200, 30, W)                                               # this one sells after 200 s
    c.commit()
    for mint, t_in in ((1, T0), (2, T0 + 100)):
        p = exit_pnls(c, W, mint, 1000, t_in)
        assert p["hold2m_or_wallet"] == p["wallet"]
    p = exit_pnls(c, W, 3, 1000, T0 + 500)
    assert p["hold2m_or_wallet"] == p["hold2m"] != p["wallet"]
