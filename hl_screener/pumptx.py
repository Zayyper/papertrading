"""Solana transactions for pump.fun's bonding curve and PumpSwap: the instructions of a copy's buy and sell, the
transaction around them, and a small JSON-RPC client.

Layouts: github.com/pump-fun/pump-public-docs (IDLs of 2026-09-12, docs/BREAKING_FEE_RECIPIENT.md) and pump.fun's SDKs,
checked against real mainnet transactions (tests/test_pumptx.py pins four of them account for account) and by
mainnet simulation (checks/pumplive_simulate.py). Nothing here holds a key: a transaction is signed only when the
caller passes one.
"""
from __future__ import annotations

import base64
import os
import random
import struct
import threading
import time
from typing import Any

PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
SLOT_S = 0.22                  # a slot's length: 265-280 a minute on mainnet, measured 2026-10-09 (0.4 earlier in Solana's life)
SYSTEM_PROGRAM = "11111111111111111111111111111111"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"      # every coin created with create_v2
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
WSOL = "So11111111111111111111111111111111111111112"
DEFAULT_KEY = "11111111111111111111111111111111"
# the bonding curve
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_GLOBAL = "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"                # ["global"]
PUMP_EVENT_AUTHORITY = "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1"       # ["__event_authority"]
GLOBAL_VOLUME_ACCUMULATOR = "Hq2wp8uJ9jCPsYgNHex8RtqdvMPfVGoYwjvF1ATiwn2Y"  # ["global_volume_accumulator"]
FEE_PROGRAM = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
FEE_CONFIG = "8Wf5TiAheLUqBrKXeYg2JtAFFMWtKdG2BSFgqUcPVwTt"                 # ["fee_config", pump program] under the fee program
BUY_EXACT_SOL_IN = bytes.fromhex("38fc74089edfcd5f")                       # sha256("global:buy_exact_sol_in")[:8]
SELL = bytes.fromhex("33e685a4017f83ad")                                   # sha256("global:sell")[:8], both programs
# PumpSwap
AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
AMM_GLOBAL_CONFIG = "ADyA8hdefvWN2dbGGWFotbzWxrAvLW83WG6QCVXvJKqw"           # ["global_config"]
AMM_EVENT_AUTHORITY = "GS4CU59F31iL7aR2Q8zVS8DRrcRnXX1yjQ66TqNVQnaR"       # ["__event_authority"]
AMM_GLOBAL_VOLUME_ACCUMULATOR = "C2aFPdENg4A2HQsmrd5rTw5TaYBX5Ku887cWjbFKtZpw"
AMM_FEE_CONFIG = "5PHirr8joyTMp9JMm6nW7hNDVyEYdkzDqazxPD7RaTjx"             # ["fee_config", AMM program] under the fee program
BUY_EXACT_QUOTE_IN = bytes.fromhex("c62e1552b4d9e870")                     # sha256("global:buy_exact_quote_in")[:8]
# where each global account keeps its recipient lists: (byte offset, count)
PUMP_LISTS = {"fee": ((41, 1), (162, 7)), "reserved": ((483, 1), (516, 7)), "buyback": ((741, 8),)}
AMM_LISTS = {"fee": ((57, 8),), "reserved": ((385, 1), (418, 7)), "buyback": ((643, 8),)}
# compute-unit limits: the most seen on mainnet on 2026-09-26, x1.3; a cashback pool buy used 151,241 (2026-10-07), so
# PumpSwap buys get 200,000. The priority fee is set for the whole limit: a higher one costs nothing more.
CURVE_BUY_CU, CURVE_SELL_CU, POOL_BUY_CU, POOL_SELL_CU = 130_000, 90_000, 200_000, 145_000
# around each swap, and what the paper copies pay (pumpfun.PRIORITY_SOL, TIP_SOL): a priority fee and a tip to Helius
# Sender's accounts. Until 2026-10-10 0.0005 + 0.001 SOL (Sender Max's least tip), 6 % of a 0.05 SOL round trip in the
# live test; since then Sender's SWQOS-only mode (one route, no buffer), whose least tip is 0.000005 SOL. Set by
# PUMP_LIVE_TIP_SOL and PUMP_LIVE_PRIORITY_SOL, read once here so the live copies and the paper pay the same.
SWQOS_MIN_TIP = 5_000          # lamports: Sender refuses less
SENDER_MAX_TIP = 1_000_000     # Sender Max, every route, wants this much; below it a send goes to the SWQOS-only endpoint
def env_num(name: str, default: float, most: float) -> float:
    """A number the collector reads from its environment: the default when blank, not a number or negative (a typo must
    not stop it: open copies still have to be sold), never above `most`."""
    try:
        v = float(os.environ.get(name) or default)
    except ValueError:
        return default
    return min(v, most) if v >= 0 else default


