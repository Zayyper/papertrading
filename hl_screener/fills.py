"""Turn raw Hyperliquid fills into round trips (one position open -> flat).

Position tracking uses the fill's own `startPosition`, `side` and `sz`, so it
is robust to missing history: we never assume we saw the opening fill.
A flip (Long > Short) closes one round trip and opens the next in one fill.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

MS_PER_MIN = 60_000.0


@dataclass
class Fill:
    coin: str
    px: float
    sz: float           # unsigned
    side: str           # "B" buy / "A" sell
    time: int           # ms
    start_pos: float    # signed position before this fill
    closed_pnl: float
    fee: float
    dir: str
    liquidation: bool
    tid: Any

    @property
    def signed_sz(self) -> float:
        return self.sz if self.side == "B" else -self.sz

    @property
    def end_pos(self) -> float:
        return self.start_pos + self.signed_sz


@dataclass
class RoundTrip:
    coin: str
    direction: int              # +1 long, -1 short
    open_time: int
    close_time: int
    entry_vwap: float
    exit_vwap: float
    max_abs_size: float
    max_notional: float         # max |size| * entry vwap (USD)
    gross_pnl: float            # sum of closedPnl
    fees: float
    liquidated: bool
    n_fills: int
    open_qty: float = 0.0       # total qty opened
    close_qty: float = 0.0      # total qty closed
    complete: bool = True       # False when we did not see the opening fill

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.fees

    @property
    def hold_minutes(self) -> float:
        return (self.close_time - self.open_time) / MS_PER_MIN

    @property
    def price_return(self) -> float:
        """Signed price move captured, as a fraction of entry price."""
        if self.entry_vwap <= 0:
            return 0.0
        return self.direction * (self.exit_vwap - self.entry_vwap) / self.entry_vwap


def parse_fills(raw: Iterable[dict[str, Any]], perps_only: bool = True, address: str | None = None) -> list[Fill]:
    """Normalise raw fills. Pass the trader's `address` so that a fill is only marked as a
    liquidation when *this* trader was liquidated: Hyperliquid attaches the `liquidation`
    record to both sides of the trade, and the counterparty (whose resting order absorbed
    the liquidation) must not be penalised for it."""
    out: list[Fill] = []
    me = address.lower() if address else None
    for f in raw:
        coin = str(f.get("coin", ""))
        if perps_only and (coin.startswith("@") or "/" in coin):
            continue  # spot markets
        d = str(f.get("dir", ""))
        liq = _is_own_liquidation(f.get("liquidation"), me) or "liquidat" in d.lower()
        out.append(Fill(
            coin=coin,
            px=float(f["px"]),
            sz=abs(float(f["sz"])),
            side=str(f.get("side", "B")),
            time=int(f["time"]),
            start_pos=float(f.get("startPosition", 0.0)),
            closed_pnl=float(f.get("closedPnl", 0.0)),
            fee=float(f.get("fee", 0.0)) + float(f.get("builderFee", 0.0) or 0.0),
            dir=d,
            liquidation=liq,
            tid=f.get("tid", f.get("hash")),
        ))
    out.sort(key=lambda x: (x.time, str(x.tid)))
    return out


def _is_own_liquidation(liq: Any, me: str | None) -> bool:
    """True when the fill's liquidation record refers to this trader.

    Without an address (or a `liquidatedUser` field) any liquidation record counts, which is
    the conservative reading; with both present only a match counts."""
    if not liq:
        return False
    victim = str(liq.get("liquidatedUser") or "").lower() if isinstance(liq, dict) else ""
    if me is None or not victim:
        return True
    return victim == me


@dataclass
class _Open:
    coin: str
    direction: int
    open_time: int
    entry_cost: float = 0.0     # sum px*qty opened
    open_qty: float = 0.0
    exit_cost: float = 0.0
    close_qty: float = 0.0
    max_abs: float = 0.0
    gross: float = 0.0
    fees: float = 0.0
    liquidated: bool = False
    n_fills: int = 0
    complete: bool = True
    last_time: int = 0

    def to_round_trip(self, close_time: int) -> RoundTrip:
        entry = self.entry_cost / self.open_qty if self.open_qty > 0 else 0.0
        exit_ = self.exit_cost / self.close_qty if self.close_qty > 0 else entry
        return RoundTrip(
            coin=self.coin, direction=self.direction, open_time=self.open_time,
            close_time=close_time, entry_vwap=entry, exit_vwap=exit_,
            max_abs_size=self.max_abs, max_notional=self.max_abs * entry,
            gross_pnl=self.gross, fees=self.fees, liquidated=self.liquidated,
            n_fills=self.n_fills, open_qty=self.open_qty, close_qty=self.close_qty,
            complete=self.complete,
        )


def build_round_trips(fills: list[Fill], eps: float = 1e-9) -> tuple[list[RoundTrip], dict[str, _Open]]:
    """Group fills per coin into round trips. Returns (closed round trips, still-open)."""
    closed: list[RoundTrip] = []
    open_by_coin: dict[str, _Open] = {}

    for f in fills:
        start = f.start_pos
        end = f.end_pos
        qty = f.sz
        px = f.px
        cur = open_by_coin.get(f.coin)

        # If we have no open record but the fill starts from a non-flat position,
        # we missed the opening: create an incomplete record.
        if cur is None and abs(start) > eps:
            cur = _Open(coin=f.coin, direction=1 if start > 0 else -1, open_time=f.time, complete=False)
            cur.max_abs = abs(start)
            open_by_coin[f.coin] = cur

        same_sign = (start > eps and end > eps) or (start < -eps and end < -eps)
        flat_to_open = abs(start) <= eps and abs(end) > eps
        flips = (start > eps and end < -eps) or (start < -eps and end > eps)
        goes_flat = abs(start) > eps and abs(end) <= eps

        if flat_to_open:
            cur = _Open(coin=f.coin, direction=1 if end > 0 else -1, open_time=f.time)
            open_by_coin[f.coin] = cur
            _add_open(cur, qty, px, f)
        elif same_sign:
            assert cur is not None
            increasing = abs(end) > abs(start)
            if increasing:
                _add_open(cur, qty, px, f)
            else:
                _add_close(cur, qty, px, f)
        elif goes_flat:
            assert cur is not None
            _add_close(cur, qty, px, f)
            closed.append(cur.to_round_trip(f.time))
            del open_by_coin[f.coin]
        elif flips:
            assert cur is not None
            close_qty = abs(start)
            open_qty = abs(end)
            _add_close(cur, close_qty, px, f)
            closed.append(cur.to_round_trip(f.time))
            new = _Open(coin=f.coin, direction=1 if end > 0 else -1, open_time=f.time)
            open_by_coin[f.coin] = new
            # the closing part already carried the fee/pnl; opening part carries nothing extra
            new.entry_cost += px * open_qty
            new.open_qty += open_qty
            new.max_abs = max(new.max_abs, open_qty)
            new.n_fills += 1
            new.last_time = f.time
        else:
            # zero-size fill or numerical noise; count it on the open record if any
            if cur is not None:
                cur.n_fills += 1
                cur.fees += f.fee
                cur.gross += f.closed_pnl

    return closed, open_by_coin


def _add_open(cur: _Open, qty: float, px: float, f: Fill) -> None:
    cur.entry_cost += px * qty
    cur.open_qty += qty
    cur.max_abs = max(cur.max_abs, abs(f.end_pos))
    cur.n_fills += 1
    cur.fees += f.fee
    cur.gross += f.closed_pnl
    cur.liquidated = cur.liquidated or f.liquidation
    cur.last_time = f.time


def _add_close(cur: _Open, qty: float, px: float, f: Fill) -> None:
    cur.exit_cost += px * qty
    cur.close_qty += qty
    cur.max_abs = max(cur.max_abs, abs(f.start_pos))
    cur.n_fills += 1
    cur.fees += f.fee
    cur.gross += f.closed_pnl
    cur.liquidated = cur.liquidated or f.liquidation
    cur.last_time = f.time
