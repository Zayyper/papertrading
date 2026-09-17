"""Local web UI: run the screener, watch progress, browse results, edit config.

Standard library only, single process, bound to localhost. The UI never talks to
Hyperliquid itself: every action runs the same CLI you would type
(`python -m hl_screener run|demo|pool|inspect`) as a subprocess from the project
folder, streams its output, and reads what it writes to `out/`.

    python -m hl_screener ui            # http://127.0.0.1:8765
    python -m hl_screener ui --port 9000 --no-browser
"""
from __future__ import annotations

import csv
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ..config import Config

STATIC_DIR = Path(__file__).resolve().parent / "static"
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RUN_FILE_RE = re.compile(r"^run_(\d{4}-\d{2}-\d{2})\.json$")
ALLOWED_CMDS = ("run", "demo", "pool", "inspect")
FILE_KINDS = {
    "report": "report_{d}.md",
    "traders": "traders_{d}.csv",
    "shortlist": "shortlist_{d}.csv",
    "shortlist_trades": "shortlist_trades_{d}.csv",
    "run_json": "run_{d}.json",
}
TRADE_COLUMNS = ("address", "window", "coin", "open_time", "close_time", "direction", "follower_notional",
                 "penalty_bps_entry", "gross_pnl", "fees", "funding", "net_pnl", "leader_net_pnl")
MIME = {
    ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8", ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
    ".md": "text/markdown; charset=utf-8", ".csv": "text/csv; charset=utf-8", ".txt": "text/plain; charset=utf-8",
}


