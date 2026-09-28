from hl_screener.pumpfun import connect
from hl_screener.pumpgo import go_lines, go_report
from hl_screener.pumplive import LIVE_SCHEMA


def db(tmp_path, *golden, dry_table=True):
    c = connect(tmp_path / "pump.db")
    if dry_table:
        c.executescript(LIVE_SCHEMA)
    c.executemany("INSERT INTO follow(wallet, added_at, golden_ever) VALUES (?, 0, 1)", [(w,) for w in golden])
    return c


def copies(c, wallet, n, pnl, stake=0.25, first=0, sold=True):
    """`n` paper copies of coins first.., each bought 10^12 tokens and, if `sold`, closed making `pnl` SOL."""
    for i in range(first, first + n):
        c.execute("INSERT INTO pfills(wallet, mint, side, sol, tok) VALUES (?, ?, 'buy', ?, 1e12)", (wallet, f"coin{i}", stake))
        if sold:
            c.execute("INSERT INTO pfills(wallet, mint, side, sol, tok, pnl) VALUES (?, ?, 'sell', ?, 1e12, ?)",
                      (wallet, f"coin{i}", stake + pnl, pnl))


def dry_run(c, wallet, toks, errs=0, first=0, unanswered=0):
    """Dry-run buys of coins first..: one that would have gone through per token amount in `toks`, then `errs` that
    would not (a simulation can fail after its trade was logged: those carry 5 * 10^11 tokens), then `unanswered`
    that the RPC never simulated (no slot): those say nothing about the buy."""
    for i, tok in enumerate([*toks, *[None] * errs], start=first):
        c.execute("INSERT INTO lorders(mode, wallet, mint, side, status, tok, slot) VALUES ('dry', ?, ?, 'buy', ?, ?, 1)",
                  (wallet, f"coin{i}", "sim_ok" if tok else "sim_err", tok or 5e11))
    for i in range(first + len(toks) + errs, first + len(toks) + errs + unanswered):
        c.execute("INSERT INTO lorders(mode, wallet, mint, side, status, err) VALUES ('dry', ?, ?, 'buy', 'sim_err', 'HTTP 429')",
                  (wallet, f"coin{i}"))


def done(c, tmp_path):
    c.commit()
    c.close()
    return tmp_path / "pump.db"


def test_a_wallet_that_meets_all_four_numbers_qualifies_and_comes_first(tmp_path):
    c = db(tmp_path, "Gold", "Busy")
    copies(c, "Gold", 100, pnl=0.01)                     # exactly 100 closed, +4% each ...
    copies(c, "Gold", 20, pnl=0, first=100, sold=False)  # ... and 20 still open, which count for neither
    dry_run(c, "Gold", [0.97e12] * 16, errs=4)           # exactly 80% of exactly 20, 3% fewer tokens than paper
    c.execute("INSERT INTO lorders(mode, wallet, mint, side, status) VALUES ('dry', 'Gold', 'coin99', 'buy', 'pending')")
    copies(c, "Busy", 150, pnl=0.005)                    # more copies than Gold, making +2%: short of +3%
    path = done(c, tmp_path)
    rep = go_report(path)
    assert [(r["wallet"], r["go"]) for r in rep["wallets"]] == [("Gold", True), ("Busy", False)]
    assert rep["rule"] == {"stake_sol": 0.25, "min_closed": 100, "min_roi": 0.03, "min_through": 0.80, "min_dry": 20,
                           "max_token_gap": 0.05}     # fixed on 2026-09-28: changing one is changing the rule
    lines = go_lines(path)
    assert lines[0].startswith("go-live rule (set 2026-09-28") and "100+ closed 0.25 SOL paper copies" in lines[0]
    assert lines[1:] == [
        "go-live Gold: GO — 100 closed ok, +4.0% per copy ok, dry run 80.0% through (n 20) ok, tokens vs paper -3.0% ok",
        "go-live Busy: NO — 150 closed ok, +2.0% per copy (need +3.0%), no dry run yet",
        "go-live: Gold qualify"]


