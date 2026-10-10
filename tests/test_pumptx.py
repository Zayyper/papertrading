import struct
import time

import pytest

pytest.importorskip("solders")

from solders.hash import Hash  # noqa: E402

from hl_screener.pumptx import (AMM_LISTS, AMM_PROGRAM, PUMP_PROGRAM, TOKEN_2022_PROGRAM, Refused, Rpc, RpcError,  # noqa: E402
                                canonical_pool, compose, curve_buy_ixs, curve_sell_ixs, parse_global, pool_buy_ixs, pool_sell_ixs,
                                sim_error, sol_for, tokens_for)

T22 = TOKEN_2022_PROGRAM


def listed(ix) -> list[str]:
    return [("S" if m.is_signer else "") + ("W" if m.is_writable else "R") + " " + str(m.pubkey) for m in ix.accounts]


# mainnet 3uVy6PSv7YTbEAT88TJA4HMauB1CpEA4wsvoKHSXfB8NZMR6ywxs5rzZMKppP5chMDxp7JHepMC4ou4Vr8vUtN5h (2026-09-26): a direct
# buy_exact_sol_in on a Token-2022 coin, as the message listed its accounts
BUY_TX = """R 4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf
W 7VtfL8fvgNfhz17qKRMjzQEXgbdpnHHHQRh54R9jP2RJ
R DwpiXVuTviMXHfoydySLhdbwcCC1aPBMDw8fWpScnXDf
W CP7Y4n55BDropCRnBp3LVUFV2pF4SnfuELboPqeoeSBq
W H536aAcDkRdQbFoeGewnF73PapDpS3KGasvsB3zBaWJJ
W 6Rif5XVo2k6NL36NxoH83nohjUnd3SGBhxxtRempqq9M
SW FFWz3afFp6jNLACoyfAxCQVP7LVPQSwZupTKnTFRXSeh
R 11111111111111111111111111111111
R TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb
W Hpx6h9LWz46d5wt17Ak9pB2Zfya6TBsovp7rVkih4cdY
R Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1
R 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P
R Hq2wp8uJ9jCPsYgNHex8RtqdvMPfVGoYwjvF1ATiwn2Y
W 3e8SPaD8NqZ5ZnHGbAKTorn4DJRRvPhikSxJaxN23N9t
R 8Wf5TiAheLUqBrKXeYg2JtAFFMWtKdG2BSFgqUcPVwTt
R pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ
R 6BuLFBCnwiCS2zVsriheouyHUq6HfEHkJHVGETwnEkQi
W 5cjcW9wExnJJiqgLjq7DEG75Pm6JBgE1hNv4B2vHXUW6""".splitlines()

# mainnet 3zM8YmGeNNgU7ia9dU2duq51CTg7ie7byKdp5KXTfy5oeKjUvZCr1rKeqP7NMasEdG2s5cjxo6qp9jUGD3mpi7pr: a direct sell
SELL_TX = """R 4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf
W 7hTckgnGnLQR6sdH7YkqFTAA7VwTfYFaZ6EhEsU3saCX
R GD2xqeHqXq6JSHEVeHbWPSkNZPu88T3NZi8D8u36pump
W 4pJzZ5E91uyFnLCQr7P41PGZMHkWWVBczJr7WaEnkFPV
W 5j79YnabYxJLLYEVDkmUneN2H3YyLsTMRSomjUsDCnPN
W 2Yv35d4ND82jL8ecyjGJ8huLGiA6ewmLc7h2ALrG9bew
SW 5BFuw7KyK5ryyvBwv4uJYwwNMnXPqGJGcf6okqtnhmFJ
R 11111111111111111111111111111111
W 52sXBLCs6ZWYQAKzURfBRcu5CjPcxWTLmV25KMx2sPbJ
R TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb
R Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1
R 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P
R 8Wf5TiAheLUqBrKXeYg2JtAFFMWtKdG2BSFgqUcPVwTt
R pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ
R AAD7DfbfkgBHq4rvvF55pCvwTgRhuGTvve2GfuKYpbDM
W 5eHhjP8JaYkz83CWwvGU2uMUXefd3AazWGx4gpcuEEYD""".splitlines()


