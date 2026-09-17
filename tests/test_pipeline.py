import json

from hl_screener.config import Config
from hl_screener.fake_api import FakeAPI
from hl_screener.report import render_markdown, write_outputs
from hl_screener.walkforward import run

END = 1_758_000_000_000  # 2025-09-16-ish, any fixed ms works


def _cfg() -> Config:
    cfg = Config()
    cfg.pool_max_accounts = 100
    cfg.min_trades = 30
    cfg.min_active_days = 15
    cfg.min_shortlist_leaders = 2
    return cfg


def test_pipeline_end_to_end_filters_personas(tmp_path):
    api = FakeAPI(END)
    cfg = _cfg()
    res = run(api, cfg, END)

    by = {r["display_name"]: r for r in res.rows}
    names = set(by)

    # excluded before any download: whale by equity band, young by account age
    assert "whale" not in names
    assert "young" not in names
    assert api.calls["user_fills"] < api.calls["portfolio"]  # young account never downloaded fills

    assert "good_swing" in names and not by["good_swing"]["reasons"], by["good_swing"]["reasons"]
    assert any(r.startswith("hold<") for r in by["scalper"]["reasons"])
    assert any(r.startswith("top2>") for r in by["lucky"]["reasons"])
    assert any(r.startswith("thin_share>") for r in by["thin_coin"]["reasons"])
    assert any(r.startswith("lev>") for r in by["degen"]["reasons"]) and any(r.startswith("liquidations") for r in by["degen"]["reasons"])
    assert any(r.startswith("inactive>") for r in by["inactive"]["reasons"])

    shortlist_names = {r["display_name"] for r in res.shortlist}
    assert "good_swing" in shortlist_names
    assert "scalper" not in shortlist_names and "degen" not in shortlist_names

    # copy gap is positive for everyone: follower always pays more than the leader
    for r in res.rows:
        f = r["is_follower"]
        if f["n_trades"] > 0 and f["copy_gap"] == f["copy_gap"]:
            assert f["copy_gap"] > 0

    b = res.benchmarks
    assert set(b["verdict"]["checks"]) == {"shortlist_has_leaders", "beats_naive_top_n", "beats_btc_hold", "drawdown_ok", "positive_oos"}
    assert b["shortlist"]["n_leaders"] >= 1
    assert b["btc_hold"]["roi"] == b["btc_hold"]["roi"]  # not NaN

    # outputs
    paths = write_outputs(res, tmp_path)
    for p in paths.values():
        assert p.exists() and p.stat().st_size > 0
    md = render_markdown(res)
    assert "## Verdict" in md and "good_swing" not in md  # addresses, not names, in the table
    with open(paths["run_json"]) as fh:
        j = json.load(fh)
    assert j["pool_size"] == res.pool_size and "config" in j


def test_no_split_mode_runs():
    api = FakeAPI(END)
    cfg = _cfg()
    cfg.oos_days = 0
    res = run(api, cfg, END)
    assert res.is_end == res.oos_end == END
    r = next(r for r in res.rows if r["display_name"] == "good_swing")
    assert r["is_follower"] == r["oos_follower"]


def test_deterministic_pool_selection():
    api = FakeAPI(END)
    cfg = _cfg()
    cfg.pool_max_accounts = 5
    a = run(api, cfg, END)
    b = run(FakeAPI(END), cfg, END)
    assert [r["address"] for r in a.rows] == [r["address"] for r in b.rows]
    assert a.pool_size == 5