# ---------------------------------------------------------------------------
# jobs: one CLI subprocess at a time, output kept in memory
# ---------------------------------------------------------------------------
class Job:
    _seq = 0

    def __init__(self, cmd: str, argv: list[str], cwd: str, env: dict[str, str]):
        Job._seq += 1
        self.id = Job._seq
        self.cmd = cmd
        self.argv = argv
        self.status = "running"          # running | done | failed | stopped
        self.returncode: int | None = None
        self.started = time.time()
        self.ended: float | None = None
        self.lines: list[str] = []
        self.lock = threading.Lock()
        self.proc = subprocess.Popen(
            argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        threading.Thread(target=self._pump, name=f"job-{self.id}", daemon=True).start()

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            with self.lock:
                self.lines.append(line.rstrip("\r\n"))
        rc = self.proc.wait()
        with self.lock:
            self.returncode = rc
            self.ended = time.time()
            if self.status == "running":
                self.status = "done" if rc == 0 else "failed"

    def stop(self) -> None:
        if self.proc.poll() is None:
            with self.lock:
                self.status = "stopped"
            self.proc.terminate()

    def view(self, since: int = 0, with_lines: bool = True) -> dict[str, Any]:
        with self.lock:
            total = len(self.lines)
            lines = self.lines[since:] if with_lines else []
            return {
                "id": self.id, "cmd": self.cmd, "argv": self.argv[4:], "status": self.status,
                "returncode": self.returncode, "started": self.started, "ended": self.ended,
                "lines": lines, "next": total, "total": total,
            }


class App:
    def __init__(self, project_root: Path, config_path: str | None):
        self.root = project_root
        self.config_path = config_path
        self.job: Job | None = None
        self.service: Job | None = None      # the long-running paper trader, independent of `job`
        self.lock = threading.Lock()
        self._mids: tuple[float, dict[str, float]] = (0.0, {})

    # ---- paper trader -------------------------------------------------------
    def paper_db(self) -> Path:
        cfg, _ = self.cfg()
        return self.root / cfg.data_dir / "paper" / "paper.db"

    def _live_mids(self) -> dict[str, float]:
        """Mid prices for marking open paper positions; one request per 15 s at most."""
        from ..api import HyperliquidAPI
        t, mids = self._mids
        if time.time() - t < 15:
            return mids
        cfg, _ = self.cfg()
        try:
            api = HyperliquidAPI(cfg.api_url, cfg.leaderboard_url, self.root / cfg.data_dir / "cache", cfg.request_weight_per_minute, cfg.http_timeout_s)
            mids = api.all_mids()
        except Exception:  # noqa: BLE001 - offline: mark at entry price
            mids = mids or {}
        self._mids = (time.time(), mids)
        return mids

    def paper_view(self, log_since: int = 0) -> dict[str, Any]:
        from ..paper import status
        cfg, _ = self.cfg()
        out_dir = self.root / cfg.out_dir
        files = sorted((p for p in out_dir.glob("shortlist_*.csv") if not p.name.startswith("shortlist_trades")),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        db = self.paper_db()
        st = status(db, self._live_mids() if db.exists() else None)
        return {"service": self.service.view(log_since) if self.service else None, "db": str(db), "status": st,
                "leaders_files": [str(p.relative_to(self.root)) for p in files],
                "defaults": {"equity": cfg.follower_equity_usd, "max_leverage": cfg.follower_max_leverage}}

    def start_service(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        leaders = (body.get("leaders") or "").strip()
        if not leaders:
            return 400, {"error": "choose a leaders file (a shortlist csv)"}
        path = (self.root / leaders).resolve()
        if self.root.resolve() not in path.parents or not path.is_file():
            return 400, {"error": "leaders file must be inside the project folder"}
        args = ["paper", "--leaders", str(path.relative_to(self.root.resolve()))]
        eq = body.get("equity")
        if eq not in (None, ""):
            try:
                eq_f = float(eq)
            except (TypeError, ValueError):
                return 400, {"error": "equity must be a number"}
            if not 10 <= eq_f <= 1e7:
                return 400, {"error": "equity must be between 10 and 10,000,000"}
            args += ["--equity", str(eq_f)]
        argv = [sys.executable, "-u", "-m", "hl_screener"]
        if self.config_path:
            argv += ["--config", self.config_path]
        argv += args
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        with self.lock:
            if self.service is not None and self.service.status == "running":
                return 409, {"error": "the paper trader is already running"}
            try:
                self.service = Job("paper", argv, str(self.root), env)
            except OSError as e:
                return 500, {"error": f"could not start: {e}"}
        return 201, {"service": self.service.view()}

    # ---- config -------------------------------------------------------------
    def cfg(self) -> tuple[Config, str | None]:
        try:
            return Config.load(self.config_path), None
        except Exception as e:  # noqa: BLE001
            return Config(), f"{type(e).__name__}: {e}"

    def config_view(self) -> dict[str, Any]:
        text = ""
        if self.config_path and Path(self.config_path).exists():
            text = Path(self.config_path).read_text(encoding="utf-8")
        cfg, err = self.cfg()
        return {"path": self.config_path, "text": text, "values": cfg.to_dict(), "error": err,
                "min_hold_minutes": cfg.min_hold_minutes}

    def save_config(self, text: str) -> tuple[int, dict[str, Any]]:
        if not self.config_path:
            return 400, {"error": "no config file: start the UI from the project folder (where config.toml lives) or pass --config"}
        p = Path(self.config_path)
        tmp = p.with_suffix(".toml.tmp")
        tmp.write_text(text, encoding="utf-8")
        try:
            Config.load(tmp)
        except Exception as e:  # noqa: BLE001
            tmp.unlink(missing_ok=True)
            return 400, {"error": f"{type(e).__name__}: {e}"}
        tmp.replace(p)
        return 200, {"ok": True, **self.config_view()}

    def out_dir(self) -> Path:
        cfg, _ = self.cfg()
        return self.root / cfg.out_dir

    # ---- jobs ---------------------------------------------------------------
    def start_job(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        cmd = body.get("cmd")
        if cmd not in ALLOWED_CMDS:
            return 400, {"error": f"cmd must be one of {ALLOWED_CMDS}"}
        args: list[str] = []
        end = (body.get("end") or "").strip()
        if cmd in ("run", "inspect") and end:
            if not DATE_RE.match(end):
                return 400, {"error": "end must be YYYY-MM-DD"}
            args += ["--end", end]
        if cmd == "run":
            pool = body.get("pool")
            if pool not in (None, ""):
                try:
                    pool_n = int(pool)
                except (TypeError, ValueError):
                    return 400, {"error": "pool must be an integer"}
                if not 1 <= pool_n <= 10_000:
                    return 400, {"error": "pool must be between 1 and 10000"}
                args += ["--pool", str(pool_n)]
            if body.get("no_split"):
                args.append("--no-split")
            if body.get("cached_only"):
                args.append("--cached-only")
        if cmd == "inspect":
            addr = (body.get("address") or "").strip()
            if not ADDRESS_RE.match(addr):
                return 400, {"error": "address must be 0x followed by 40 hex characters"}
            args = [addr, *args]
        argv = [sys.executable, "-u", "-m", "hl_screener"]
        if self.config_path:
            argv += ["--config", self.config_path]
        if body.get("verbose"):
            argv.append("-v")
        argv += [cmd, *args]
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        with self.lock:
            if self.job is not None and self.job.status == "running":
                return 409, {"error": "a job is already running; stop it first", "job": self.job.view(with_lines=False)}
            try:
                self.job = Job(cmd, argv, str(self.root), env)
            except OSError as e:
                return 500, {"error": f"could not start {sys.executable}: {e}"}
            return 201, {"job": self.job.view()}

    # ---- runs (what the CLI wrote to out/) ---------------------------------
    def list_runs(self) -> list[dict[str, Any]]:
        out = self.out_dir()
        runs: list[dict[str, Any]] = []
        for kind, d in (("real", out), ("demo", out / "demo")):
            if not d.is_dir():
                continue
            for p in d.iterdir():
                m = RUN_FILE_RE.match(p.name)
                if not m:
                    continue
                date = m.group(1)
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    data = {}
                bench = data.get("benchmarks") or {}
                v = bench.get("verdict") or {}
                runs.append({
                    "id": f"{kind}/{date}", "kind": kind, "date": date, "dir": str(d),
                    "proceed": bool(v.get("proceed_to_paper_test")), "checks": v.get("checks", {}),
                    "shortlist_n": len(data.get("shortlist") or []),
                    "pool_size": data.get("pool_size"), "loaded": data.get("loaded"),
                    "is_start": data.get("is_start"), "is_end": data.get("is_end"),
                    "shortlist_roi": (bench.get("shortlist") or {}).get("roi"),
                    "mtime": p.stat().st_mtime,
                })
        runs.sort(key=lambda r: r["mtime"], reverse=True)
        return runs

    def run_dir(self, kind: str, date: str) -> Path:
        out = self.out_dir()
        return out / "demo" if kind == "demo" else out

    def run_detail(self, kind: str, date: str) -> dict[str, Any] | None:
        d = self.run_dir(kind, date)
        rj = d / f"run_{date}.json"
        if not rj.exists():
            return None
        data = json.loads(rj.read_text(encoding="utf-8"))
        report = d / f"report_{date}.md"
        return {
            "id": f"{kind}/{date}", "kind": kind, "date": date, "dir": str(d), "run": data,
            "report_md": report.read_text(encoding="utf-8") if report.exists() else "",
            "shortlist": read_csv(d / f"shortlist_{date}.csv"),
            "traders": read_csv(d / f"traders_{date}.csv"),
            "trades": read_csv(d / f"shortlist_trades_{date}.csv", columns=TRADE_COLUMNS),
            "files": {k: (d / v.format(d=date)).exists() for k, v in FILE_KINDS.items()},
        }

    def run_file(self, kind: str, date: str, which: str) -> Path | None:
        if which not in FILE_KINDS:
            return None
        p = self.run_dir(kind, date) / FILE_KINDS[which].format(d=date)
        return p if p.exists() else None

    def status(self) -> dict[str, Any]:
        cfg, err = self.cfg()
        return {
            "project_root": str(self.root), "python": sys.executable, "config_path": self.config_path,
            "config_error": err, "out_dir": str(self.out_dir()), "data_dir": str(self.root / cfg.data_dir),
            "job": self.job.view(with_lines=False) if self.job else None,
            "runs": self.list_runs(),
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def read_csv(path: Path, columns: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with open(path, newline="", encoding="utf-8") as fh:
        for rec in csv.DictReader(fh):
            if columns:
                rec = {k: rec[k] for k in columns if k in rec}
            rows.append({k: _coerce(v) for k, v in rec.items()})
    return rows


_INT_RE = re.compile(r"^-?\d{1,18}$")


def _coerce(s: str | None) -> Any:
    if s is None:
        return None
    t = s.strip()
    if t == "":
        return None
    if t in ("True", "False"):
        return t == "True"
    if _INT_RE.match(t):
        return int(t)
    try:
        f = float(t)
    except ValueError:
        return s
    if math.isnan(f):
        return None
    if math.isinf(f):
        return "inf" if f > 0 else "-inf"
    return f


def _clean(o: Any) -> Any:
    """Make an object JSON-safe: NaN -> null, inf -> "inf" (browsers reject NaN literals)."""
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, float):
        if math.isnan(o):
            return None
        if math.isinf(o):
            return "inf" if o > 0 else "-inf"
    return o


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    app: App
    password: str | None = None          # HL_UI_PASSWORD: HTTP Basic auth on every request when set
    server_version = "hl-screener-ui/0.1"
    protocol_version = "HTTP/1.1"

    def parse_request(self) -> bool:  # runs before any do_*; a wrong or missing password ends the request here
        if not super().parse_request():
            return False
        if self.password is None or self._authorized():
            return True
        body = b"password required"
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="hl-niche-screener", charset="UTF-8"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True
        return False

    def _authorized(self) -> bool:
        import base64
        import hmac
        auth = self.headers.get("Authorization", "")
        if not auth.lower().startswith("basic "):
            return False
        try:
            raw = base64.b64decode(auth[6:].strip()).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        _, _, given = raw.partition(":")
        return hmac.compare_digest(given.encode(), (self.password or "").encode())

    def log_request(self, code: Any = "-", size: Any = "-") -> None:  # quiet: only server errors reach the terminal
        if str(code).startswith("5"):
            super().log_request(code, size)

    # ---- plumbing ----------------------------------------------------------
    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj: Any, status: int = 200) -> None:
        self._send(status, json.dumps(_clean(obj)).encode("utf-8"), MIME[".json"])

    def _error(self, status: int, msg: str) -> None:
        self._json({"error": msg}, status)

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n > 0 else b""

    def _file(self, path: Path, download_name: str | None = None) -> None:
        try:
            data = path.read_bytes()
        except OSError:
            return self._error(404, "not found")
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(path.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if download_name:
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _static(self, rel: str) -> None:
        target = (STATIC_DIR / rel).resolve()
        if STATIC_DIR.resolve() not in target.parents or not target.is_file():
            return self._error(404, "not found")
        self._file(target)

    # ---- routes ------------------------------------------------------------
    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        u = urlsplit(self.path)
        path, q = u.path, parse_qs(u.query)
        app = self.app
        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        if path == "/api/status":
            return self._json(app.status())
        if path == "/api/config":
            return self._json(app.config_view())
        if path == "/api/jobs/current":
            since = int(q.get("since", ["0"])[0] or 0)
            return self._json({"job": app.job.view(since) if app.job else None})
        if path == "/api/runs":
            return self._json({"runs": app.list_runs()})
        m = re.match(r"^/api/runs/(real|demo)/(\d{4}-\d{2}-\d{2})$", path)
        if m:
            d = app.run_detail(m.group(1), m.group(2))
            return self._json(d) if d else self._error(404, "run not found")
        m = re.match(r"^/api/runs/(real|demo)/(\d{4}-\d{2}-\d{2})/files/(\w+)$", path)
        if m:
            p = app.run_file(m.group(1), m.group(2), m.group(3))
            return self._file(p, p.name) if p else self._error(404, "file not found")
        if path == "/api/paper":
            since = int(q.get("log_since", ["0"])[0] or 0)
            return self._json(app.paper_view(since))
        if path == "/api/design":
            src = Path(__file__).resolve().parents[2] / "design" / "DESIGN.md"
            return self._json({"design_md": src.read_text(encoding="utf-8") if src.exists() else "", "path": str(src)})
        self._error(404, "not found")

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        app = self.app
        if path == "/api/jobs":
            try:
                body = json.loads(self._body() or b"{}")
            except ValueError:
                return self._error(400, "invalid JSON")
            status, payload = app.start_job(body if isinstance(body, dict) else {})
            return self._json(payload, status)
        if path == "/api/jobs/current/stop":
            if app.job is None or app.job.status != "running":
                return self._json({"ok": False, "error": "no running job"}, 409)
            app.job.stop()
            return self._json({"ok": True, "job": app.job.view(with_lines=False)})
        if path == "/api/paper/start":
            try:
                body = json.loads(self._body() or b"{}")
            except ValueError:
                return self._error(400, "invalid JSON")
            status, payload = app.start_service(body if isinstance(body, dict) else {})
            return self._json(payload, status)
        if path == "/api/paper/stop":
            if app.service is None or app.service.status != "running":
                return self._json({"ok": False, "error": "the paper trader is not running"}, 409)
            app.service.stop()
            return self._json({"ok": True, "service": app.service.view(with_lines=False)})
        self._error(404, "not found")

    def do_PUT(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/api/config":
            text = self._body().decode("utf-8", errors="replace")
            status, payload = self.app.save_config(text)
            return self._json(payload, status)
        self._error(404, "not found")


def serve(config_path: str | None, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> int:
    app = App(Path.cwd(), config_path)
    Handler.app = app
    Handler.password = os.environ.get("HL_UI_PASSWORD") or None
    if host not in ("127.0.0.1", "localhost", "::1") and Handler.password is None:
        print("WARNING: listening on a non-local address without HL_UI_PASSWORD; anyone who can reach this port can run jobs and edit the config.", file=sys.stderr, flush=True)
    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        print(f"cannot bind {host}:{port}: {e}", file=sys.stderr)
        return 1
    httpd.daemon_threads = True
    url = f"http://{host}:{port}/"
    print(f"hl_screener ui  {url}", flush=True)
    print(f"  project root: {app.root}", flush=True)
    print(f"  config:       {config_path or '(built-in defaults, read-only)'}", flush=True)
    print(f"  password:     {'set (HL_UI_PASSWORD)' if Handler.password else 'none'}", flush=True)
    print("  Ctrl+C to stop", flush=True)
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if app.job is not None and app.job.status == "running":
            app.job.stop()
        if app.service is not None and app.service.status == "running":
            app.service.stop()   # a paper trader started from the page lives as long as the page's server
        httpd.server_close()
    return 0