def test_a_curve_buy_is_the_one_a_real_wallet_sent():
    coin = {"mint": "DwpiXVuTviMXHfoydySLhdbwcCC1aPBMDw8fWpScnXDf", "creator": "9XnB8yCHz2qeQCthkyvYWzsfHspY18kBdvNJD1SKAuA2",
            "fee_recipient": "7VtfL8fvgNfhz17qKRMjzQEXgbdpnHHHQRh54R9jP2RJ", "buyback": "5cjcW9wExnJJiqgLjq7DEG75Pm6JBgE1hNv4B2vHXUW6"}
    args = bytes.fromhex("c562532a00000000e96448cab10e0000")
    (create, buy), cu = curve_buy_ixs(coin, "FFWz3afFp6jNLACoyfAxCQVP7LVPQSwZupTKnTFRXSeh", *struct.unpack("<QQ", args), T22, {})
    assert str(buy.program_id) == PUMP_PROGRAM and listed(buy) == BUY_TX       # every account, in order, with its flags
    assert bytes(buy.data) == bytes.fromhex("38fc74089edfcd5f") + args + b"\0"    # the same 25 bytes it sent
    assert str(create.accounts[1].pubkey) == BUY_TX[5].split()[1]               # its token account is created first


def test_a_curve_sell_is_the_one_a_real_wallet_sent_then_the_account_is_closed():
    coin = {"mint": "GD2xqeHqXq6JSHEVeHbWPSkNZPu88T3NZi8D8u36pump", "creator": "ESSUYwwDVrfWgGCFcanFmgvb2vp6gDqEuLaDqKwUfC3f",
            "fee_recipient": "7hTckgnGnLQR6sdH7YkqFTAA7VwTfYFaZ6EhEsU3saCX", "buyback": "5eHhjP8JaYkz83CWwvGU2uMUXefd3AazWGx4gpcuEEYD"}
    (sell, close), _ = curve_sell_ixs(coin, "5BFuw7KyK5ryyvBwv4uJYwwNMnXPqGJGcf6okqtnhmFJ", 702_121_406_671, 1, T22, {})
    assert listed(sell) == SELL_TX
    assert bytes(sell.data) == bytes.fromhex("33e685a4017f83adcf70b279a30000000100000000000000")
    assert str(close.accounts[0].pubkey) == SELL_TX[5].split()[1] and str(close.program_id) == T22
    (cash_sell,), _ = curve_sell_ixs({**coin, "cashback": True}, "5BFuw7KyK5ryyvBwv4uJYwwNMnXPqGJGcf6okqtnhmFJ", 1, 0, T22, {}, close=False)
    assert len(cash_sell.accounts) == 17 and listed(cash_sell)[-2:] == SELL_TX[-2:]   # a cashback coin: its volume account first


def test_recipients_come_from_the_global_account_by_the_coins_mode():
    raw = bytearray(1087)
    for at, n, first in ((41, 1, 1), (162, 7, 2), (483, 1, 9), (516, 7, 10), (741, 8, 17)):
        for i in range(n):
            raw[at + 32 * i:at + 32 * (i + 1)] = bytes([first + i]) * 32
    g = parse_global(bytes(raw))
    assert [len(g[k]) for k in ("fee", "reserved", "buyback")] == [8, 8, 8]
    coin = {"mint": "GD2xqeHqXq6JSHEVeHbWPSkNZPu88T3NZi8D8u36pump", "creator": "ESSUYwwDVrfWgGCFcanFmgvb2vp6gDqEuLaDqKwUfC3f"}
    user = "5BFuw7KyK5ryyvBwv4uJYwwNMnXPqGJGcf6okqtnhmFJ"
    (_, buy), _ = curve_buy_ixs(coin, user, 1, 1, T22, g)
    assert str(buy.accounts[1].pubkey) in g["fee"] and str(buy.accounts[-1].pubkey) in g["buyback"]
    (_, buy), _ = curve_buy_ixs({**coin, "mayhem": True}, user, 1, 1, T22, g)
    assert str(buy.accounts[1].pubkey) in g["reserved"]                         # a mayhem coin's recipients are the reserved ones


