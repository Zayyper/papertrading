# hl-niche-screener

Find small Hyperliquid perp traders whose trades are worth *copying*, and check
walk-forward whether the selection carries any signal — before spending calendar
time on a forward paper test.

Everything comes from Hyperliquid's public, unauthenticated API. No Invo token, no
scraping, no ToS risk.

## What it does

1. **Pool** — pulls the public leaderboard, keeps accounts with equity in
   `1k–50k USD` and enough all-time volume to prove they trade, takes a random
   sample (default 300; random, not "top by ROI", to avoid pre-selecting winners).
2. **History** — for each account: the `portfolio` equity/PnL curve (drops accounts
   younger than 90 days before downloading anything else), then all fills over the
   last 180 days. Fills are rebuilt into round trips (open → flat) per coin.
3. **In-sample screen** (first 120 days) — hard filters, each with a reason string:
   min 50 trades, active ≥ 20 days, traded in the last 14 days, median hold
   ≥ 60 × latency (3 min), ≤ 20 % of notional in thin coins, median leverage ≤ 25×,
   top-2 winners ≤ 40 % of gross profit, profit factor ≥ 1.3, drawdown ≤ 30 %,
   ≥ 50 % of 2-week buckets positive, no liquidations, **and** follower-realized
   return > 0.
4. **Copy simulator** — the number that is ranked. Per trade the follower sizes at
   the leader's leverage (capped), enters and exits at the leader's VWAP moved
   *against* it by a penalty (half-spread + latency move + √-impact), pays taker
   fees both legs and hourly funding. Score = follower ROI / max(drawdown, 5 %).
5. **Out-of-sample check** (last 60 days) — the shortlist, the naive "top-5 by
   in-sample leader ROI" basket and BTC buy-and-hold are run through the same
   simulator on the held-out window. A Spearman rank correlation between in-sample
   score and out-of-sample follower ROI says whether the screen has any information.
6. **Verdict** — proceed to the forward paper test only if the shortlist beats both
   benchmarks out-of-sample, is positive, has ≥ 3 leaders and a drawdown ≤ 30 %.

## Setup (Windows)

