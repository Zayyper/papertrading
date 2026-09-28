"""The go-live rule in numbers: which golden wallets have earned live copies. Fixed on 2026-09-28, before the results
were seen, so nobody can talk themselves into it.

A golden wallet qualifies only if all four hold. Its paper copies at today's stake, 0.25 SOL (the 0.1 SOL ones from
before 2026-09-26 are another size and do not count): enough of them closed, and they made money after fees. Its
dry-run buys (pumplive): the real transactions would have gone through, and bought about what the paper copies
bought. Anything short of that is a NO, a missing dry run included.
"""
from __future__ import annotations

import collections
import statistics
from typing import Any

from .pumpfun import PAPER_STAKE_SOL, _pct, connect

GO_MIN_CLOSED = 100        # closed paper copies: with fewer, one lucky coin sets the average
GO_MIN_ROI = 0.03          # profit per closed copy after fees and tips: a margin for what paper cannot see
GO_MIN_THROUGH = 0.80      # dry-run buys the RPC says would have gone through ...
GO_MIN_DRY = 20            # ... out of at least this many
GO_MAX_TOKEN_GAP = 0.05    # the dry run's median tokens may fall this far short of the paper copy's; more is fine


def go_report(db_path, stake: float = PAPER_STAKE_SOL) -> dict[str, Any]:
    """Every golden wallet against the rule, on its `stake` SOL paper copies and its dry-run buys: its numbers, whether
    it qualifies, and what fails if not. Those that qualify first, then the most closed copies."""
    c = connect(db_path, readonly=True)
    try:
        paper = c.execute("""SELECT f.wallet, COUNT(b.id), COUNT(s.pnl), COALESCE(SUM(s.pnl), 0) FROM follow f
                             LEFT JOIN pfills b ON b.wallet = f.wallet AND b.side = 'buy' AND ABS(b.sol - ?) < 1e-9
                             LEFT JOIN pfills s ON s.wallet = b.wallet AND s.mint = b.mint AND s.side = 'sell'
                             WHERE f.golden_ever = 1 GROUP BY f.wallet""", (stake,)).fetchall()
        dry = []
        if c.execute("SELECT 1 FROM sqlite_master WHERE name = 'lorders'").fetchone():   # absent before the dry run
            # each buy next to the paper copy of the same buy at the same stake: a 0.1 SOL copy got fewer tokens
            dry = c.execute("""SELECT d.wallet, d.status = 'sim_ok', d.tok, p.tok FROM lorders d
                               LEFT JOIN pfills p ON p.wallet = d.wallet AND p.mint = d.mint AND p.side = 'buy'
                                                 AND ABS(p.sol - ?) < 1e-9
                               WHERE d.mode = 'dry' AND d.side = 'buy' AND d.status IN ('sim_ok', 'sim_err')
                                 AND (d.status = 'sim_ok' OR d.slot IS NOT NULL)""",   # no slot: the RPC failed, not the buy
                            (stake,)).fetchall()
    finally:
        c.close()
    by = collections.defaultdict(list)
    for w, ok, tok, paper_tok in dry:
        by[w].append((ok, tok, paper_tok))
    wallets = []
    for w, copies, closed, pnl in paper:
        d = by[w]
        gaps = [tok / paper_tok - 1 for ok, tok, paper_tok in d if ok and tok and paper_tok]
        r = {"wallet": w, "copies": copies, "closed": closed, "roi": pnl / (closed * stake) if closed else None,
             "dry_n": len(d), "through": sum(ok for ok, _, _ in d) / len(d) if d else None,
             "token_gap": statistics.median(gaps) if gaps else None}   # the median: a few big fills cannot hide the rest
        r["why"] = [text for ok, text in _checks(r) if not ok]
        r["go"] = not r["why"]
        wallets.append(r)
    wallets.sort(key=lambda r: (not r["go"], -r["closed"], r["wallet"]))
    return {"rule": {"stake_sol": stake, "min_closed": GO_MIN_CLOSED, "min_roi": GO_MIN_ROI, "min_through": GO_MIN_THROUGH,
                     "min_dry": GO_MIN_DRY, "max_token_gap": GO_MAX_TOKEN_GAP}, "wallets": wallets}


def _checks(r: dict[str, Any]) -> list[tuple[bool, str]]:
    """The four tests on one wallet: passed or not, and how each reads in the log. One decimal more than the rule's
    numbers, so a value just short of a threshold never prints as the threshold itself."""
    def test(ok: bool, text: str, need: str) -> tuple[bool, str]:
        return ok, f"{text} ok" if ok else f"{text} (need {need})"
    out = [test(r["closed"] >= GO_MIN_CLOSED, f"{r['closed']} closed", f"{GO_MIN_CLOSED}"),
           test(r["roi"] is not None and r["roi"] >= GO_MIN_ROI, f"{_pct(r['roi'])} per copy", _pct(GO_MIN_ROI))]
    if not r["dry_n"]:
        return [*out, (False, "no dry run yet")]
    return [*out, test(r["dry_n"] >= GO_MIN_DRY and r["through"] >= GO_MIN_THROUGH,
                       f"dry run {r['through']:.1%} through (n {r['dry_n']})", f"{GO_MIN_THROUGH:.0%} of {GO_MIN_DRY}+"),
            test(r["token_gap"] is not None and r["token_gap"] >= -GO_MAX_TOKEN_GAP,
                 f"tokens vs paper {_pct(r['token_gap'])}", f"{_pct(-GO_MAX_TOKEN_GAP, 0)} or better")]


def go_lines(db_path, stake: float = PAPER_STAKE_SOL) -> list[str]:
    """For the log, after the dry run's lines: the rule in words, each golden wallet with a copy or a dry-run buy
    against it, and the verdict. Nothing while there is no golden wallet."""
    ws = go_report(db_path, stake)["wallets"]
    if not ws:
        return []
    go = [r["wallet"] for r in ws if r["go"]]
    return [f"go-live rule (set 2026-09-28, before any result): a golden wallet goes live only with {GO_MIN_CLOSED}+ "
            f"closed {stake:g} SOL paper copies making {_pct(GO_MIN_ROI)} or more per copy after fees, "
            f"{GO_MIN_THROUGH:.0%}+ of {GO_MIN_DRY}+ dry-run buys going through, and the dry run's median tokens at most "
            f"{GO_MAX_TOKEN_GAP:.0%} under the paper copy's",
            *(f"go-live {r['wallet']}: {'GO' if r['go'] else 'NO'} — {', '.join(text for _, text in _checks(r))}"
              for r in ws if r["copies"] or r["dry_n"]),
            f"go-live: {', '.join(go)} qualify" if go else f"go-live: none of {len(ws)} golden wallets qualifies yet"]
