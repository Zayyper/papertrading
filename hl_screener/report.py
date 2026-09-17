"""CSV + Markdown outputs for a run."""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from .walkforward import RunResult


def _d(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _pct(x: Any, nd: int = 1) -> str:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "n/a"
    if x != x or math.isinf(x):
        return "n/a" if x != x else ("inf" if x > 0 else "-inf")
    return f"{x * 100:.{nd}f}%"


def _num(x: Any, nd: int = 2) -> str:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "n/a"
    if x != x:
        return "n/a"
    if math.isinf(x):
        return "inf"
    return f"{x:.{nd}f}"


def flatten_rows(res: RunResult) -> pd.DataFrame:
    recs = []
    for r in res.rows:
        rec: dict[str, Any] = {
            "address": r["address"],
            "display_name": r["display_name"],
            "account_value": r["account_value"],
            "lb_month_roi": r["lb_month_roi"],
            "n_fills": r["n_fills"],
            "fills_truncated": r["fills_truncated"],
            "score": r["score"],
            "passed": not r["reasons"],
            "reasons": ";".join(r["reasons"]),
        }
        for pfx, m in (("is_", r["is"]), ("oos_", r["oos"])):
            for k, v in m.to_dict().items():
                if k in ("address", "window_start", "window_end", "fills_truncated"):
                    continue
                rec[pfx + k] = v
        for pfx, f in (("is_f_", r["is_follower"]), ("oos_f_", r["oos_follower"])):
            for k, v in f.items():
                rec[pfx + k] = v
        recs.append(rec)
    df = pd.DataFrame(recs)
    if not df.empty:
        df = df.sort_values(["passed", "score"], ascending=[False, False])
    return df


def write_outputs(res: RunResult, out_dir: str | Path) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = _d(res.end_ms)
    paths: dict[str, Path] = {}

    df = flatten_rows(res)
    paths["all_traders"] = out / f"traders_{stamp}.csv"
    df.to_csv(paths["all_traders"], index=False)

    sl = df[df["address"].isin([r["address"] for r in res.shortlist])] if not df.empty else df
    paths["shortlist"] = out / f"shortlist_{stamp}.csv"
    sl.to_csv(paths["shortlist"], index=False)

    # per-trade detail for the shortlist (what the paper trader will be compared against)
    trades = []
    for r in res.shortlist:
        for tag, f in (("is", r["_f_is"]), ("oos", r["_f_oos"])):
            for s in f.trades:
                trades.append({"address": r["address"], "window": tag, **s.__dict__})
    paths["shortlist_trades"] = out / f"shortlist_trades_{stamp}.csv"
    pd.DataFrame(trades).to_csv(paths["shortlist_trades"], index=False)

    paths["run_json"] = out / f"run_{stamp}.json"
    with open(paths["run_json"], "w") as fh:
        json.dump({
            "end": _d(res.end_ms), "is_start": _d(res.is_start), "is_end": _d(res.is_end),
            "pool_size": res.pool_size, "loaded": res.loaded,
            "benchmarks": _jsonable(res.benchmarks), "timings": res.timings,
            "drop_counts": res.drop_counts, "config": res.config,
            "shortlist": [r["address"] for r in res.shortlist],
        }, fh, indent=2)

    paths["report"] = out / f"report_{stamp}.md"
    paths["report"].write_text(render_markdown(res), encoding="utf-8")
    return paths


def render_markdown(res: RunResult) -> str:
    b = res.benchmarks
    cfg = res.config
    L: list[str] = []
    L.append(f"# Hyperliquid niche-trader screen — {_d(res.end_ms)}\n")
    L.append(f"In-sample: {_d(res.is_start)} → {_d(res.is_end)}  |  Out-of-sample: {_d(res.is_end)} → {_d(res.oos_end)}  ")
    L.append(f"Pool: {res.pool_size} accounts in {cfg['equity_min_usd']:,.0f}–{cfg['equity_max_usd']:,.0f} USD; "
             f"{res.loaded} with usable history; {sum(1 for r in res.rows if not r['reasons'])} passed all filters; "
             f"shortlist {len(res.shortlist)}.\n")
    L.append(f"Copy assumptions: latency {cfg['latency_s']}s (min median hold {cfg['latency_s']*cfg['hold_multiple_of_latency']/60:.0f} min), "
             f"taker {cfg['taker_fee_bps']} bps + builder {cfg['builder_fee_bps']} bps per leg, follower equity {cfg['follower_equity_usd']:,.0f} USD per leader, "
             f"max follower leverage {cfg['follower_max_leverage']}x, funding {'on' if cfg['apply_funding'] else 'off'}.\n")

    v = b.get("verdict", {})
    L.append("## Verdict\n")
    L.append(f"**Proceed to forward paper test: {'YES' if v.get('proceed_to_paper_test') else 'NO'}**\n")
    for k, ok in v.get("checks", {}).items():
        L.append(f"- {'✅' if ok else '❌'} {k}")
    L.append("")

    L.append("## Out-of-sample benchmarks (same simulator, same window)\n")
    L.append("| Basket | Leaders | Trades | Follower ROI | Max DD | Leaders positive |")
    L.append("|---|---:|---:|---:|---:|---:|")
    for name, key in (("Shortlist (niche)", "shortlist"), ("All that passed filters", "all_passed"), (f"Naive top-{cfg['naive_top_n']} by in-sample ROI", "naive_top_n")):
        p = b.get(key, {})
        L.append(f"| {name} | {p.get('n_leaders', 0)} | {p.get('n_trades', 0)} | {_pct(p.get('roi'))} | {_pct(p.get('max_dd'))} | {_pct(p.get('leaders_positive_share'))} |")
    btc = b.get("btc_hold", {})
    L.append(f"| BTC buy & hold | – | – | {_pct(btc.get('roi'))} | {_pct(btc.get('max_dd'))} | – |")
    L.append("")
    pers = b.get("persistence", {})
    L.append(f"Persistence: Spearman(in-sample score, out-of-sample follower ROI) = {_num(pers.get('spearman'))} over n={pers.get('n')} traders that passed filters. "
             f"Near zero or negative means the in-sample screen carries no information — do not run the paper test on this shortlist.\n")

    L.append("## Shortlist\n")
    L.append("| # | Address | Equity | IS trades | IS follower ROI | IS DD | Copy gap IS | Med hold (min) | Med lev | Thin % | OOS follower ROI | OOS DD | Score |")
    L.append("|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for i, r in enumerate(res.shortlist, 1):
        m, fi, fo = r["is"], r["is_follower"], r["oos_follower"]
        L.append(f"| {i} | `{r['address']}` | {_num(r['account_value'], 0)} | {m.n_trades} | {_pct(fi['roi'])} | {_pct(fi['max_dd'])} | "
                 f"{_pct(fi['copy_gap'])} | {_num(m.median_hold_min, 0)} | {_num(m.median_leverage, 1)}x | {_pct(m.thin_share, 0)} | "
                 f"{_pct(fo['roi'])} | {_pct(fo['max_dd'])} | {_num(r['score'])} |")
    L.append("")

    L.append("## Why traders were dropped (in-sample filters; one trader can fail several)\n")
    for k, n in sorted(res.drop_counts.items(), key=lambda kv: -kv[1]):
        L.append(f"- {k}: {n}")
    L.append("")
    L.append("## Notes\n")
    L.append("- Follower ROI is on a fixed equity base per leader (no compounding), sized as the leader's leverage capped at the follower max.")
    L.append("- Penalty per leg = half-spread(tier) + latency move (z·σ₁ₕ·√(latency/3600)) + √-impact (σ_day·√(notional/day volume)). Replace with measured l2Book slippage in the paper test.")
    L.append("- Only the 10,000 most recent fills per address are available from the API; traders flagged `fills_truncated` have a shorter effective in-sample window.")
    L.append("- The naive top-N basket picks the best in-sample *leader* ROI at the split date, i.e. what a leaderboard copier would have chosen then. No look-ahead.")
    return "\n".join(L) + "\n"


def _jsonable(o: Any) -> Any:
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, float):
        return None if (o != o or math.isinf(o)) else o
    return o
