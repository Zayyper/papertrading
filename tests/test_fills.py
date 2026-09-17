from hl_screener.fills import build_round_trips, parse_fills


def fill(coin, px, sz, side, t, start, pnl=0.0, fee=0.0, d="", liq=None, tid=None):
    f = {"coin": coin, "px": str(px), "sz": str(sz), "side": side, "time": t, "startPosition": str(start),
         "closedPnl": str(pnl), "fee": str(fee), "dir": d, "tid": tid if tid is not None else t}
    if liq:
        f["liquidation"] = liq
    return f


def test_simple_long_round_trip():
    raw = [
        fill("ETH", 100, 1, "B", 1000, 0, d="Open Long", fee=0.1),
        fill("ETH", 110, 1, "A", 61000, 1, pnl=10, d="Close Long", fee=0.1),
    ]
    trips, open_ = build_round_trips(parse_fills(raw))
    assert not open_
    assert len(trips) == 1
    t = trips[0]
    assert t.direction == 1 and t.entry_vwap == 100 and t.exit_vwap == 110
    assert t.gross_pnl == 10 and abs(t.fees - 0.2) < 1e-9 and abs(t.net_pnl - 9.8) < 1e-9
    assert t.hold_minutes == 1.0 and t.max_notional == 100 and t.complete


def test_scale_in_partial_out_and_short():
    raw = [
        fill("BTC", 100, 1, "A", 0, 0, d="Open Short"),
        fill("BTC", 90, 1, "A", 1, -1, d="Open Short"),          # add at better price
        fill("BTC", 80, 1, "B", 2, -2, pnl=15, d="Close Short"),  # partial close
        fill("BTC", 70, 1, "B", 3, -1, pnl=25, d="Close Short"),  # flat
    ]
    trips, open_ = build_round_trips(parse_fills(raw))
    assert not open_ and len(trips) == 1
    t = trips[0]
    assert t.direction == -1
    assert t.entry_vwap == 95 and t.exit_vwap == 75
    assert t.max_abs_size == 2 and t.max_notional == 190
    assert t.gross_pnl == 40 and t.n_fills == 4
    assert abs(t.price_return - (95 - 75) / 95) < 1e-12


def test_flip_creates_two_trips():
    raw = [
        fill("SOL", 10, 2, "B", 0, 0, d="Open Long"),
        fill("SOL", 12, 5, "A", 1, 2, pnl=4, d="Long > Short"),   # closes 2, opens 3 short
        fill("SOL", 11, 3, "B", 2, -3, pnl=3, d="Close Short"),
    ]
    trips, open_ = build_round_trips(parse_fills(raw))
    assert not open_ and len(trips) == 2
    a, b = trips
    assert a.direction == 1 and a.exit_vwap == 12 and a.gross_pnl == 4
    assert b.direction == -1 and b.entry_vwap == 12 and b.exit_vwap == 11 and b.gross_pnl == 3
    assert b.max_abs_size == 3


def test_missing_opening_is_marked_incomplete():
    raw = [fill("DOGE", 0.1, 100, "A", 5, 100, pnl=1, d="Close Long")]
    trips, open_ = build_round_trips(parse_fills(raw))
    assert len(trips) == 1 and not trips[0].complete
    assert trips[0].direction == 1


def test_liquidation_flag_and_spot_skipped():
    raw = [
        fill("@107", 1, 1, "B", 1, 0, d="Buy"),
        fill("PEPE", 1, 10, "B", 2, 0, d="Open Long"),
        fill("PEPE", 0.5, 10, "A", 3, 10, pnl=-5, d="Close Long", liq={"liquidatedUser": "0x"}),
    ]
    fills = parse_fills(raw)
    assert all(f.coin != "@107" for f in fills)
    trips, _ = build_round_trips(fills)
    assert len(trips) == 1 and trips[0].liquidated


def test_liquidation_counts_only_when_this_trader_was_liquidated():
    me = "0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    other = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    # our resting order absorbed someone else's liquidation: not our liquidation
    raw = [
        fill("HYPE", 40, 1, "B", 1, 0, d="Open Long", liq={"liquidatedUser": other, "markPx": "40", "method": "market"}),
        fill("HYPE", 41, 1, "A", 2, 1, pnl=1, d="Close Long"),
    ]
    trips, _ = build_round_trips(parse_fills(raw, address=me))
    assert len(trips) == 1 and not trips[0].liquidated
    # we were the one liquidated (address case-insensitive)
    raw = [
        fill("HYPE", 40, 1, "B", 1, 0, d="Open Long"),
        fill("HYPE", 30, 1, "A", 2, 1, pnl=-10, d="Close Long", liq={"liquidatedUser": me.lower(), "markPx": "30", "method": "market"}),
    ]
    trips, _ = build_round_trips(parse_fills(raw, address=me))
    assert len(trips) == 1 and trips[0].liquidated
    # no address given: conservative, any liquidation record counts
    trips, _ = build_round_trips(parse_fills(raw))
    assert trips[0].liquidated


def test_still_open_position_not_counted():
    raw = [fill("ETH", 100, 1, "B", 1, 0, d="Open Long")]
    trips, open_ = build_round_trips(parse_fills(raw))
    assert trips == [] and "ETH" in open_