# mainnet 5F6tsS4XkV7aWDXWYxJR5wcQFvK6cCmZyYbroUVRGGHoPRB7oGNbm4mzUhkf4a7fvxK56P39cVp2fuNdDaSvZYRK: a direct PumpSwap
# buy_exact_quote_in on a graduated Token-2022 coin; it turned volume tracking on, which makes #19 writable
POOL_BUY_TX = """W 6zhMKK3GC7C9srDWAdVvVt1mtXTagVX9euRDFFzrgeLA
SW 4jafoRx8VmqZvJ3ctmrXvURv74kDs2sc9zcCLhfGdZf5
R ADyA8hdefvWN2dbGGWFotbzWxrAvLW83WG6QCVXvJKqw
R 5jcH4EjDJzQVKfQqYKcDCzbG77Lz6yaYuv39Gh1Vpump
R So11111111111111111111111111111111111111112
W AuG87bL2iX46ovG4MoSpwDhgdXDWUH2jtmxaUenWr6jY
W 2HvFRVPTTNpghANc4jT5uhUPabg8w1a3suDxQuXr6Cn4
W 7v9ETKHhxAhwpq2A5QhFCG9segjzkCBHxPxJbZSQbbvh
W 6Xv42eXKDYkdgdPrAyrhCp5dWEwmg4bYgemkVKkGxKqH
R 62qc2CNXwrYqQScmEdiZFFAnJR262PxWEuNQtxfafNgV
W 94qWNrtmfn42h3ZjUZwWvK1MEo9uVmmrBPd2hpNjYDjb
R TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb
R TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA
R 11111111111111111111111111111111
R ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL
R GS4CU59F31iL7aR2Q8zVS8DRrcRnXX1yjQ66TqNVQnaR
R pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA
W EzCTvLkJnNMFc7Doq2CGu4GjFR5BeJdwm1FUxAwsSy4A
R J73GDbpMg2HKB6zQFo2ToEjJiwNSrQrJK7SMzQ9YvaEN
W C2aFPdENg4A2HQsmrd5rTw5TaYBX5Ku887cWjbFKtZpw
W AxtawyPdyLqLG7y21LQxXqDX5acrnHudsswswv32igNi
R 5PHirr8joyTMp9JMm6nW7hNDVyEYdkzDqazxPD7RaTjx
R pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ
R 7oSku2js7ud75nB6ygT35aFYRsfANf5skMFfdWWkfHTd
R 5YxQFdt3Tr9zJLvkFccqXVUwhdTWJQc1fFg2YPbxvxeD
W HjQjngTDqoHE6aaGhUqfz9aQ7WZcBRjy5xB8PScLSr8i""".splitlines()
POOL_COIN = {"mint": "5jcH4EjDJzQVKfQqYKcDCzbG77Lz6yaYuv39Gh1Vpump", "pool": "6zhMKK3GC7C9srDWAdVvVt1mtXTagVX9euRDFFzrgeLA",
             "creator": "DZ6fRooozrthCRPteBiF3hZoUPrqrEtAXbTYWRyj2GyA", "fee_recipient": "62qc2CNXwrYqQScmEdiZFFAnJR262PxWEuNQtxfafNgV",
             "buyback": "5YxQFdt3Tr9zJLvkFccqXVUwhdTWJQc1fFg2YPbxvxeD"}
POOL_USER = "4jafoRx8VmqZvJ3ctmrXvURv74kDs2sc9zcCLhfGdZf5"


def test_a_pumpswap_buy_is_the_one_a_real_wallet_sent_inside_a_wrap_and_unwrap():
    args = bytes.fromhex("b54c0500000000007dad730000000000")
    ixs, cu = pool_buy_ixs(POOL_COIN, POOL_USER, *struct.unpack("<QQ", args), T22, {})
    assert cu == 200_000                                                         # a cashback pool buy used 151,241 (2026-10-07)
    swap = ixs[4]
    ours = POOL_BUY_TX[:19] + ["R " + POOL_BUY_TX[19].split()[1]] + POOL_BUY_TX[20:]   # volume tracking off: read-only
    assert str(swap.program_id) == AMM_PROGRAM and listed(swap) == ours
    assert bytes(swap.data) == bytes.fromhex("c62e1552b4d9e870") + args + b"\0"
    wsol = POOL_BUY_TX[6].split()[1]
    assert [str(ix.accounts[0].pubkey) if i in (2, 5) else str(ix.accounts[1].pubkey) for i, ix in enumerate(ixs) if i != 4] == [
        wsol, wsol, wsol, POOL_BUY_TX[5].split()[1], wsol]    # wrap: create, fund, sync; the coin's account; unwrap after
    assert canonical_pool(POOL_COIN["mint"]) == POOL_COIN["pool"]                 # the pool follows from the coin alone


