"""Command line entry point.

  python -m hl_screener run   [--config config.toml] [--end 2026-09-16] [--no-split] [--pool 100]
  python -m hl_screener pool  [--config config.toml]            # just show who would be screened
  python -m hl_screener inspect 0xADDRESS [--config config.toml] # one trader, full detail
  python -m hl_screener ui    [--port 8765] [--no-browser]       # local web page for all of the above
  python -m hl_screener pump collect|report                      # pump.fun snipers and traders on Solana
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .api import HyperliquidAPI
from .config import Config


def _end_ms(s: str | None) -> int:
    """Run date as ms. Default = today's UTC midnight, so re-runs on the same day reuse the disk cache."""
    if not s:
        return (int(time.time()) // 86_400) * 86_400 * 1000
    dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _read_addresses(path: Path) -> list[str]:
    """Addresses from a traders_<date>.csv (its `address` column) or a plain one-per-line file."""
    import csv
    import re
    text = path.read_text(encoding="utf-8")
    if text.lstrip().lower().startswith("address"):
        with open(path, newline="", encoding="utf-8") as fh:
            return [row["address"].strip() for row in csv.DictReader(fh) if row.get("address", "").strip().startswith("0x")]
    return re.findall(r"0x[0-9a-fA-F]{40}", text)


def newest_shortlist(out_dir: Path) -> Path | None:
    files = sorted((p for p in out_dir.glob("shortlist_*.csv") if not p.name.startswith("shortlist_trades")), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def _api(cfg: Config) -> HyperliquidAPI:
    return HyperliquidAPI(cfg.api_url, cfg.leaderboard_url, Path(cfg.data_dir) / "cache",
                          cfg.request_weight_per_minute, cfg.http_timeout_s)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="hl_screener")
    p.add_argument("--config", default="config.toml" if Path("config.toml").exists() else None)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="full walk-forward screen")
    r.add_argument("--end", help="run date YYYY-MM-DD (UTC), default now")
    r.add_argument("--no-split", action="store_true", help="no out-of-sample window (screen only)")
    r.add_argument("--pool", type=int, help="override pool_max_accounts")
    r.add_argument("--pool-file", help="re-screen exactly the addresses in this file (a traders_<date>.csv from a previous "
                                       "run, or one address per line) instead of sampling the leaderboard")
    r.add_argument("--cached-only", action="store_true",
                   help="screen every in-band account already downloaded for the run date and download nothing new "
                        "(turn an interrupted run into a result; combine with --end <that run's date>)")

    sub.add_parser("pool", help="show the pool that would be screened")
    sub.add_parser("demo", help="run the whole pipeline on synthetic data (no network) to see what a report looks like")

    i = sub.add_parser("inspect", help="metrics + round trips for one address")
    i.add_argument("address")
    i.add_argument("--end")

    pp = sub.add_parser("paper", help="forward paper test: follow leaders live, priced at the real order book (no orders sent)")
    pp.add_argument("--leaders", help="shortlist_<date>.csv, traders csv, or a plain address list (default: newest out/shortlist_*.csv)")
    pp.add_argument("--equity", type=float, help="virtual equity per leader (default follower_equity_usd)")
    pp.add_argument("--max-leverage", type=float, help="cap on follower notional / equity (default follower_max_leverage)")
    pp.add_argument("--db", help="sqlite file (default <data_dir>/paper/paper.db)")
    pp.add_argument("--status", action="store_true", help="print the current state of the paper accounts and exit")

    pf = sub.add_parser("pump", help="pump.fun on Solana: find snipers and consistently profitable traders, test copyability")
    pf.add_argument("action", choices=["collect", "report", "dossier"])
    pf.add_argument("wallets", nargs="*", help="dossier: these wallets (default: every golden wallet and every wallet copied 5+ times)")
    pf.add_argument("--db", help="sqlite file (default <data_dir>/pump/pump.db)")
    pf.add_argument("--ws", help="Solana websocket RPC (default SOLANA_WS_URL, else the public mainnet endpoint)")
    pf.add_argument("--retention-days", type=float, help="keep this many days of tokens (default PUMP_RETENTION_DAYS, else 3)")
    pf.add_argument("--latency-slots", type=int, help="report: slots between a wallet's trade and the copier's, 400 ms each "
                                                       "(default: the measured feed delay + 1, at least 2)")
    pf.add_argument("--stake", type=float, default=0.1, help="report: SOL the copier puts into each copied buy")

    ct = sub.add_parser("copytest", help="copy test on Hyperliquid: book captured after every leader trade (Postgres), "
                                         "copy gap per wallet, delay curve, random-wallet test, holdout judged once")
    ct.add_argument("action", choices=["collect", "report", "gap", "holdout", "log"])
    ct.add_argument("address", nargs="?", help="gap: the wallet, every copied order itemised (the hand check)")
    ct.add_argument("--plan", default="copytest.toml", help="the test plan (frozen in the database on the first collect)")
    ct.add_argument("--db-url", help="Postgres URL (default HL_DATABASE_URL)")
    ct.add_argument("--delay", type=float, help="report/gap: one delay in seconds instead of the plan's list")
    ct.add_argument("--fee-bps", type=float, help="report/gap: taker + builder fee instead of the plan's")
    ct.add_argument("--leverage", type=float, help="report/gap: follower leverage cap instead of the plan's")
    ct.add_argument("--slippage-bps", type=float, help="report/gap: fill at the captured mid +/- this many bps instead of walking the book")
    ct.add_argument("--no-funding", action="store_true", help="report/gap: leave funding out")

    u = sub.add_parser("ui", help="local web page: run jobs, watch progress, browse results, edit config")
    u.add_argument("--host", default="127.0.0.1")
    u.add_argument("--port", type=int, default=8765)
    u.add_argument("--no-browser", action="store_true", help="do not open the page in a browser")

    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if a.cmd == "ui":
        # deliberately before Config.load: a broken config.toml can be fixed from the page itself
        from .webui.server import serve
        return serve(a.config, host=a.host, port=a.port, open_browser=not a.no_browser)

    cfg = Config.load(a.config)

    if a.cmd == "pool":
        import random
        from .walkforward import select_pool
        pool = select_pool(_api(cfg).leaderboard(), cfg, random.Random(cfg.random_seed))
        for row in pool:
            m = row["perf"].get("month", {})
            print(f"{row['address']}  equity={row['account_value']:>10,.0f}  month_roi={m.get('roi', float('nan')):+.3f}  alltime_vlm={row['perf'].get('allTime', {}).get('vlm', float('nan')):,.0f}")
        print(f"{len(pool)} accounts")
        return 0

    if a.cmd == "inspect":
        from .walkforward import load_trader
        from .metrics import compute_metrics
        from .liquidity import build_coin_stats, tiers_map
        from .simulate import simulate_follower
        api = _api(cfg)
        end = _end_ms(a.end)
        start = end - cfg.lookback_days * 86_400_000
        row = {"address": a.address.lower(), "account_value": float("nan"), "perf": {}, "display_name": None}
        td = load_trader(api, row, start, end, cfg)
        if td is None:
            print("no usable history (young account, no fills, or API error). Raw portfolio keys:")
            try:
                pf = api.portfolio(row["address"])
                for k, v in pf.items():
                    print(f"  {k}: {len(v.get('account_value', []))} equity points, {len(v.get('pnl', []))} pnl points")
                raw, trunc = api.user_fills(row["address"], start, end)
                print(f"  fills in window: {len(raw)} (truncated={trunc})")
                if raw:
                    print("  first fill:", json.dumps(raw[0])[:400])
            except Exception as e:  # noqa: BLE001
                print("  error:", e)
            return 1
        cs = build_coin_stats(api, cfg, {t.coin for t in td.trips}, start, end)
        m = compute_metrics(td.address, td.trips, td.equity, td.pnl_curve, tiers_map(cs), td.effective_start, end, td.fills_truncated)
        f = simulate_follower(td.trips, td.equity, cs, None, cfg, td.effective_start, end)
        for k, v in m.to_dict().items():
            print(f"{k:>26}: {v}")
        print("--- follower (no funding) ---")
        for k, v in f.summary().items():
            print(f"{k:>26}: {v}")
        print(f"--- last 15 round trips of {len(td.trips)} ---")
        for t in td.trips[-15:]:
            print(f"{datetime.fromtimestamp(t.open_time/1000, tz=timezone.utc):%Y-%m-%d %H:%M} {t.coin:>8} {'L' if t.direction>0 else 'S'} "
                  f"hold={t.hold_minutes:8.1f}min notional={t.max_notional:10,.0f} net={t.net_pnl:+9.2f} fills={t.n_fills}{' LIQ' if t.liquidated else ''}{'' if t.complete else ' (partial)'}")
        return 0

    if a.cmd == "pump":
        from . import pumpfun
        db = Path(a.db) if a.db else Path(cfg.data_dir) / "pump" / "pump.db"
        if a.action == "collect":
            return pumpfun.collect(db, a.ws or os.environ.get("SOLANA_WS_URL") or pumpfun.PUBLIC_WS,
                                   a.retention_days or float(os.environ.get("PUMP_RETENTION_DAYS") or 3),
                                   fallback_url=os.environ.get("SOLANA_WS_FALLBACK") or None)
        if not db.exists():
            print(f"no data at {db}: run `python -m hl_screener pump collect` first", file=sys.stderr)
            return 1
        if a.action == "dossier":
            from .pumpdossier import dossiers
            print("\n".join(dossiers(db, a.wallets or None)))
            return 0
        settled = pumpfun.settle_launches(db)
        if settled:
            print(f"settled {settled:,} launches into the permanent history")
        rep = pumpfun.build_report(db, latency_slots=a.latency_slots, stake_sol=a.stake)
        pumpfun.save_report(db, rep)
        pumpfun.update_follow(db, rep)
        pumpfun.print_report(rep)
        strat = pumpfun.strategy_report(db, stake_sol=a.stake)
        pumpfun.save_meta(db, "strategies", strat)
        if strat.get("rules"):
            print(f"\nexit rules on {strat['counts']['launches']:,} launches by {strat['counts']['makers']:,} repeat makers "
                  f"({strat['params']['hold_s'] / 60:.0f} min window; late60 and sol8 buy later):")
            for r in strat["rules"]:
                print(f"  {r['rule']:<15} {r['roi']:+7.1%} per launch  won {r['win_rate']:>4.0%}  total {r['pnl_sol']:+8.2f} SOL  n {r['n']:,}")
        return 0

    if a.cmd == "copytest":
        url = a.db_url or os.environ.get("HL_DATABASE_URL") or ("" if os.environ.get("PGHOST") else None)   # "": libpq's PG* variables
        if url is None:
            print("no database: set HL_DATABASE_URL (or PGHOST, PGUSER, PGPASSWORD, PGDATABASE) or pass --db-url", file=sys.stderr)
            return 2
        if a.action == "collect":
            from .tape import collect
            return collect(url, Path(a.plan), cfg, Path.cwd())
        from .copygap import cli as copygap_cli
        return copygap_cli(a, url)

    if a.cmd == "paper":
        from .paper import print_status
        from .paper import serve as paper_serve
        db = Path(a.db) if a.db else Path(cfg.data_dir) / "paper" / "paper.db"
        if a.status:
            return print_status(db, _api(cfg))
        leaders = Path(a.leaders) if a.leaders else newest_shortlist(Path(cfg.out_dir))
        if leaders is None or not leaders.exists():
            print("no leaders file: pass --leaders <shortlist_<date>.csv or address list>", file=sys.stderr)
            return 2
        return paper_serve(_api(cfg), cfg, leaders, db, a.equity or cfg.follower_equity_usd, a.max_leverage or cfg.follower_max_leverage)

    if a.cmd in ("run", "demo"):
        from .report import write_outputs
        from .walkforward import run
        if a.cmd == "demo":
            from .fake_api import FakeAPI
            end = _end_ms(None)
            api = FakeAPI(end)
            cfg.pool_max_accounts = 100
            cfg.min_trades = 30
            cfg.min_active_days = 15
            cfg.min_shortlist_leaders = 2
            out_dir = str(Path(cfg.out_dir) / "demo")
        else:
            if a.no_split:
                cfg.oos_days = 0
            if a.pool:
                cfg.pool_max_accounts = a.pool
            api = _api(cfg)
            end = _end_ms(a.end)
            out_dir = cfg.out_dir

        def progress(s: str) -> None:
            print(f"[{datetime.now():%H:%M:%S}] {s}", flush=True)

        pool_addresses = None
        if a.cmd == "run" and a.pool_file:
            pool_addresses = _read_addresses(Path(a.pool_file))
            if not pool_addresses:
                print(f"no 0x addresses found in {a.pool_file}", file=sys.stderr)
                return 2

        res = run(api, cfg, end, progress, pool_addresses=pool_addresses,
                  cached_only=bool(a.cmd == "run" and a.cached_only))
        paths = write_outputs(res, out_dir)
        print()
        print(Path(paths["report"]).read_text(encoding="utf-8"))
        print("files:")
        for k, v in paths.items():
            print(f"  {k:>16}: {v}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
