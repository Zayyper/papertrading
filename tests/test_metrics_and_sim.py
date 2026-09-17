
from hl_screener.config import Config
from hl_screener.fills import RoundTrip
from hl_screener.liquidity import CoinStats
from hl_screener.metrics import EquityCurve, compute_metrics
from hl_screener.simulate import FundingBook, penalty_bps, portfolio_of, simulate_follower

DAY = 86_400_000


def rt(coin, d, t_open, hold_min, entry, exit_, size, fees=0.0, liq=False):
    gross = d * (exit_ - entry) * size
    return RoundTrip(coin=coin, direction=d, open_time=t_open, close_time=t_open + int(hold_min * 60_000),
                     entry_vwap=entry, exit_vwap=exit_, max_abs_size=size, max_notional=size * entry,
                     gross_pnl=gross, fees=fees, liquidated=liq, n_fills=2)


def test_equity_curve_step_and_dd():
    e = EquityCurve([(0, 1000), (10, 1200), (20, 900), (30, 1100)])
    assert e.at(-1) == 1000 and e.at(15) == 1200 and e.at(25) == 900 and e.at(100) == 1100
    assert abs(e.max_drawdown_between(0, 30) - 0.25) < 1e-12
    assert e.first_time == 0


def test_metrics_basic():
    t0 = 100 * DAY
    trips = [
        rt("BTC", 1, t0 + 1 * DAY, 60, 100, 110, 1),       # +10
        rt("BTC", -1, t0 + 5 * DAY, 30, 100, 95, 1),       # +5
        rt("ETH", 1, t0 + 20 * DAY, 10, 100, 90, 1),       # -10
        rt("XYZ", 1, t0 + 40 * DAY, 120, 10, 12, 10),      # +20 (thin coin, notional 100)
    ]
    eq = EquityCurve([(0, 1000.0), (t0 + 60 * DAY, 1025.0)])
    tiers = {"BTC": "major", "ETH": "major", "XYZ": "thin"}
    m = compute_metrics("0xabc", trips, eq, [(0, 0.0), (t0 + 60 * DAY, 25.0)], tiers, t0, t0 + 60 * DAY)
    assert m.n_trades == 4 and m.net_pnl == 25 and m.gross_profit == 35 and m.gross_loss == 10
    assert abs(m.profit_factor - 3.5) < 1e-12 and m.win_rate == 0.75
    assert abs(m.top2_share - 30 / 35) < 1e-12
    assert abs(m.roi_trades - 0.025) < 1e-12 and abs(m.roi_portfolio - 0.025) < 1e-12
    assert m.median_hold_min == 45.0
    assert abs(m.thin_share - 100 / 400) < 1e-12 and abs(m.major_share - 0.75) < 1e-12
    assert m.n_coins == 3 and m.liquidations == 0 and m.active_days == 4
    assert abs(m.max_dd_trades - 10 / 1000) < 1e-12   # +10, +15, +5 (peak 15 -> 5)
    assert m.account_age_days == 160
    # 14-day buckets over 60 days = 5; closes on days 1,5 -> b0 (+15), day 20 -> b1 (-10), day 40 -> b2 (+20), b3/b4 empty
    assert m.n_buckets == 5 and abs(m.positive_bucket_share - 2 / 5) < 1e-12


def test_penalty_model_orders_tiers():
    cfg = Config()
    major = CoinStats("BTC", 2e9, "major", 0.004)
    thin = CoinStats("XYZ", 2e6, "thin", 0.02)
    assert penalty_bps(major, 1000, cfg) < penalty_bps(thin, 1000, cfg)
    assert penalty_bps(thin, 1000, cfg) < penalty_bps(thin, 100_000, cfg)   # impact grows with size
    # numbers: major = 1 + 0.004*sqrt(3/3600)*1e4 (~1.15) + 0.004*sqrt(24)*sqrt(1000/2e9)*1e4 (~0.14) ≈ 2.3 bps
    assert 2.0 < penalty_bps(major, 1000, cfg) < 2.6


def test_follower_sim_gap_and_funding_sign():
    cfg = Config(follower_equity_usd=1000.0, follower_max_leverage=10.0, taker_fee_bps=4.5, builder_fee_bps=0.0,
                 latency_z=0.0, impact_coeff=0.0, half_spread_bps={"major": 0.0, "mid": 0.0, "thin": 0.0})
    t0 = 100 * DAY
    # leader equity 10k, trades 20k notional (2x), captures +1%
    trips = [rt("BTC", 1, t0, 120, 100, 101, 200, fees=18.0)]
    eq = EquityCurve([(0, 10_000.0)])
    cs = {"BTC": CoinStats("BTC", 2e9, "major", 0.004)}
    # one funding event inside the hold at +0.01%/h -> long pays
    fb = FundingBook.from_records({"BTC": [{"time": t0 + 30 * 60_000, "fundingRate": "0.0001"}]})
    r = simulate_follower(trips, eq, cs, fb, cfg, t0 - DAY, t0 + DAY)
    assert r.n_trades == 1
    notional = 2.0 * 1000.0
    exp_gross = 0.01 * notional
    exp_fees = 4.5e-4 * notional * 2
    exp_fund = 0.0001 * notional
    assert abs(r.gross_pnl - exp_gross) < 1e-9
    assert abs(r.fees - exp_fees) < 1e-9
    assert abs(r.funding - exp_fund) < 1e-9
    assert abs(r.net_pnl - (exp_gross - exp_fees - exp_fund)) < 1e-9
    assert abs(r.roi - r.net_pnl / 1000.0) < 1e-12
    # leader: +200 gross - 18 fees on 10k = 1.82%
    assert abs(r.leader_roi - 0.0182) < 1e-12
    assert abs(r.copy_gap - (0.0182 - r.roi)) < 1e-12

    # with an adverse penalty the follower does worse
    cfg2 = Config(follower_equity_usd=1000.0, latency_z=0.0, impact_coeff=0.0, half_spread_bps={"major": 5.0, "mid": 5.0, "thin": 5.0})
    r2 = simulate_follower(trips, eq, cs, None, cfg2, t0 - DAY, t0 + DAY)
    assert r2.gross_pnl < r.gross_pnl


def test_portfolio_of_merges_streams():
    cfg = Config(follower_equity_usd=1000.0)
    from hl_screener.simulate import FollowerResult, SimTrade

    def st(t, pnl):
        return SimTrade("BTC", t - 1, t, 1, 0, 0, 1000, 0, 0, pnl, 0, 0, pnl)

    a = FollowerResult(roi=0.05, trades=[st(1, 30), st(3, 20)])
    b = FollowerResult(roi=-0.02, trades=[st(2, -40), st(4, 20)])
    p = portfolio_of({"a": a, "b": b}, cfg)
    assert p["n_leaders"] == 2 and p["n_trades"] == 4
    assert abs(p["net_pnl"] - 30) < 1e-9 and abs(p["roi"] - 30 / 2000) < 1e-12
    assert abs(p["max_dd"] - 40 / 2000) < 1e-12       # 30 -> -10
    assert p["leaders_positive_share"] == 0.5