TIP_LAMPORTS = max(SWQOS_MIN_TIP, round(env_num("PUMP_LIVE_TIP_SOL", 0.00001, 0.01) * 1e9))
PRIORITY_LAMPORTS = round(env_num("PUMP_LIVE_PRIORITY_SOL", 0.0002, 0.005) * 1e9)
TIP_ACCOUNTS = ("4ACfpUFoaSD9bfPdeu6DBt89gB6ENTeHBXCAi87NhDEE", "D2L6yPZ2FmmmTKPgzaMKdhu6EWZcTpLy1Vhx8uvZe7NZ",
                "9bnz4RShgq1hAnLnZbP8kbgBg1kEmcJBYQq3gQbmnSta", "5VY91ws6B2hMmBFRsXkoAAdsPHBJwRfBht4DXox3xkwn",
                "2nyhqdwKcJZR2vcqCyrYsaPVdAnFoJjiksCXJ7hfEYgD", "2q5pghRs6arqVjRvT5gfgWfWcHWmw1ZuCzphgd5KfWGJ",
                "wyvPkWjVZz1M8fHQnMMCDTQDbkManefNNhweYk5WkcF", "3KCKozbAaF75qEU33jtzozcJ29yJuaLJTy2jFdzUY8bT",
                "4vieeGHPYPG2MmyPRcYjdiDmmhN3ww7hsFNap8pVN3Ey", "4TQLFNWK8AovT1gFvda5jfw2oJeRMKEmw7aH6MGBJ3or")


class Lag:
    """How late a trade reaches us, by its block time (whole seconds): now less that time, less the usual lag (the least
    of this minute's and the last's, so a stall's backlog never becomes the norm) and a second for the rounding. The
    live copies' age check and the paper's landing both read it: behind a database stall the slots handled look new."""

    def __init__(self) -> None:
        self.v = [float("inf"), float("inf"), 0.0]

    def see(self, ts: int | None, now: float) -> float:
        """Learn from one trade's block time; how late that trade is (0 when on time, or with no time)."""
        if not ts:
            return 0.0
        if now - self.v[2] > 60:
            self.v = [now - ts, self.v[0], now]
        else:
            self.v[0] = min(self.v[0], now - ts)
        return self.behind(ts, now)

    def behind(self, ts: int | None, now: float) -> float:
        return max(0.0, now - ts - min(self.v[:2]) - 1) if ts else 0.0


def pk(s):
    from solders.pubkey import Pubkey
    return s if not isinstance(s, (str, bytes)) else Pubkey.from_string(s) if isinstance(s, str) else Pubkey.from_bytes(s)


def pda(seeds: list, program: str):
    from solders.pubkey import Pubkey
    return Pubkey.find_program_address([s if isinstance(s, bytes) else bytes(pk(s)) for s in seeds], pk(program))[0]


def ata(owner, mint, token_program=TOKEN_PROGRAM):
    from solders.token.associated import get_associated_token_address
    return get_associated_token_address(pk(owner), pk(mint), pk(token_program))


def _meta(a, writable: bool = False, signer: bool = False):
    from solders.instruction import AccountMeta
    return AccountMeta(pk(a), signer, writable)


def create_ata_idempotent(payer, owner, mint, token_program=TOKEN_PROGRAM):
    from solders.instruction import Instruction
    return Instruction(pk(ATA_PROGRAM), bytes([1]), [
        _meta(payer, True, True), _meta(ata(owner, mint, token_program), True), _meta(owner), _meta(mint),
        _meta(SYSTEM_PROGRAM), _meta(token_program)])


def sync_native(account, token_program=TOKEN_PROGRAM):
    from solders.instruction import Instruction
    return Instruction(pk(token_program), bytes([17]), [_meta(account, True)])