Requires Python 3.11 or newer (`tomllib`). In **PowerShell**, from the unzipped folder:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m pytest -q          # 20 tests, offline, ~2 s
python -m hl_screener demo   # full pipeline on synthetic traders, no network; shows a report
```

If `Activate.ps1` is blocked, run once: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.
(In Git Bash the activation line is `source .venv/Scripts/activate` instead.)

## Running for real

Still in PowerShell with the venv active:

```powershell
python -m hl_screener pool                 # who would be screened (fast, one request)
python -m hl_screener run --pool 60        # first real look, ~15–25 min
python -m hl_screener run                  # full 300-account pool, roughly an hour
python -m hl_screener inspect 0xADDRESS    # every metric + last round trips for one trader
python -m hl_screener run --pool-file out/traders_2026-09-17.csv   # re-screen exactly those accounts (from cache, minutes)
```

`--pool-file` pins the pool to the addresses in a previous `traders_<date>.csv` (or a plain list)
instead of sampling the leaderboard. Use it after a code or config change: the leaderboard cache
expires after 6 h and a fresh sample would mean fresh downloads.

```powershell
python -m hl_screener run --cached-only --end 2026-09-17   # interrupted run -> result, no new downloads
```

`--cached-only` screens every in-band account whose history is already on disk for that run date
and downloads nothing else. The cache is keyed by run date, so `--end` must name the date the
interrupted run used (its default is the UTC day it started). Outputs are written normally.

## Web UI

The same commands behind a local page, plus a results browser and a config editor. Still in
PowerShell with the venv active:

```powershell
python -m hl_screener ui                  # opens http://127.0.0.1:8765 in your browser
python -m hl_screener ui --port 9000 --no-browser
```

- **Run** — demo (offline), pool preview, full screen (pool size, run date, no-split) and
  inspect-one-address. Output streams live; Stop kills the job. Pool and inspect output is
  parsed into tables with an Inspect button per address.
- **Results** — every `run_<date>.json` in `out/` and `out/demo/`: verdict with its checks,
  the out-of-sample benchmark table, cumulative follower PnL of the shortlist basket,
  in-sample score vs out-of-sample follower ROI, sortable shortlist and pooled-trader tables,
  drop reasons, file downloads, and the markdown report.
- **Config** — edit `config.toml` in place. A save is validated by loading it exactly as the
  tool would, and rejected with the reason if it does not load.
- **Design** — the design system the page is built from (see `design/README.md`).

The server is standard library only, binds to localhost, runs one job at a time, and never talks
to Hyperliquid itself — it runs `python -m hl_screener …` as a subprocess and reads `out/`.

Hyperliquid allows 1200 request-weight per minute per IP; fills cost 20 + 1 per 20
fills, so the history download is the slow part. Every response is cached on disk
under `data/cache/`, so re-running with different filters the same day costs nothing
(the default run date is today's UTC midnight precisely so the cache keys stay
stable). Use `--end 2026-08-01` to run "as of" an earlier date.

Outputs land in `out/`:

| file | content |
|---|---|
| `report_<date>.md` | verdict, out-of-sample benchmark table, shortlist, drop reasons |
| `traders_<date>.csv` | every pooled trader: in-sample and out-of-sample metrics, follower results, reasons |
| `shortlist_<date>.csv` | the same rows for the shortlist only |
| `shortlist_trades_<date>.csv` | every simulated follower trade for the shortlist (entry/exit penalty, fees, funding) — the paper trader is compared against this |
| `run_<date>.json` | benchmarks, drop counts, timings and the exact config used |

## Running on a server (Coolify)

The paper test should not depend on a PC staying on. `Dockerfile` + `docker-compose.yml` run
two services from one image: `ui` (the web page, port 8765) and `paper` (the trader, following
`paper/leaders.csv`), sharing volumes for `data/` and `out/`.

1. Push this folder to a git repository (GitHub, GitLab, Gitea…).
2. In Coolify: **New resource → Docker Compose**, point it at the repository, branch `main`.
3. Environment: set `HL_UI_PASSWORD` to a long password. The page asks for it on every visit
   (any user name). Without it the page is open to whoever finds the address.
   Optional: `HL_PAPER_LEADERS` to follow a different file than `paper/leaders.csv`.
4. Domains: attach your domain to the `ui` service, port 8765. Coolify's proxy adds HTTPS.
5. Deploy. The Paper tab shows the trader as "live, running elsewhere" once its heartbeat
   arrives. Screens can be started from the Run tab exactly as on the PC; the cache and the
   results live on the volumes. A 3,000-account screen needs a few GB of RAM on the server.

To change the leaders, edit `paper/leaders.csv` (any `shortlist_<date>.csv` works) and redeploy.
The database keeps the old accounts' history; new addresses get fresh $1,000 accounts.

Locally the same stack runs with `docker compose up --build`.

## Reading the report honestly

- **Copy gap** = leader ROI − follower ROI on the same trades. It is always positive;
  what matters is how much of the leader's edge survives it. A great trader with a
  gap larger than their return is not copyable.
- **Persistence (Spearman)** near 0 or negative over a decent n means the in-sample
  filters are picking noise. Do not run a forward test on that shortlist; widen the
  pool or tighten filters and re-run instead.
- The most likely honest outcome is "shortlist beats the naive top-5 basket but not
  BTC hold". That is a real result: it says don't deploy, and it cost a few hours.
- The penalty model is a model. The forward paper test replaces it with measured
  `l2Book` slippage at detection time; if measured slippage is much worse than the
  modelled `avg_penalty_bps`, raise `latency_z` / `impact_coeff` and re-screen.

## Tuning

All parameters are in `config.toml`, grouped by purpose, with the reasoning inline.
The ones that change results most: `hold_multiple_of_latency` (copyability),
`max_thin_notional_share` (crowding on thin books), `max_top2_share` (luck),
`follower_max_leverage` and `builder_fee_bps` (set 10–20 if you would copy through
a platform that takes a cut rather than trading Hyperliquid directly).

## Known limits

- The API only serves the 10,000 most recent fills per address. Very active accounts
  get a shorter effective window and are flagged `fills_truncated`.
- Follower returns use a fixed equity base per leader (no compounding) and
  aggregate each round trip to VWAP entry/exit; scale-ins are copied as one entry.
- Leaderboard membership is itself a survivorship-biased universe (blown-up accounts
  drop below the equity floor). The out-of-sample split protects against
  performance look-ahead, not against that.
- Funding is charged on the follower's max notional for the whole hold — slightly
  conservative.
- Drawdown and the follower cap are per trade in sequence; a leader running many
  positions at once exposes a follower to their *sum*. Check `n_coins` and the
  trade timestamps in `shortlist_trades_<date>.csv` before trusting a low DD.

## Deployment rule (written down before anyone is attached to a result)

Go live only if the shortlist's copy-adjusted return beats both benchmarks over the
forward paper period with a drawdown you would actually sit through, and then with
a slice of capital sized so one leader blowing up is a shrug.

## Forward paper test

The step after a shortlist exists: follow each leader live, one virtual account each, and see
whether the screen's numbers hold in real conditions. No order is ever sent.

```powershell
python -m hl_screener paper                                   # newest out/shortlist_*.csv, $1,000 per leader
python -m hl_screener paper --leaders out/shortlist_2026-09-17.csv --equity 500
python -m hl_screener paper --status                          # print the accounts and exit
```

- Every leader fill arrives over Hyperliquid's public WebSocket (`userFills`, no key). The
  follower mirrors it with the simulator's sizing rule (fixed equity base, leader's leverage,
  capped at `follower_max_leverage`), priced by walking the real `l2Book` at detection time,
  paying taker + builder fees and hourly funding on open positions. Reduces and closes are
  proportional, so an account that joined mid-position still exits in step.
- Measured slippage and latency are stored next to the penalty the screen modelled for that
  leader. That comparison is the point of the test. If measured slippage is much worse than
  `avg_penalty_bps`, raise `latency_z` / `impact_coeff` and re-screen.
- State lives in `data/paper/paper.db` (SQLite). The service resumes from it after a restart
  and reconciles fills missed while disconnected (marked `late`, priced at the current book).
- The web page's **Paper** tab shows the accounts, equity curves against the leader's own
  account, open positions, fills and events, and can start or stop the service. A service
  started from the page stops with the page's server; for a test that must survive reboots,
  run the command above in its own window.
