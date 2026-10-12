"""The Live tab's server side: what it reads of the live copies, and STOP / RESUME as a file the collector checks."""
import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from hl_screener.webui.server import App, Handler


def serve(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "data" / "pump" / "pump.db"
    db.parent.mkdir(parents=True)
    c = sqlite3.connect(db)
    c.executescript("""CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
                       CREATE TABLE lbal (ts INTEGER, sol REAL);
                       CREATE TABLE lpos (mint TEXT PRIMARY KEY, wallet TEXT, tok INTEGER, cost REAL, opened INTEGER, stuck INTEGER DEFAULT 0);
                       CREATE TABLE lorders (id INTEGER PRIMARY KEY, mode TEXT, wallet TEXT, mint TEXT, side TEXT, venue TEXT,
                           trigger_slot INTEGER, seen REAL, ready REAL, done REAL, slot INTEGER, sig TEXT, status TEXT, err TEXT,
                           sol REAL, tok REAL, want REAL, units INTEGER, pnl REAL, tries INTEGER DEFAULT 1);""")
    now = int(time.time())
    c.executemany("INSERT INTO lbal VALUES (?, ?)", [(now - 60, 0.72), (now - 30, 0.57), (now, 0.73)])
    c.execute("INSERT INTO meta VALUES ('live_cfg', ?)", (json.dumps({"mode": "live", "me": "ME", "wallets": ["G"], "exit": "hold2m"}),))
    c.execute("INSERT INTO lorders(mode, side, mint, status, sol, pnl, done) VALUES ('live', 'sell', 'M', 'filled', 0.16, 0.01, ?)", (now,))
    c.execute("INSERT INTO lorders(mode, side, mint, status) VALUES ('dry', 'buy', 'D', 'sim_ok')")   # not live: not shown
    c.commit()
    c.close()
    Handler.app, Handler.password = App(tmp_path, None), None
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def call(url, method="GET", ctype="application/json"):
    req = urllib.request.Request(url, data=b"{}" if method == "POST" else None, method=method, headers={"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_the_live_tab_reads_the_wallet_and_stop_resume_write_only_the_flag(tmp_path, monkeypatch):
    httpd, base = serve(tmp_path, monkeypatch)
    try:
        code, d = call(base + "/api/live?hours=1")
        assert code == 200 and d["cfg"]["exit"] == "hold2m" and [b[1] for b in d["balance"]] == [0.72, 0.57, 0.73]
        assert [o["mint"] for o in d["orders"]] == ["M"] and d["stopped"] is False
        assert call(base + "/api/live/stop", "POST", "text/plain")[0] == 415   # a form elsewhere cannot press it
        assert call(base + "/api/live/stop", "POST") == (200, {"ok": True, "stopped": True})
        flag = tmp_path / "data" / "pump" / "live_stop"
        assert flag.exists() and call(base + "/api/live")[1]["stopped"] is True
        assert call(base + "/api/live/resume", "POST") == (200, {"ok": True, "stopped": False}) and not flag.exists()
    finally:
        httpd.shutdown()


def test_the_pump_log_is_served_as_text_its_tail_filtered_and_its_keys_blanked(tmp_path, monkeypatch):
    httpd, base = serve(tmp_path, monkeypatch)
    try:
        get = lambda q: urllib.request.urlopen(base + "/api/pump/log" + q, timeout=5).read().decode()   # noqa: E731
        assert get("").startswith("no pump.log yet")
        lines = [f"2026-10-12 00:00:{i:02d},000 INFO hl_screener.pumpfun: report line {i}" for i in range(10)]
        lines += ["2026-10-12 00:01:00,000 INFO hl_screener.pumpexits: exit sweep 3gHrfi (75 copies): hold2m +16.0%",
                  "2026-10-12 00:01:01,000 WARNING hl_screener.pumpfun: feed error on wss://rpc.example/?api-key=SECRET123&x=1"]
        (tmp_path / "data" / "pump" / "pump.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
        assert get("?grep=SWEEP").splitlines() == [lines[10]]           # case-insensitive filter
        tail = get("?lines=2").splitlines()
        assert tail[0] == lines[10] and "SECRET123" not in tail[1] and "api-key=***" in tail[1]
        assert len(get("?lines=bad").splitlines()) == 12                # a bad count falls back to 300
    finally:
        httpd.shutdown()