def close_account(account, owner, token_program=TOKEN_PROGRAM):
    """Closes a token account and returns its rent to the owner (it must be empty, or wrapped SOL)."""
    from solders.instruction import Instruction
    return Instruction(pk(token_program), bytes([9]), [_meta(account, True), _meta(owner, True), _meta(owner, False, True)])


def transfer(src, dst, lamports: int):
    from solders.system_program import TransferParams, transfer as _transfer
    return _transfer(TransferParams(from_pubkey=pk(src), to_pubkey=pk(dst), lamports=int(lamports)))


def compose(payer, ixs: list, blockhash=None, keypair=None, cu_limit: int = 200_000, priority_lamports: int = PRIORITY_LAMPORTS,
            tip_to: str | None = None, tip_lamports: int = 0):
    """A v0 transaction: compute budget, the swap's instructions, the tip. Signed with `keypair` when one is given;
    otherwise it carries an empty signature and a zero blockhash, which only a simulation that skips both accepts."""
    from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
    from solders.hash import Hash
    from solders.message import MessageV0
    from solders.signature import Signature
    from solders.transaction import VersionedTransaction
    budget = [set_compute_unit_limit(cu_limit), set_compute_unit_price(priority_lamports * 1_000_000 // cu_limit)]
    tip = [transfer(payer, tip_to, tip_lamports)] if tip_to and tip_lamports else []
    msg = MessageV0.try_compile(pk(payer), budget + list(ixs) + tip, [], blockhash or Hash.default())
    if keypair is not None:
        return VersionedTransaction(msg, [keypair])
    return VersionedTransaction.populate(msg, [Signature.default()])


def parse_global(raw: bytes, lists: dict[str, tuple] = PUMP_LISTS) -> dict[str, list[str]]:
    """The recipient lists a trade names one of: `fee` for a normal coin, `reserved` for a mayhem coin (the other list
    fails with NotAuthorized), `buyback` for the account every trade has had to name since 2026-04-28. pump.fun's
    global account with PUMP_LISTS, PumpSwap's global config with AMM_LISTS."""
    from .pumpfun import b58
    keys = {name: [b58(raw[at + 32 * i:at + 32 * (i + 1)]) for at, n in spans for i in range(n)] for name, spans in lists.items()}
    return {name: [k for k in ks if k != DEFAULT_KEY] for name, ks in keys.items()}   # an empty slot is no recipient


def _fee_to(coin: dict[str, Any], g: dict[str, list[str]]) -> str:
    """The fee recipient a trade names: the one the copied trade's event logged, else one of the global's list. Since
    2026-10-10 some events log the default key there (seen on a curve coin): named as the recipient, pump.fun fails
    the trade with ConstraintMut (fee_recipient), the first live buy of 3gHrfi's copies. Such a key is no recipient."""
    logged = coin.get("fee_recipient")
    return logged if logged and logged != DEFAULT_KEY else random.choice(g["reserved" if coin.get("mayhem") else "fee"])


# ---------------------------------------------------------------------------
# the bonding curve
# ---------------------------------------------------------------------------
def _curve_common(coin: dict[str, Any], user: str, tp: str, g: dict[str, list[str]]):
    mint, me = pk(coin["mint"]), pk(user)
    curve = pda([b"bonding-curve", mint], PUMP_PROGRAM)
    head = [_meta(PUMP_GLOBAL), _meta(_fee_to(coin, g), True), _meta(mint), _meta(curve, True), _meta(ata(curve, mint, tp), True),
            _meta(ata(me, mint, tp), True), _meta(me, True, True), _meta(SYSTEM_PROGRAM)]
    vault = _meta(pda([b"creator-vault", coin["creator"]], PUMP_PROGRAM), True)
    buyback = coin.get("buyback") if coin.get("buyback") not in (None, DEFAULT_KEY) else random.choice(g["buyback"])
    tail = [_meta(pda([b"bonding-curve-v2", mint], PUMP_PROGRAM)), _meta(buyback, True)]
    return mint, me, head, vault, tail


def curve_buy_ixs(coin: dict[str, Any], user: str, lamports: int, min_tok: int, tp: str, g: dict[str, list[str]]) -> tuple[list, int]:
    """buy_exact_sol_in: `lamports` spent, fees included, for at least `min_tok` tokens or it fails. The legacy
    accounts, then the two the 2026-04-28 upgrade appended (without them: BuybackFeeRecipientMissing). The token
    account is created first if it is not there yet."""
    from solders.instruction import Instruction
    mint, me, head, vault, tail = _curve_common(coin, user, tp, g)
    accounts = head + [_meta(tp), vault, _meta(PUMP_EVENT_AUTHORITY), _meta(PUMP_PROGRAM), _meta(GLOBAL_VOLUME_ACCUMULATOR),
                       _meta(pda([b"user_volume_accumulator", me], PUMP_PROGRAM), True), _meta(FEE_CONFIG), _meta(FEE_PROGRAM)] + tail
    data = BUY_EXACT_SOL_IN + struct.pack("<QQ?", lamports, min_tok, False)      # track_volume off
    return [create_ata_idempotent(me, me, mint, tp), Instruction(pk(PUMP_PROGRAM), data, accounts)], CURVE_BUY_CU


def curve_sell_ixs(coin: dict[str, Any], user: str, tok: int, min_sol: int, tp: str, g: dict[str, list[str]],
                   close: bool = True) -> tuple[list, int]:
    """sell: `tok` tokens for at least `min_sol` lamports after fees. creator_vault and token_program swap places
    against the buy; a cashback coin also names the seller's volume accumulator first among the appended accounts.
    With `close`, the emptied token account is closed and its rent comes back."""
    from solders.instruction import Instruction
    mint, me, head, vault, tail = _curve_common(coin, user, tp, g)
    cashback = [_meta(pda([b"user_volume_accumulator", me], PUMP_PROGRAM), True)] if coin.get("cashback") else []
    accounts = head + [vault, _meta(tp), _meta(PUMP_EVENT_AUTHORITY), _meta(PUMP_PROGRAM), _meta(FEE_CONFIG), _meta(FEE_PROGRAM)] + cashback + tail
    ixs = [Instruction(pk(PUMP_PROGRAM), SELL + struct.pack("<QQ", tok, min_sol), accounts)]
    return ixs + ([close_account(ata(me, mint, tp), me, tp)] if close else []), CURVE_SELL_CU


# ---------------------------------------------------------------------------
# PumpSwap, where a coin trades once it graduated: quoted in wrapped SOL, so SOL is wrapped around each swap
# ---------------------------------------------------------------------------
def canonical_pool(mint: str) -> str:
    """The pool pump.fun migrates a coin into: index 0, created by the coin's pool authority, quoted in SOL."""
    authority = pda([b"pool-authority", mint], PUMP_PROGRAM)
    return str(pda([b"pool", (0).to_bytes(2, "little"), authority, mint, WSOL], AMM_PROGRAM))


def _pool_head(coin: dict[str, Any], me, mint, tp: str, g: dict[str, list[str]]) -> list:
    pool, fee_to = pk(coin["pool"]), _fee_to(coin, g)
    vault = pda([b"creator_vault", coin["creator"]], AMM_PROGRAM)      # still named when the creator is the default key
    return [_meta(pool, True), _meta(me, True, True), _meta(AMM_GLOBAL_CONFIG), _meta(mint), _meta(WSOL),
            _meta(ata(me, mint, tp), True), _meta(ata(me, WSOL), True), _meta(ata(pool, mint, tp), True), _meta(ata(pool, WSOL), True),
            _meta(fee_to), _meta(ata(fee_to, WSOL), True), _meta(tp), _meta(TOKEN_PROGRAM), _meta(SYSTEM_PROGRAM),
            _meta(ATA_PROGRAM), _meta(AMM_EVENT_AUTHORITY), _meta(AMM_PROGRAM), _meta(ata(vault, WSOL), True), _meta(vault)]


def _pool_tail(coin: dict[str, Any], mint, g: dict[str, list[str]]) -> list:
    """The accounts the 2026-04-28 upgrade appended: pool_v2 (when the coin has a creator), then a buyback recipient
    and its wrapped-SOL account. Without them: BuybackFeeRecipientMissing."""
    buyback = coin.get("buyback") if coin.get("buyback") not in (None, DEFAULT_KEY) else random.choice(g["buyback"])
    v2 =[_meta(pda([b"pool-v2", mint], AMM_PROGRAM))] if coin["creator"] != DEFAULT_KEY else []
    return v2 + [_meta(buyback), _meta(ata(buyback, WSOL), True)]


def pool_buy_ixs(coin: dict[str, Any], user: str, lamports: int, min_tok: int, tp: str, g: dict[str, list[str]]) -> tuple[list, int]:
    """buy_exact_quote_in: `lamports` of wrapped SOL spent, fees included, for at least `min_tok` tokens. The SOL is
    wrapped into the buyer's WSOL account first and what is left unwrapped after."""
    from solders.instruction import Instruction
    mint, me = pk(coin["mint"]), pk(user)
    uva = pda([b"user_volume_accumulator", me], AMM_PROGRAM)
    cashback = [_meta(ata(uva, WSOL), True)] if coin.get("cashback") else []
    accounts = (_pool_head(coin, me, mint, tp, g) + [_meta(AMM_GLOBAL_VOLUME_ACCUMULATOR), _meta(uva, True), _meta(AMM_FEE_CONFIG),
                _meta(FEE_PROGRAM)] + cashback + _pool_tail(coin, mint, g))
    wsol = ata(me, WSOL)
    swap = Instruction(pk(AMM_PROGRAM), BUY_EXACT_QUOTE_IN + struct.pack("<QQ?", lamports, min_tok, False), accounts)
    return [create_ata_idempotent(me, me, WSOL), transfer(me, wsol, lamports), sync_native(wsol),
            create_ata_idempotent(me, me, mint, tp), swap, close_account(wsol, me)], POOL_BUY_CU


def pool_sell_ixs(coin: dict[str, Any], user: str, tok: int, min_sol: int, tp: str, g: dict[str, list[str]],
                  close: bool = True) -> tuple[list, int]:
    """sell: `tok` tokens for at least `min_sol` lamports of wrapped SOL, unwrapped after. With `close`, the emptied
    token account is closed too."""
    from solders.instruction import Instruction
    mint, me = pk(coin["mint"]), pk(user)
    uva = pda([b"user_volume_accumulator", me], AMM_PROGRAM)
    cashback = [_meta(ata(uva, WSOL), True), _meta(uva, True)] if coin.get("cashback") else []
    accounts = _pool_head(coin, me, mint, tp, g) + [_meta(AMM_FEE_CONFIG), _meta(FEE_PROGRAM)] + cashback + _pool_tail(coin, mint, g)
    wsol = ata(me, WSOL)
    swap = Instruction(pk(AMM_PROGRAM), SELL + struct.pack("<QQ", tok, min_sol), accounts)
    ixs = [create_ata_idempotent(me, me, WSOL), swap, close_account(wsol, me)]
    return ixs + ([close_account(ata(me, mint, tp), me, tp)] if close else []), POOL_SELL_CU


def buy_ixs(coin: dict[str, Any], user: str, lamports: int, min_tok: int, tp: str, g: dict[str, list[str]]) -> tuple[list, int]:
    """The copy's buy on the coin's venue: the bonding curve, or PumpSwap once it graduated."""
    return (pool_buy_ixs if coin.get("pool") else curve_buy_ixs)(coin, user, lamports, min_tok, tp, g)


def sell_ixs(coin: dict[str, Any], user: str, tok: int, min_sol: int, tp: str, g: dict[str, list[str]],
             close: bool = True) -> tuple[list, int]:
    return (pool_sell_ixs if coin.get("pool") else curve_sell_ixs)(coin, user, tok, min_sol, tp, g, close)


# ---------------------------------------------------------------------------
# prices and events
# ---------------------------------------------------------------------------
def coin_of(mint: str, e: dict[str, Any]) -> dict[str, Any]:
    """What a trade of this coin needs, from a trade event: the reserves it left, and the accounts it named. A cashback
    coin's trades pay a cashback fee (30 bps on the curve, 95 on PumpSwap, 2026-10-07): its sells and pool buys name more."""
    from .pumpfun import FEE, b58
    return {"mint": mint, "pool": b58(e["pool"]) if "pool" in e else None, "creator": b58(e["creator"]),
            "fee_recipient": b58(e["fee_recipient"]), "mayhem": bool(e.get("mayhem")), "cashback": e.get("cashback_bps", 0) > 0,
            "vsol": e["vsol"], "vtok": e["vtok"],
            "fee": max(FEE, e["fee"] / e["sol"]) if e["sol"] and e["fee"] > 0 else FEE}   # a PumpSwap buy logs ~0 fee


def fresh_coin(rpc: "Rpc", mint: str, tp: str) -> dict[str, Any]:
    """The same, read from the chain, for a coin no trade has shown since a restart: its curve, or its pool once the
    curve completed. The fee recipient is left to the global lists."""
    from .pumpfun import FEE, b58
    curve = rpc.account(str(pda([b"bonding-curve", mint], PUMP_PROGRAM)))
    if curve is None:
        raise ValueError(f"no bonding curve for {mint}")
    d = curve[1]
    if not d[48]:                                                      # not complete: still on the curve
        return {"mint": mint, "pool": None, "creator": b58(d[49:81]), "mayhem": bool(d[81]), "cashback": bool(d[82]),
                "vtok": int.from_bytes(d[8:16], "little"), "vsol": int.from_bytes(d[16:24], "little"), "fee": FEE}
    pool = canonical_pool(mint)
    p = rpc.account(pool)
    if p is None:
        raise ValueError(f"{mint} completed its curve but has no pool yet")
    d = p[1]
    base = int(rpc.call("getTokenAccountBalance", [str(ata(pool, mint, tp))])["value"]["amount"])
    quote = int(rpc.call("getTokenAccountBalance", [str(ata(pool, WSOL))])["value"]["amount"])
    virtual = int.from_bytes(d[245:261], "little", signed=True) if len(d) >= 261 else 0
    return {"mint": mint, "pool": pool, "creator": b58(d[211:243]), "mayhem": bool(d[243]), "cashback": bool(d[244]),
            "vtok": base, "vsol": quote + virtual, "fee": FEE}


def tokens_for(coin: dict[str, Any], lamports: int) -> int:
    """Tokens `lamports` buy at the coin's reserves, after its fee: constant product, as the paper copies price it."""
    net = lamports / (1 + coin["fee"])
    return int(coin["vtok"] - coin["vsol"] * coin["vtok"] / (coin["vsol"] + net))


def sol_for(coin: dict[str, Any], tok: int) -> int:
    """Lamports selling `tok` tokens fetches at the coin's reserves, after its fee."""
    return int((coin["vsol"] - coin["vsol"] * coin["vtok"] / (coin["vtok"] + tok)) * (1 - coin["fee"]))


def own_trade(logs: list[str], user: str) -> dict[str, Any]:
    """What `user` paid or got in a transaction's logs, from pump.fun's or PumpSwap's own trade event (lamports and
    raw token units, as the events give them)."""
    from .pumpfun import D_BUY, D_SELL, D_TRADE, b58, parse_amm_trade, parse_trade
    for line in logs:
        if not line.startswith("Program data: "):
            continue
        try:
            b = base64.b64decode(line[14:])
        except ValueError:
            continue
        e = parse_trade(b) if b[:8] == D_TRADE else parse_amm_trade(b) if b[:8] in (D_BUY, D_SELL) else None
        if e is not None and b58(e["user"]) == user:
            return {"sol": e["sol"], "tok": e["tok"], "fee": e["fee"], "buy": e["buy"]}
    return {}


def sim_error(res: dict[str, Any]) -> str:
    """Why a simulation or a transaction failed, short: the program's own error name when its logs give one, and the
    account a constraint failed on. 'ConstraintSeeds (creator_vault)': the coin's creator moved to a fee-sharing config
    between the copied buy and ours (seen on 5 % of 4yFAz7's buys, 2026-09-30), so the copy named the old creator's vault."""
    import re
    for line in reversed(res.get("logs") or res.get("logMessages") or []):
        m = re.search(r"(?:caused by account: (\w+)\. )?Error Code: (\w+)", line)
        if m:
            return f"{m.group(2)} ({m.group(1)})" if m.group(1) else m.group(2)
        if "insufficient" in line.lower():
            return line.split(": ", 1)[-1][:120]
    return str(res.get("err"))[:160]


# ---------------------------------------------------------------------------
# JSON-RPC
# ---------------------------------------------------------------------------
HASH_KEEP_S = 45.0       # a blockhash older than this is not sent with, even when a fresh one cannot be read


class RpcError(Exception):
    pass


class Refused(RpcError):
    """The endpoint answered, and said no: a client error status (429 among them) or a JSON-RPC error. A transaction it
    refused did not go out there. A 5xx, a timeout or a dropped connection says nothing of the sort."""


class Rpc:
    """Plain JSON-RPC over HTTPS, one connection per thread, called from worker threads: the feed never waits on it.
    Reads go to `url`; a signed transaction goes to every `send_urls` endpoint at once (Helius Sender, Jito...)."""

    def __init__(self, url: str = PUBLIC_RPC, send_urls: tuple[str, ...] = (), timeout: float = 5.0) -> None:
        self.url, self.send_urls, self.timeout, self.local = url, send_urls, timeout, threading.local()
        self._hash: tuple[float, Any] | None = None

    def call(self, method: str, params: list, url: str | None = None) -> Any:
        import requests
        if getattr(self.local, "http", None) is None:
            self.local.http = requests.Session()
        r = self.local.http.post(url or self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                                 timeout=self.timeout)
        if r.status_code != 200:
            raise (Refused if r.status_code < 500 else RpcError)(f"{method}: HTTP {r.status_code}")
        body = r.json()
        if "error" in body:
            raise Refused(f"{method}: {str(body['error'])[:200]}")
        return body["result"]

    def account(self, addr: str) -> tuple[str, bytes] | None:
        """(owner, data) of an account, or None."""
        v = self.call("getAccountInfo", [addr, {"encoding": "base64", "commitment": "confirmed"}])["value"]
        return (v["owner"], base64.b64decode(v["data"][0])) if v else None

    def simulate(self, tx) -> dict[str, Any]:
        """Run `tx` on the newest state without checking signatures or its blockhash: nothing is sent."""
        res = self.call("simulateTransaction", [base64.b64encode(bytes(tx)).decode(), {
            "encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True, "commitment": "processed"}])
        return {**res["value"], "slot": res["context"]["slot"]}

    def send(self, tx) -> str:
        """Send a signed transaction to every endpoint at once; its signature, whichever gets it in. Refused when every
        endpoint said no: nothing went out. Any other failure (a timeout, a dropped connection, a 5xx) may hide an
        endpoint that took it: RpcError, and only a lookup of the signature knows."""
        from concurrent.futures import ThreadPoolExecutor
        raw = base64.b64encode(bytes(tx)).decode()
        params = [raw, {"encoding": "base64", "skipPreflight": True, "maxRetries": 0}]
        with ThreadPoolExecutor(max_workers=len(self.send_urls)) as pool:
            errors = [e for e in pool.map(lambda u: self._try(u, params), self.send_urls) if e is not None]
        if len(errors) == len(self.send_urls):
            text = "; ".join(f"{u.split('?')[0]}: {type(e).__name__}: {str(e)[:120]}" for u, e in errors)
            raise (Refused if all(isinstance(e, Refused) for _, e in errors) else RpcError)(text)
        return str(tx.signatures[0])

    def _try(self, url: str, params: list) -> tuple[str, Exception] | None:
        try:
            self.call("sendTransaction", params, url)
            return None
        except Exception as e:  # noqa: BLE001 - one endpoint down is fine while another takes it
            return url, e

    def blockhash(self, max_age_s: float = 20.0):
        """A recent blockhash, read again once older than `max_age_s`. A read refused keeps the last one while it is
        under HASH_KEEP_S old: a blockhash lands for about 150 slots (~60 s), and a sell must not wait on a rate limit."""
        from solders.hash import Hash
        if self._hash is None or time.time() - self._hash[0] > max_age_s:
            try:
                v = self.call("getLatestBlockhash", [{"commitment": "confirmed"}])["value"]
            except Exception:
                if self._hash is None or time.time() - self._hash[0] > HASH_KEEP_S:
                    raise
                return self._hash[1]
            self._hash = (time.time(), Hash.from_string(v["blockhash"]))
        return self._hash[1]

    def balance(self, addr: str) -> int:
        return self.call("getBalance", [addr, {"commitment": "confirmed"}])["value"]