def test_a_pumpswap_sell_is_the_one_a_real_wallet_sent():
    # mainnet yJxP97j52QdaktYLKRefkU4AtsnEYULAiSSDCmUymPkq69nGzXuu5v54j67pp86VeCSM1V4hgc6eWpnSmubKTBr, the same wallet and pool
    ixs, _ = pool_sell_ixs(POOL_COIN, POOL_USER, 63_306_310, 0, T22, {})
    sell = ixs[1]
    assert listed(sell) == POOL_BUY_TX[:19] + POOL_BUY_TX[21:]                    # no volume accounts on a sell
    assert bytes(sell.data) == bytes.fromhex("33e685a4017f83ad46fac503000000000000000000000000")
    assert len(ixs) == 4 and str(ixs[3].accounts[0].pubkey) == POOL_BUY_TX[5].split()[1]   # then unwrap, and close the coin's account
    g = parse_global(bytes(range(256)) * 4, AMM_LISTS)
    ixs, _ = pool_sell_ixs({**POOL_COIN, "fee_recipient": None, "buyback": None, "creator": "11111111111111111111111111111111"},
                           POOL_USER, 1, 0, T22, g, close=False)
    assert len(ixs[1].accounts) == 23 and str(ixs[1].accounts[9].pubkey) in g["fee"]   # no creator: no pool_v2


def test_a_copy_is_priced_like_the_paper_copy_and_fits_one_transaction():
    coin = {"vsol": 37_052_323_640, "vtok": 868_771_432_598_378, "fee": 0.0125}  # the curve the real buy above left
    tok = tokens_for(coin, 250_000_000)
    assert tok == int(868_771_432_598_378 - 37_052_323_640 * 868_771_432_598_378 / (37_052_323_640 + 250_000_000 / 1.0125))
    assert 0.97 < sol_for({**coin, "vsol": coin["vsol"] + 246_913_580, "vtok": coin["vtok"] - tok}, tok) / 250_000_000 / (1 - 0.0125) ** 2 < 1.01
    coin = {"mint": "DwpiXVuTviMXHfoydySLhdbwcCC1aPBMDw8fWpScnXDf", "creator": "9XnB8yCHz2qeQCthkyvYWzsfHspY18kBdvNJD1SKAuA2",
            "fee_recipient": "7VtfL8fvgNfhz17qKRMjzQEXgbdpnHHHQRh54R9jP2RJ", "buyback": "5cjcW9wExnJJiqgLjq7DEG75Pm6JBgE1hNv4B2vHXUW6"}
    user = "FFWz3afFp6jNLACoyfAxCQVP7LVPQSwZupTKnTFRXSeh"
    ixs, cu = curve_buy_ixs(coin, user, 250_000_000, tok, T22, {})
    tx = compose(user, ixs, cu_limit=cu, tip_to="96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5", tip_lamports=1_000_000)
    assert len(bytes(tx)) < 1232                                                 # Solana's packet limit, no lookup table needed


def test_a_failure_names_the_account_its_constraint_failed_on():
    moved = {"err": {"InstructionError": [3, {"Custom": 2006}]}, "logs": [   # a mainnet simulation, 2026-09-30
        "Program log: AnchorError caused by account: creator_vault. Error Code: ConstraintSeeds. Error Number: 2006. "
        "Error Message: A seeds constraint was violated.", "Program log: Left:", "Program log: Right:"]}
    assert sim_error(moved) == "ConstraintSeeds (creator_vault)"               # the coin's creator moved after the copied buy
    assert sim_error({"err": {}, "logs": ["Program log: AnchorError occurred. Error Code: TooLittleSolReceived. "
                                          "Error Number: 6003. Error Message: slippage."]}) == "TooLittleSolReceived"


class Answer:
    def __init__(self, status, body=None):
        self.status_code, self.body = status, body

    def json(self):
        return self.body


