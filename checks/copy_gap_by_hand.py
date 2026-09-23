"""Check copy_gap by hand, independently of its code: fills straight from Hyperliquid, every copied order recomputed
from the raw captured book with plain arithmetic, and every book held against Hyperliquid's own 1-minute candle.

    python checks/copy_gap_by_hand.py <address> [<address> ...]   (HL_DATABASE_URL set, from the project folder)

The results of 2026-09-23 are written up in checks/copy_gap_by_hand.md.
"""
import os
import sys
import time

import requests

sys.path.insert(0, os.getcwd())
from hl_screener.copygap import CopyCfg, PgMarket, copy_gap  # noqa: E402  (the thing under test)
from hl_screener.tape import connect, get_meta, plan_of  # noqa: E402

API = "https://api.hyperliquid.xyz/info"


def post(body):
    for i in range(5):
        r = requests.post(API, json=body, timeout=30)
        if r.status_code == 200:
            return r.json()
        time.sleep(2 + 3 * i)
    raise RuntimeError(body)


def walk(levels, size):
    """VWAP of taking `size` from price levels [[px, sz], ...], best first: the arithmetic written out."""
    left, cost, steps = size, 0.0, []
    for px, sz in levels:
        take = min(left, sz)
        cost += take * px
        steps.append(f"{take:.6g} @ {px:.6g}")
        left -= take
        if left <= 1e-12:
            break
    if left > 1e-12:
        cost += left * levels[-1][0]
        steps.append(f"{left:.6g} @ {levels[-1][0]:.6g} (past the visible book)")
    return cost / size, steps


def check(conn, addr, plan):
    t0 = conn.execute("SELECT MIN(added_ms) FROM tracked WHERE address = %s", (addr,)).fetchone()[0]   # this wallet's own start
    t1 = int(time.time() * 1000)
    cfg = CopyCfg.from_plan(plan["copy"], plan["holdout"]["delay_s"])
    # 1. the fills, from the exchange itself, against what the collector stored
    raw = [f for f in post({"type": "userFillsByTime", "user": addr, "startTime": t0}) if not f["coin"].startswith("@") and ":" not in f["coin"]]
    stored = {r[0]: r for r in conn.execute("SELECT tid, time_ms, coin, side, px, sz, start_pos, fee FROM fills WHERE address = %s AND time_ms >= %s", (addr, t0))}
    upto = max((r[1] for r in stored.values()), default=0)
    seen = [f for f in raw if int(f["time"]) <= upto]
    diff = [f for f in seen if f["tid"] not in stored or abs(stored[f["tid"]][4] - float(f["px"])) > 1e-9
            or abs(stored[f["tid"]][5] - float(f["sz"])) > 1e-12 or abs(stored[f["tid"]][6] - float(f["startPosition"])) > 1e-9]
    print(f"\n## {addr}\nfills from the exchange since tracking began: {len(raw)}; stored by the collector: {len(stored)}; "
          f"differing (px, size or start position): {len(diff)}; newer than the collector's last fetch: {len(raw) - len(seen)}")
    # 2. orders, sizing and the book, by hand
    fills = sorted(({"tid": r[0], "time_ms": r[1], "coin": r[2], "side": r[3], "px": r[4], "sz": r[5], "start_pos": r[6], "fee": r[7]}
                    for r in stored.values()), key=lambda f: (f["time_ms"], f["tid"]))
    eq = [(int(t), v) for t, v in conn.execute("SELECT time_ms, value FROM equity WHERE address = %s ORDER BY time_ms", (addr,))]
    res = copy_gap(fills, eq, PgMarket(conn, t0, t1), cfg, t0, t1)
    tot = {"latency": 0.0, "slippage": 0.0, "copy_fee": 0.0, "lead_fee": 0.0}
    worst, outside = 0.0, 0
    for i, row in enumerate(res["rows"]):
        b = conn.execute("SELECT taken_ms, bids, asks FROM books WHERE coin = %s AND taken_ms = %s", (row["coin"], row["book_ms"])).fetchone()
        bids, asks = b[1], b[2]
        mid = (bids[0][0] + asks[0][0]) / 2
        px, steps = walk(asks if row["size"] > 0 else bids, abs(row["size"]))
        lat, slip = row["size"] * (mid - row["leader_px"]), row["size"] * (px - mid)
        fee_c = abs(row["size"]) * px * (cfg.taker_fee_bps + cfg.builder_fee_bps) / 1e4
        worst = max(worst, abs(px - row["copy_px"]), abs(mid - row["mid"]), abs(lat - row["latency_usd"]), abs(slip - row["slippage_usd"]),
                    abs(fee_c - row["copy_fee"]))
        tot["latency"] += lat
        tot["slippage"] += slip
        tot["copy_fee"] += fee_c
        tot["lead_fee"] += row["leader_fee"]
        # 3. the book against the exchange's own 1-minute candle of that moment
        m0 = row["book_ms"] - row["book_ms"] % 60_000
        c = post({"type": "candleSnapshot", "req": {"coin": row["coin"], "interval": "1m", "startTime": m0, "endTime": m0 + 59_999}})
        lo, hi = (float(c[0]["l"]), float(c[0]["h"])) if c else (None, None)
        inside = lo is not None and lo * 0.9995 <= mid <= hi * 1.0005
        outside += not inside
        if i < 3 or not inside:
            print(f"- {time.strftime('%H:%M:%S', time.gmtime(row['time_ms'] / 1000))}.{row['time_ms'] % 1000:03d} UTC {row['coin']} {row['action']} "
                  f"{row['size']:+.6g} | leader px {row['leader_px']:.6g} | book taken {row['book_ms'] - row['time_ms']} ms after the trade "
                  f"(due {row['due_ms'] - row['time_ms']}) | best bid {bids[0][0]:.6g}, ask {asks[0][0]:.6g}, mid {mid:.6g} | walk: "
                  f"{', '.join(steps)} = {px:.6g} | latency {row['size']:+.6g} x ({mid:.6g} - {row['leader_px']:.6g}) = {lat:+.4f} | "
                  f"slippage {row['size']:+.6g} x ({px:.6g} - {mid:.6g}) = {slip:+.4f} | fee {abs(row['size']):.6g} x {px:.6g} x 4.5 bps = "
                  f"{fee_c:.4f} | exchange 1-min candle {lo}-{hi}: {'mid inside' if inside else 'MID OUTSIDE'}")
    g = res["gap"]
    print(f"orders {res['orders']}, copied {res['copied']}, no book in time {res['missing_book']}, under $10 {res['too_small']}, "
          f"actions {res['actions']}, books outside their candle {outside}")
    print(f"by hand: latency {tot['latency']:+.4f}, slippage {tot['slippage']:+.4f}, fees {tot['copy_fee'] - tot['lead_fee']:+.4f} | "
          f"copy_gap: latency {g['latency']:+.4f}, slippage {g['slippage']:+.4f}, fees {g['fees']:+.4f}, funding {g['funding']:+.4f}, "
          f"total {g['usd']:+.4f} (parts add to {g['latency'] + g['slippage'] + g['fees'] + g['funding']:+.4f}) | largest per-row difference {worst:.1e}")
    return res


if __name__ == "__main__":
    conn = connect(os.environ["HL_DATABASE_URL"])
    plan = plan_of(get_meta(conn, "plan"))
    for a in sys.argv[1:]:
        check(conn, a.lower(), plan)
