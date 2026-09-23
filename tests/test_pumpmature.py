import time

from hl_screener.pumpfun import FEE, TX_COST_SOL, connect
from hl_screener.pumpmature import CURVE_DONE_VTOK, LOOK_AT_S, coin_entries, mature_pnl, mature_report, settle_matures

SOL = 10**9


class Coin:
    """A coin's trades: the pump.fun curve until its last real token is sold, then its PumpSwap pool. Sells pay
    1 % on the pool and buys log none, the way PumpSwap's events read."""
    def __init__(self, t0: int):
        self.t0, self.vsol, self.vtok, self.rows = t0, 30 * SOL, 1_073_000_000 * 10**6, []

    def to(self, sec: int, vsol: int, vtok: int | None = None) -> int:
        """One trade `sec` s after launch that leaves `vsol` in the reserves (constant product)."""
        vtok = vtok or self.vsol * self.vtok // vsol
        buy, sol = vsol > self.vsol, abs(vsol - self.vsol)
        fee = 0 if buy else sol // 100
        self.rows.append((1000 + sec * 5 // 2, self.t0 + sec, vsol, vtok, int(buy), sol, fee))
        self.vsol, self.vtok = vsol, vtok
        return len(self.rows) - 1

    def complete(self, sec: int) -> int:
        """The buy that takes the curve's last real token."""
        return self.to(sec, 30 * SOL * 1_073_000_000 * 10**6 // CURVE_DONE_VTOK, CURVE_DONE_VTOK)

    def migrate(self, quote: int = 85 * SOL) -> None:
        self.vsol, self.vtok = quote, 206_900_000 * 10**6


def graduating_coin(t0: int) -> tuple[Coin, dict[str, int]]:
    c, at = Coin(t0), {}
    c.to(0, 35 * SOL)
    c.to(60, 95 * SOL)                                   # 280 SOL of market cap: not near yet
    at["near"] = c.to(120, 100 * SOL)                    # 311: near the end of the curve
    at["180"] = c.to(180, 105 * SOL)
    at["done"] = c.complete(600)
    c.migrate()
    at["grad"] = c.to(610, 86 * SOL)                     # the pool's first trade
    at["810"] = c.to(810, 96 * SOL)                      # +25 % on the migration price
    at["1610"] = c.to(1610, 80 * SOL)                    # -13 %
    at["3000"] = c.to(3000, 100 * SOL)                   # above the migration price an hour after it: aged1h
    at["5000"] = c.to(5000, 130 * SOL)                   # 961 SOL of market cap: past $100k
    at["12000"] = c.to(12000, 60 * SOL)                  # and back down 79 %
    return c, at


def test_each_mature_entry_and_exit_lands_on_its_own_price():
    c, at = graduating_coin(0)
    found, done = coin_entries(c.rows, 0, latency_slots=2)
    assert done == at["done"] and set(found) == {"near", "grad", "aged1h", "mc100k"}
    state = lambda i, fee: [c.rows[i][2], c.rows[i][3], fee]            # noqa: E731 - every order here lands on a quiet slot
    near, grad, aged, mc = (found[k]["states"] for k in ("near", "grad", "aged1h", "mc100k"))
    assert near["entry"] == state(at["near"], FEE) and near["5m"] == state(at["180"], FEE)   # on the curve: its fee
    assert near["tp20_sl10"] == state(at["done"], FEE)                  # +32 % on the last curve trade
    assert near["30m"] == state(at["1610"], 0.01)                       # on the pool: what its sells pay
    assert grad["entry"] == state(at["grad"], 0.01) and found["grad"]["ts"] == 610
    assert grad["5m"] == grad["tp20_sl10"] == state(at["810"], 0.01)
    assert grad["30m"] == state(at["1610"], 0.01) and grad["2h"] == state(at["5000"], 0.01)
    assert grad["tp50_sl25"] == grad["tp100_sl50"] == state(at["5000"], 0.01)   # -13 % never hit -25 %
    assert grad["6h"] == state(at["12000"], 0.01)
    assert aged["entry"] == state(at["3000"], 0.01) and found["aged1h"]["ts"] == 610 + 3600   # the hour, not the trade
    assert aged["5m"] == state(at["3000"], 0.01) and aged["2h"] == state(at["5000"], 0.01)    # timed from the hour
    assert mc["entry"] == state(at["5000"], 0.01) and mc["tp20_sl10"] == state(at["12000"], 0.01)   # the stop
    v, t, _ = grad["entry"]
    tokens = t - v * t / (v + 0.5 * SOL / 1.01)
    v2, t2, _ = grad["tp20_sl10"]
    want = (v2 - v2 * t2 / (t2 + tokens)) * 0.99 / SOL - 0.5 - 2 * TX_COST_SOL
    assert abs(mature_pnl(grad, 0.5, TX_COST_SOL)["tp20_sl10"] - want) < 1e-9 and want > 0
    thin = Coin(0)                                       # migrates into a pool a 0.5 SOL order would swamp
    thin.to(0, 100 * SOL)
    thin.complete(30)
    thin.migrate(5 * SOL)
    thin.to(40, 6 * SOL)
    assert set(coin_entries(thin.rows, 0, 2)[0]) == {"near"}


def test_mature_coins_are_looked_at_once_when_30_h_old_and_reported(tmp_path):
    db, now = tmp_path / "pump.db", time.time()
    c = connect(db)
    c.execute("INSERT INTO wallets (id, addr) VALUES (1, 'W')")
    for mid, t0 in ((1, int(now - LOOK_AT_S - 600)), (2, int(now - 3600))):   # old enough, and not yet
        coin, _ = graduating_coin(t0)
        c.execute("INSERT INTO mints (id, addr, slot, ts, creator) VALUES (?, ?, 1000, ?, 1)", (mid, f"M{mid}", t0))
        c.executemany("INSERT INTO trades VALUES (?,?,?,1,?,?,0,?,?,?)",
                      [(s, ts, mid, b, sol, fee, v, t) for s, ts, v, t, b, sol, fee in coin.rows])
    c.commit()
    assert settle_matures(db, latency_slots=2, now=now, chunk=3) == 4
    assert settle_matures(db, latency_slots=2, now=now) == 0                 # each coin once
    assert c.execute("SELECT mint, done FROM mature_cand ORDER BY mint").fetchall() == [(1, 1), (2, 0)]
    assert c.execute("SELECT trig, age_s, grad_s FROM matures ORDER BY age_s").fetchall() == [
        ("near", 120, 600), ("grad", 610, 600), ("aged1h", 4210, 600), ("mc100k", 5000, 600)]
    c.close()
    rep = mature_report(db)
    assert rep["counts"]["grad"] == {"all": 1, "organic": 1}                 # 10 min to migrate: not a bundle
    tp = next(r for r in rep["triggers"]["grad"]["all"] if r["rule"] == "tp20_sl10")
    assert tp["n"] == 1 and tp["roi"] > 0 and tp["win_rate"] == 1
