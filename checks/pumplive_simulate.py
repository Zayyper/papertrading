"""The copy tool's own transactions, simulated on mainnet: a bonding-curve buy and sell and a PumpSwap buy and sell, each
built by hl_screener.pumptx for a wallet that has just traded that coin (so it holds SOL and the coin), and run by the
public RPC's simulateTransaction without signatures. Read-only: nothing is signed or sent, no key is involved.

    python checks/pumplive_simulate.py        (from the project folder)
"""
import os
import random
import sys
import time

sys.path.insert(0, os.getcwd())
from hl_screener.pumpfun import D_BUY, D_TRADE, b58, parse_amm_trade, parse_trade  # noqa: E402
from hl_screener.pumptx import (AMM_GLOBAL_CONFIG, AMM_LISTS, AMM_PROGRAM, PUMP_GLOBAL, PUMP_LISTS, PUMP_PROGRAM,  # noqa: E402
                                TIP_ACCOUNTS, TIP_LAMPORTS, WSOL, Rpc, buy_ixs, coin_of, compose, own_trade, parse_global,
                                sell_ixs, sim_error, sol_for, tokens_for)

rpc = Rpc(timeout=20)


def recent_buy(program: str, disc: bytes):
    """A recent successful buy on `program`: (event, mint)."""
    import base64
    for s in rpc.call("getSignaturesForAddress", [program, {"limit": 60}]):
        if s["err"]:
            continue
        time.sleep(1.1)                                             # getTransaction: 10 calls per 10 s
        tx = rpc.call("getTransaction", [s["signature"], {"encoding": "json", "maxSupportedTransactionVersion": 1}])
        for line in (tx or {}).get("meta", {}).get("logMessages") or []:
            if not line.startswith("Program data: "):
                continue
            b = base64.b64decode(line[14:])
            if b[:8] != disc:
                continue
            if disc == D_TRADE:
                e = parse_trade(b)
                if e and e["buy"] and e["sol_quote"] and e["sol"] > 10**7:
                    return e, b58(e["mint"])
            else:
                e = parse_amm_trade(b)
                if e and e["sol"] > 10**7:
                    _, pool = rpc.account(b58(e["pool"]))
                    if b58(pool[75:107]) == WSOL:                  # quoted in SOL
                        return e, b58(pool[43:75])
    raise SystemExit(f"no recent buy found on {program}")


def run(name: str, program: str, disc: bytes, lists: dict) -> None:
    e, mint = recent_buy(program, disc)
    user, coin = b58(e["user"]), coin_of(mint, e)
    tp = rpc.account(mint)[0]
    lamports = 10_000_000                                           # 0.01 SOL: well within what the wallet just spent
    want = tokens_for(coin, lamports)
    for side, (ixs, cu) in (("buy", buy_ixs(coin, user, lamports, int(want * 0.75), tp, lists)),
                            ("sell", sell_ixs(coin, user, e["tok"] // 2, 0, tp, lists, close=False))):
        res = rpc.simulate(compose(user, ixs, cu_limit=cu, tip_to=random.choice(TIP_ACCOUNTS), tip_lamports=TIP_LAMPORTS))
        got = own_trade(res.get("logs") or [], user)
        what = (f"{got['tok']:,} tokens for {(got['sol'] + got['fee']) / 1e9:.5f} SOL (priced {want:,})" if side == "buy" else
                f"{e['tok'] // 2:,} tokens for {(got['sol'] - got['fee']) / 1e9:.5f} SOL (priced {sol_for(coin, e['tok'] // 2) / 1e9:.5f})") if got else ""
        print(f"{name} {side:4s} {mint} as {user}: " + ("ok" if res.get("err") is None else f"FAILED {sim_error(res)}")
              + f", {res.get('unitsConsumed'):,} CU of {cu:,}, {len(bytes(compose(user, ixs, cu_limit=cu)))} bytes" + (f", {what}" if what else ""))


run("curve", PUMP_PROGRAM, D_TRADE, parse_global(rpc.account(PUMP_GLOBAL)[1], PUMP_LISTS))
run("pool ", AMM_PROGRAM, D_BUY, parse_global(rpc.account(AMM_GLOBAL_CONFIG)[1], AMM_LISTS))
