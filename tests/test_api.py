"""Client tests with a fake HTTP session: pagination in both server orderings, caching, parsing."""
import json

from hl_screener.api import FILLS_PAGE_MAX, HyperliquidAPI


class _Resp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeSession:
    """Serves userFillsByTime from a fixed fill list, either ascending or descending order."""

    def __init__(self, fills, descending: bool):
        self.fills = sorted(fills, key=lambda f: f["time"])
        self.descending = descending
        self.headers = {}
        self.posts = 0

    def post(self, url, data=None, timeout=None):
        self.posts += 1
        body = json.loads(data)
        if body["type"] == "userFillsByTime":
            lo, hi = body["startTime"], body.get("endTime", 1 << 62)
            sel = [f for f in self.fills if lo <= f["time"] <= hi]
            if self.descending:
                sel = sel[::-1]
            return _Resp(sel[:FILLS_PAGE_MAX])
        if body["type"] == "portfolio":
            return _Resp([["day", {"accountValueHistory": [[1, "10"]], "pnlHistory": [[1, "0"]], "vlm": "5"}],
                          ["allTime", {"accountValueHistory": [[1, "10"], [2, "12"]], "pnlHistory": [[1, "0"], [2, "2"]], "vlm": "100"}]])
        if body["type"] == "fundingHistory":
            lo = body["startTime"]
            recs = [{"coin": body["coin"], "fundingRate": "0.0001", "premium": "0", "time": t}
                    for t in range(0, 1300 * 3_600_000, 3_600_000) if t >= lo][:500]
            return _Resp(recs)
        raise AssertionError(body)

    def get(self, url, timeout=None):
        return _Resp({"leaderboardRows": [
            {"ethAddress": "0xABC", "accountValue": "1234.5", "displayName": None, "prize": 0,
             "windowPerformances": [["day", {"pnl": "1", "roi": "0.01", "vlm": "100"}], ["allTime", {"pnl": "50", "roi": "0.5", "vlm": "9000"}]]},
        ]})


def _fills(n):
    return [{"coin": "BTC", "px": "1", "sz": "1", "side": "B", "time": 1_000 + i * 10, "startPosition": "0",
             "dir": "Open Long", "closedPnl": "0", "fee": "0", "tid": i, "hash": f"0x{i}"} for i in range(n)]


def _client(tmp_path, session):
    api = HyperliquidAPI("https://x/info", "https://x/lb", tmp_path / "cache", weight_per_minute=10**9, session=session)
    return api


def test_pagination_ascending_server(tmp_path):
    n = 4500
    s = FakeSession(_fills(n), descending=False)
    api = _client(tmp_path, s)
    fills, truncated = api.user_fills("0xabc", 0, 10**9)
    assert len(fills) == n and not truncated
    assert [f["tid"] for f in fills] == list(range(n))
    assert s.posts <= 6


def test_pagination_descending_server(tmp_path):
    n = 4500
    s = FakeSession(_fills(n), descending=True)
    api = _client(tmp_path, s)
    fills, truncated = api.user_fills("0xabc", 0, 10**9)
    assert len(fills) == n and not truncated
    assert [f["tid"] for f in fills] == list(range(n))
    assert s.posts <= 7


def test_fills_cache_hits_disk(tmp_path):
    s = FakeSession(_fills(10), descending=False)
    api = _client(tmp_path, s)
    a, _ = api.user_fills("0xabc", 0, 10**9)
    n_posts = s.posts
    api2 = _client(tmp_path, s)
    b, _ = api2.user_fills("0xabc", 0, 10**9)
    assert a == b and s.posts == n_posts


def test_truncation_flag(tmp_path):
    s = FakeSession(_fills(10_000), descending=False)
    api = _client(tmp_path, s)
    fills, truncated = api.user_fills("0xabc", 0, 10**9)
    assert len(fills) == 10_000 and truncated


def test_leaderboard_and_portfolio_parsing(tmp_path):
    api = _client(tmp_path, FakeSession([], descending=False))
    rows = api.leaderboard()
    assert rows[0]["address"] == "0xabc" and rows[0]["account_value"] == 1234.5
    assert rows[0]["perf"]["allTime"]["vlm"] == 9000.0
    pf = api.portfolio("0xabc")
    assert pf["allTime"]["account_value"] == [(1, 10.0), (2, 12.0)]
    assert pf["allTime"]["pnl"][-1] == (2, 2.0)


def test_funding_pagination(tmp_path):
    api = _client(tmp_path, FakeSession([], descending=False))
    recs = api.funding_history("BTC", 0, 1300 * 3_600_000)
    assert len(recs) == 1300
    assert recs[0]["time"] == 0 and recs[-1]["time"] == 1299 * 3_600_000