def test_a_wallet_one_closed_copy_short_is_a_no_that_says_it_needs_100(tmp_path):
    c = db(tmp_path, "Near", "Idle")
    copies(c, "Near", 99, pnl=0.01)
    copies(c, "Near", 5, pnl=0, first=99, sold=False)    # open: 104 copies, still 99 closed
    dry_run(c, "Near", [1e12] * 20)
    path = done(c, tmp_path)
    near, idle = go_report(path)["wallets"]
    assert near["why"] == ["99 closed (need 100)"] and not near["go"]
    assert go_lines(path)[1:] == [          # Idle has no copy and no dry run yet: counted, not listed
        "go-live Near: NO — 99 closed (need 100), +4.0% per copy ok, dry run 100.0% through (n 20) ok, tokens vs paper +0.0% ok",
        "go-live: none of 2 golden wallets qualifies yet"]


def test_the_0_1_sol_copies_from_before_the_switch_are_not_counted(tmp_path):
    c = db(tmp_path, "Old")
    copies(c, "Old", 200, pnl=0.05, stake=0.1)           # +50% each, at the old size
    copies(c, "Old", 30, pnl=0.01, first=200)            # the 0.25 SOL ones
    dry_run(c, "Old", [2.5e12] * 20)                     # coins whose paper copy was 0.1 SOL: nothing to compare with
    (r,) = go_report(done(c, tmp_path))["wallets"]
    assert (r["copies"], r["closed"], round(r["roi"], 9), r["token_gap"]) == (30, 30, 0.04, None)
    assert r["why"] == ["30 closed (need 100)", "tokens vs paper n/a (need -5% or better)"]


def test_a_dry_run_that_mostly_failed_or_has_under_20_buys_is_a_no_on_that_alone(tmp_path):
    c = db(tmp_path, "Slow", "Short")
    for w in ("Slow", "Short"):
        copies(c, w, 100, pnl=0.01)
    dry_run(c, "Slow", [1e12] * 5, errs=15)              # 25% through; the failed ones' tokens are not compared
    dry_run(c, "Short", [1e12] * 19, unanswered=5)       # 19 answers: the 5 the RPC never simulated are not buys that failed
    short, slow = go_report(done(c, tmp_path))["wallets"]
    assert slow["why"] == ["dry run 25.0% through (n 20) (need 80% of 20+)"] and slow["token_gap"] == 0
    assert short["why"] == ["dry run 100.0% through (n 19) (need 80% of 20+)"]


def test_the_dry_run_may_buy_more_tokens_than_paper_but_its_median_not_over_5_percent_fewer(tmp_path):
    c = db(tmp_path, "More", "Less")
    for w, toks in (("More", [1.3e12] * 20), ("Less", [0.93e12] * 11 + [2e12] * 9)):   # Less: its mean is +41%
        copies(c, w, 100, pnl=0.01)
        dry_run(c, w, toks)
    more, less = go_report(done(c, tmp_path))["wallets"]
    assert more["wallet"] == "More" and more["go"]
    assert less["why"] == ["tokens vs paper -7.0% (need -5% or better)"]


def test_without_the_dry_run_table_a_wallet_is_a_no_with_no_dry_run_yet(tmp_path):
    c = db(tmp_path, "Gold", dry_table=False)
    copies(c, "Gold", 120, pnl=0.01)
    assert go_lines(done(c, tmp_path))[1:] == ["go-live Gold: NO — 120 closed ok, +4.0% per copy ok, no dry run yet",
                                               "go-live: none of 1 golden wallets qualifies yet"]


def test_no_golden_wallet_means_no_go_live_lines(tmp_path):
    c = db(tmp_path)
    c.execute("INSERT INTO follow(wallet, added_at, golden_ever, sniper_now) VALUES ('Sniper', 0, 0, 1)")
    copies(c, "Sniper", 120, pnl=0.01)
    dry_run(c, "Sniper", [1e12] * 20)
    path = done(c, tmp_path)
    assert go_lines(path) == [] and go_report(path)["wallets"] == []
