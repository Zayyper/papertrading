"""Thin Hyperliquid info-API client with disk cache and weight-based rate limiting.

Everything here is public and unauthenticated. Weights follow the HL docs:
1200/min/IP aggregate; most info requests weigh 20; userFillsByTime adds
1 per 20 fills returned; candleSnapshot adds 1 per 60 candles.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Protocol

import requests

log = logging.getLogger(__name__)

FILLS_PAGE_MAX = 2000
FILLS_HISTORY_CAP = 10_000
CANDLES_MAX = 5000


class InfoAPI(Protocol):
    """What the pipeline needs from a data source (real or fake)."""

    def leaderboard(self) -> list[dict[str, Any]]: ...
    def portfolio(self, user: str) -> dict[str, Any]: ...
    def user_fills(self, user: str, start_ms: int, end_ms: int) -> tuple[list[dict[str, Any]], bool]: ...
    def fills_cached(self, user: str, start_ms: int, end_ms: int) -> bool: ...
    def meta_and_ctxs(self) -> tuple[dict[str, Any], list[dict[str, Any]]]: ...
    def candles(self, coin: str, interval: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]: ...
    def funding_history(self, coin: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]: ...


class _WeightLimiter:
    def __init__(self, weight_per_minute: int):
        self.cap = weight_per_minute
        self.events: deque[tuple[float, int]] = deque()
        self.lock = threading.Lock()                  # the copy-test collector calls from several threads at once

    def add(self, weight: int) -> None:
        """Charge weight after the fact (e.g. per-item surcharges known only from the response)."""
        if weight > 0:
            with self.lock:
                self.events.append((time.monotonic(), weight))

    def acquire(self, weight: int) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                while self.events and now - self.events[0][0] > 60.0:
                    self.events.popleft()
                used = sum(w for _, w in self.events)
                if used + weight <= self.cap:
                    self.events.append((now, weight))
                    return
                sleep_for = 60.0 - (now - self.events[0][0]) + 0.05
            log.debug("rate limit: sleeping %.1fs", sleep_for)
            time.sleep(max(sleep_for, 0.1))


class DiskCache:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        h = hashlib.sha1(key.encode()).hexdigest()
        return self.root / h[:2] / f"{h}.json"

    def has(self, key: str) -> bool:
        return self._path(key).exists()

    def get(self, key: str) -> Any | None:
        p = self._path(key)
        if p.exists():
            with open(p) as fh:
                return json.load(fh)
        return None

    def put(self, key: str, value: Any) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        with open(tmp, "w") as fh:
            json.dump(value, fh)
        tmp.replace(p)


class HyperliquidAPI:
    def __init__(
        self,
        api_url: str,
        leaderboard_url: str,
        cache_dir: str | Path,
        weight_per_minute: int = 1000,
        timeout_s: float = 30.0,
        session: requests.Session | None = None,
    ):
        self.api_url = api_url
        self.leaderboard_url = leaderboard_url
        self.cache = DiskCache(Path(cache_dir))
        self.limiter = _WeightLimiter(weight_per_minute)
        self.timeout = timeout_s
        self.s = session or requests.Session()
        self.s.headers.update({"Content-Type": "application/json", "User-Agent": "hl-niche-screener/0.1"})

    # ---- low level -------------------------------------------------------
    def _post(self, body: dict[str, Any], weight: int) -> Any:
        self.limiter.acquire(weight)
        for attempt in range(6):
            try:
                r = self.s.post(self.api_url, data=json.dumps(body), timeout=self.timeout)
            except requests.RequestException as e:
                wait = 2 ** attempt + random.random()
                log.warning("network error %s; retry in %.1fs", e, wait)
                time.sleep(wait)
                continue
            if r.status_code == 429:
                wait = 10 * (attempt + 1)
                log.warning("429 rate limited; sleeping %ss", wait)
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                wait = 2 ** attempt + random.random()
                log.warning("server %s; retry in %.1fs", r.status_code, wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"giving up on request {body.get('type')}")

    def _cached(self, key: str, fetch, ttl_s: float | None = None) -> Any:
        """Cache forever unless ttl_s is given (stored alongside a timestamp)."""
        hit = self.cache.get(key)
        if hit is not None:
            if ttl_s is None or (time.time() - hit.get("_ts", 0)) < ttl_s:
                return hit["v"]
        v = fetch()
        self.cache.put(key, {"_ts": time.time(), "v": v})
        return v

    # ---- endpoints ---------------------------------------------------------
    def leaderboard(self) -> list[dict[str, Any]]:
        def fetch():
            self.limiter.acquire(20)
            r = self.s.get(self.leaderboard_url, timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        raw = self._cached("leaderboard", fetch, ttl_s=6 * 3600)
        rows = raw.get("leaderboardRows", raw) if isinstance(raw, dict) else raw
        out = []
        for row in rows:
            perf = {}
            for item in row.get("windowPerformances", []):
                if isinstance(item, (list, tuple)) and len(item) == 2:
                    name, d = item
                    perf[name] = {k: _f(d.get(k)) for k in ("pnl", "roi", "vlm")}
            out.append({
                "address": row.get("ethAddress", "").lower(),
                "account_value": _f(row.get("accountValue")),
                "display_name": row.get("displayName"),
                "perf": perf,
            })
        return out

    def portfolio(self, user: str) -> dict[str, Any]:
        body = {"type": "portfolio", "user": user}
        raw = self._cached(f"portfolio:{user}", lambda: self._post(body, 20), ttl_s=24 * 3600)
        out: dict[str, Any] = {}
        for item in raw:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                name, d = item
                out[name] = {
                    "account_value": [(int(t), _f(v)) for t, v in d.get("accountValueHistory", [])],
                    "pnl": [(int(t), _f(v)) for t, v in d.get("pnlHistory", [])],
                    "vlm": _f(d.get("vlm")),
                }
        return out

    def fills_cached(self, user: str, start_ms: int, end_ms: int) -> bool:
        """True when this user's fills for exactly this window are already on disk (no request needed)."""
        return self.cache.has(f"fills:{user}:{start_ms}:{end_ms}")

    def user_fills(self, user: str, start_ms: int, end_ms: int) -> tuple[list[dict[str, Any]], bool]:
        """All fills in [start_ms, end_ms]. Returns (fills sorted ascending, truncated).

        `truncated` is True when we hit the 10k-most-recent cap, i.e. the
        account has older fills we can never see; the caller should shrink
        its window to the earliest fill returned.
        """
        key = f"fills:{user}:{start_ms}:{end_ms}"
        hit = self.cache.get(key)
        if hit is not None:
            return hit["v"]["fills"], hit["v"]["truncated"]

        seen: dict[int, dict[str, Any]] = {}
        lo, hi = start_ms, end_ms
        descending: bool | None = None
        pages = 0
        while True:
            body = {"type": "userFillsByTime", "user": user, "startTime": lo, "endTime": hi}
            page = self._post(body, 20)
            self.limiter.add(len(page) // 20)
            pages += 1
            new = 0
            for f in page:
                tid = f.get("tid", f.get("hash"))
                if tid not in seen:
                    seen[tid] = f
                    new += 1
            if len(page) < FILLS_PAGE_MAX:
                break
            times = [int(f["time"]) for f in page]
            tmin, tmax = min(times), max(times)
            if tmin == tmax:
                break  # a full page inside one millisecond: cannot paginate further
            if descending is None:
                # Probe: assume ascending (docs); if the next page yields nothing new, flip.
                probe_lo, probe_hi = tmax, hi
                body = {"type": "userFillsByTime", "user": user, "startTime": probe_lo, "endTime": probe_hi}
                probe = self._post(body, 20)
                self.limiter.add(len(probe) // 20)
                pages += 1
                pn = 0
                for f in probe:
                    tid = f.get("tid", f.get("hash"))
                    if tid not in seen:
                        seen[tid] = f
                        pn += 1
                if pn > 0:
                    descending = False
                    if len(probe) < FILLS_PAGE_MAX:
                        break
                    lo = max(int(f["time"]) for f in probe)
                    continue
                descending = True
            if descending:
                hi = tmin
            else:
                lo = tmax
            if new == 0 and pages > 2:
                break  # nothing more to learn
            if len(seen) >= FILLS_HISTORY_CAP + 500:
                break
        fills = sorted(seen.values(), key=lambda f: (int(f["time"]), f.get("tid", 0)))
        truncated = len(fills) >= FILLS_HISTORY_CAP - 50
        self.cache.put(key, {"_ts": time.time(), "v": {"fills": fills, "truncated": truncated}})
        return fills, truncated

    def meta_and_ctxs(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        raw = self._cached("metaAndAssetCtxs", lambda: self._post({"type": "metaAndAssetCtxs"}, 20), ttl_s=6 * 3600)
        meta, ctxs = raw[0], raw[1]
        return meta, ctxs

    def candles(self, coin: str, interval: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        body = {"type": "candleSnapshot", "req": {"coin": coin, "interval": interval, "startTime": start_ms, "endTime": end_ms}}
        key = f"candles:{coin}:{interval}:{start_ms}:{end_ms}"
        def fetch():
            page = self._post(body, 20)
            self.limiter.add(len(page) // 60)
            return page
        return self._cached(key, fetch)

    # ---- live, uncached (used by the paper trader) ---------------------------
    def l2_book(self, coin: str) -> dict[str, Any]:
        """Current order book: {"levels": [bids, asks]} with px/sz/n per level."""
        return self._post({"type": "l2Book", "coin": coin}, 2)

    def all_mids(self) -> dict[str, float]:
        raw = self._post({"type": "allMids"}, 2)
        return {k: _f(v) for k, v in raw.items()} if isinstance(raw, dict) else {}

    def clearinghouse_state(self, user: str) -> dict[str, Any]:
        """Live account state: marginSummary.accountValue and open positions."""
        return self._post({"type": "clearinghouseState", "user": user}, 2)

    def live_funding_rates(self) -> dict[str, float]:
        """Current hourly funding rate per perp (positive = longs pay)."""
        raw = self._post({"type": "metaAndAssetCtxs"}, 20)
        meta, ctxs = raw[0], raw[1]
        out: dict[str, float] = {}
        for asset, ctx in zip(meta.get("universe", []), ctxs):
            out[asset["name"]] = _f(ctx.get("funding"))
        return out

    def fills_since(self, user: str, start_ms: int) -> list[dict[str, Any]]:
        """Fills from start_ms to now, one page, uncached (reconciliation after a WebSocket gap)."""
        page = self._post({"type": "userFillsByTime", "user": user, "startTime": start_ms}, 20)
        self.limiter.add(len(page) // 20)
        return sorted(page, key=lambda f: (int(f["time"]), f.get("tid", 0)))

    def funding_since(self, coin: str, start_ms: int) -> list[dict[str, Any]]:
        """Hourly funding from start_ms to now, one page (500 hours), uncached (the copy-test collector's hourly top-up)."""
        return self._post({"type": "fundingHistory", "coin": coin, "startTime": start_ms}, 20)

    def funding_history(self, coin: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        """Hourly funding records; paginated (500 per response)."""
        key = f"funding:{coin}:{start_ms}:{end_ms}"
        hit = self.cache.get(key)
        if hit is not None:
            return hit["v"]
        out: list[dict[str, Any]] = []
        lo = start_ms
        while lo < end_ms:
            page = self._post({"type": "fundingHistory", "coin": coin, "startTime": lo, "endTime": end_ms}, 20)
            if not page:
                break
            out.extend(page)
            last = max(int(p["time"]) for p in page)
            if len(page) < 500 or last <= lo:
                break
            lo = last + 1
        seen = {}
        for p in out:
            seen[int(p["time"])] = p
        res = [seen[t] for t in sorted(seen)]
        self.cache.put(key, {"_ts": time.time(), "v": res})
        return res


def _f(x: Any) -> float:
    if x is None:
        return float("nan")
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")