def test_an_endpoint_that_says_no_refused_the_transaction_and_one_that_did_not_answer_may_have_taken_it():
    rpc = Rpc(send_urls=("https://a", "https://b"))
    for status, body, refused in ((429, None, True), (400, None, True), (200, {"error": {"code": -32602}}, True), (503, None, False)):
        rpc.local.http = type("Http", (), {"post": lambda self, *a, answer=Answer(status, body), **k: answer})()
        with pytest.raises(RpcError) as e:
            rpc.call("sendTransaction", [])
        assert isinstance(e.value, Refused) is refused                           # a 5xx may come from in front of a sender that took it
    tx = compose("FFWz3afFp6jNLACoyfAxCQVP7LVPQSwZupTKnTFRXSeh", [])

    def answers(a, b):
        def call(method, params, url=None):
            e = {"https://a": a, "https://b": b}[url]
            if e is not None:
                raise e
        return call
    rpc.call = answers(Refused("sendTransaction: HTTP 429"), Refused("sendTransaction: {'code': -32602}"))
    with pytest.raises(Refused):
        rpc.send(tx)                                                             # every one said no: nothing went out
    rpc.call = answers(Refused("sendTransaction: HTTP 429"), TimeoutError("read timed out"))
    with pytest.raises(RpcError) as e:
        rpc.send(tx)
    assert not isinstance(e.value, Refused) and "read timed out" in str(e.value)   # one may have taken it: not refused
    rpc.call = answers(Refused("sendTransaction: HTTP 429"), None)
    assert rpc.send(tx) == str(tx.signatures[0])


def test_a_blockhash_refresh_refused_keeps_the_last_one_while_it_can_still_land():
    rpc = Rpc()

    def refused(method, params, url=None):
        raise Refused("getLatestBlockhash: HTTP 429")
    rpc.call = refused
    last = Hash.new_unique()
    rpc._hash = (time.time() - 30, last)
    assert rpc.blockhash() == last                                               # 30 s old: a transaction with it still lands
    rpc._hash = (time.time() - 70, last)
    with pytest.raises(Refused):
        rpc.blockhash()                                                          # too old to land: the error, not a doomed send


def test_a_default_key_logged_as_fee_recipient_is_no_recipient():
    """2026-10-10 23:03 UTC, the first live buy of 3gHrfi's copies: the trade it copied logged the default key as its
    fee recipient, the buy named it, and pump.fun failed it (ConstraintMut, fee_recipient). Such a key, logged or in an
    empty slot of the global's list, is no recipient: one from the list is named instead, on a buy as on a sell."""
    from hl_screener.pumptx import DEFAULT_KEY, PUMP_LISTS, _fee_to, curve_buy_ixs, curve_sell_ixs, parse_global
    fee, res, bb = ("4ACfpUFoaSD9bfPdeu6DBt89gB6ENTeHBXCAi87NhDEE", "D2L6yPZ2FmmmTKPgzaMKdhu6EWZcTpLy1Vhx8uvZe7NZ",
                    "9bnz4RShgq1hAnLnZbP8kbgBg1kEmcJBYQq3gQbmnSta")
    g = {"fee": [fee], "reserved": [res], "buyback": [bb]}
    assert _fee_to({"fee_recipient": DEFAULT_KEY}, g) == fee and _fee_to({"fee_recipient": DEFAULT_KEY, "mayhem": True}, g) == res
    assert _fee_to({"fee_recipient": bb}, g) == bb                    # a real one logged is still the one named
    coin = {"mint": "BtAMWjq6e82fyCD5f6hw6WPunArrF6ZiNZdTqjmppump", "creator": "2nyhqdwKcJZR2vcqCyrYsaPVdAnFoJjiksCXJ7hfEYgD",
            "fee_recipient": DEFAULT_KEY, "mayhem": False, "cashback": False, "pool": None}
    tp = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
    for ixs, _ in (curve_buy_ixs(coin, bb, 10**8, 1, tp, g), curve_sell_ixs(coin, bb, 10**6, 0, tp, g)):
        swap = [ix for ix in ixs if str(ix.program_id) == "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"][0]
        assert str(swap.accounts[1].pubkey) == fee and swap.accounts[1].is_writable
    assert all(v == [] for v in parse_global(bytes(1000), PUMP_LISTS).values())   # empty slots are dropped
