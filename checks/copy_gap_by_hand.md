# copy_gap, checked by hand on 5 wallets

2026-09-23, 18:38–20:08 UTC, local run of the copy-test collector (`copytest collect` against a
throwaway Postgres), delay 3 s, the plan's settings: $1,000 per wallet, 10× cap, 4.5 bps taker,
book walk. Three of the wallets are from the plan's random group; two (`0x4e36…`, `0xe34a…`) were
added to the local database only, to get copied trades within the hour. The server's plan and its
groups are untouched.

## What was checked, independently of `copy_gap`'s code

1. **The fills.** Every fill Hyperliquid itself returns for the wallet since it was tracked
   (`userFillsByTime`), against what the collector stored: price, size and starting position.
2. **Every copied order, recomputed with plain arithmetic** from the raw book the collector
   stored: walk the levels for the copy's size (VWAP), mid = (best bid + best ask) / 2,
   latency = size × (mid − wallet's price), slippage = size × (copy price − mid),
   fee = |size| × copy price × 4.5 bps. Then the sums, against `copy_gap`'s totals.
3. **Every book against the exchange's own record of that minute**: its mid must sit inside the
   1-minute candle (`candleSnapshot`) of the moment it was taken. A book stored at the wrong time
   or for the wrong coin would fall outside.
4. **Sizing, once in full**: the copy's size = the order's size × $1,000 / the wallet's account
   value at that moment, capped at 10× leverage.

## Results

| Wallet | Group | Fills: exchange / stored / different | Orders | Copied | No book in time | Under $10 | By hand = copy_gap (largest difference) | Books outside their candle | Gap, USD = latency + slippage + fees |
|---|---|---|---|---|---|---|---|---|---|
| `0x10fa553d…f056` | random | 1 / 1 / 0 | 1 | 1 | 0 | 0 | yes (0) | 0 | +6.14 = +2.78 + 0.06 + 3.30 |
| `0x4e360be6…95` | local only | 302 / 302 / 0 | 76 | 28 | 2 | 46 | yes (1.4e-17) | 0 | +0.49 = −0.07 + 0.16 + 0.40 |
| `0xa84bd866…5761` | random | 1 / 1 / 0 | 1 | 1 | 0 | 0 | yes (2.2e-16) | 0 | −2.37 = −0.13 + 0.01 − 2.25 |
| `0xe34aea3b…3b38` | local only | 58 / 58 / 0 | 34 | 19 | 1 | 3 | yes (5.6e-17) | 0 | +0.52 = −0.24 + 0.13 + 0.62 |
| `0xc24574dc…3c` | random | 87 / 87 / 0 (34 newer than the last fetch) | 6 | 5 | 0 | 0 | yes (5.6e-17) | 0 | +2.53 = +1.03 + 1.41 + 0.10 |

Every order's arithmetic agrees with `copy_gap` to floating-point rounding, every captured book's
mid is inside its minute's candle, and the stored fills are the exchange's, field for field. The
three orders without a book fell in the seconds the local collector was restarted (19:11), and
are counted against coverage rather than priced by guess.

## Worked examples (one per wallet)

**`0x10fa…`, BTC, 19:07:24.043 UTC.** Wallet bought 1.0 BTC at 84,321 (already long 1.0, so an
add; the copy holds nothing yet, so it opens only the added part). Account value then $8,444.70 →
ratio 1,000 / 8,444.70 = 0.118417 → copy 0.118417 BTC; the 10× cap is 10 × 1,000 / 84,321 =
0.11859, not binding. Book taken 3,325 ms after the trade (due 3,000): bid 84,344, ask 84,345,
mid 84,344.5; the copy fits in the best ask → 84,345. Latency 0.118417 × (84,344.5 − 84,321) =
**+2.7828**; slippage 0.118417 × (84,345 − 84,344.5) = **+0.0592**; copy fee 0.118417 × 84,345 ×
0.00045 = 4.4946 against the wallet's own 1.2 bps (1.1982 on this size) → **+3.2964**. Candle
84,279–84,372: inside. Total +6.1384, as `copy_gap` says.

**`0x4e36…`, TAO, 19:13:29.301 UTC.** Copy +0.0683452 at the ask 289.33 (mid 289.315), wallet at
289.30: latency +0.0010, slippage +0.0010, fee 0.0089. 46 of this wallet's 76 orders would be
copies under Hyperliquid's $10 minimum: with $57k in the account, a $1,000 copy is 1/57 of it.

**`0xa84b…`, BTC, 20:02:35.578 UTC.** Copy +0.0281125 at 84,402 (mid 84,401.5), wallet at 84,406:
the price moved the copy's way, latency −0.1265. Fees −2.2543: this wallet paid more per trade
than a 4.5 bps copier would (a front end's fee on top), so the copy is ahead on this order.

**`0xe34a…`, ETH, 19:46:30.081 UTC.** Copy +0.307837 at 2,676.5 (mid 2,676.45), wallet at 2,676.9:
latency −0.1385 (the price fell before the copy bought), slippage +0.0154, fee 0.3708.

**`0xc245…`, XPL, 19:53:55.753 UTC.** Copy +17,794.4 XPL walks six levels: 2,992 @ 0.088723,
180 @ 0.088725, 563 @ 0.088727, 7,218 @ 0.08874, 4,506 @ 0.088764, 2,335.44 @ 0.088765 =
0.0887459 VWAP; mid 0.0887225, wallet at 0.0887089: latency +0.2420, slippage +0.4170 (the
depth), fee 0.7106. Candle 0.088556–0.088725: mid inside.

## What it already shows (1.5 hours, 5 wallets: an illustration, not a result)

- At 3 s on majors the price rarely moves much; **fees are the biggest piece** whenever the wallet
  pays maker or discounted rates and the copier pays taker. On thinner coins the **book depth**
  takes over (XPL: 0.4 of the 0.7 gap).
- **Account size decides copyability at $1,000**: most orders of a $57k account are below the
  exchange minimum once scaled. The screener's pool ($1k–$50k) is the right range for this.
- The latency term can be negative (the price moves the copier's way). Only the sum over many
  trades says whether 3 s costs anything; that is what the delay curve measures on the server.
