"""A tiny stdlib web server that reads ``results/*.json`` and serves the UI.

No web framework, no build step, no CDN. It does three things:

* ``GET /``                 the single-page dashboard
* ``GET /api/runs``         index of every saved run, with headline metrics
* ``GET /api/run/<run_id>`` one full run (equity series, trades, fills, metrics)
* ``POST /api/run``         launch ``run_backtest.py`` with validated arguments

The POST endpoint shells out to the CLI with a fixed argument list (never a
shell string) and only accepts values that pass validation, so the worst a
hostile local request can do is run a backtest you already had permission to
run. It is bound to loopback and is not a public web service.
"""

from __future__ import annotations

import json
import re
import socket
import subprocess
import sys
import threading
from dataclasses import dataclass
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from quant.report import verdict
from quant.metrics import largest_losses, concentration_read

ROOT = Path(__file__).resolve().parent.parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_RESULTS_DIR = ROOT / "results"
DEFAULT_PORT = 8765
RUN_TIMEOUT_SECONDS = 600

RUN_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,120}$")
SYMBOL_RE = re.compile(r"^[A-Za-z0-9]{2,12}[/\-][A-Za-z0-9]{2,12}$")
TIMEFRAMES = {"1m", "5m", "15m", "1h", "4h", "1d", "1w"}
PROVIDERS = {"auto", "binance", "okx", "kraken", "coinbase"}

#: Bounds for user-supplied numbers, so a typo cannot ask for absurd work.
NUMERIC_BOUNDS = {
    "cash": (100.0, 10_000_000.0),
    "fee_bps": (0.0, 100.0),
    "slippage_bps": (0.0, 200.0),
    "risk": (0.001, 0.5),
    "fast": (2, 400),
    "slow": (3, 600),
    "atr_stop_mult": (0.1, 20.0),
    "trail_mult": (0.1, 20.0),
}


class BadRequest(ValueError):
    """Raised when a request payload fails validation."""


# --------------------------------------------------------------------------- #
# Result files
# --------------------------------------------------------------------------- #


@dataclass
class RunIndex:
    """Reads the results directory, caching by modification time."""

    directory: Path = DEFAULT_RESULTS_DIR
    _cache: dict[str, tuple[float, dict[str, Any]]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self._cache is None:
            self._cache = {}

    def paths(self) -> list[Path]:
        if not self.directory.exists():
            return []
        return sorted(
            (p for p in self.directory.glob("*.json") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

    def _load(self, path: Path) -> dict[str, Any] | None:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None
        cached = self._cache.get(str(path))
        if cached is not None and cached[0] == mtime:
            return cached[1]
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        # The results directory holds backtest results, but other studies (the
        # robustness sweep, for one) write JSON there too. Anything without a
        # run id and metrics is not a run, and must not show up as a junk row.
        if not isinstance(payload, dict) or "run_id" not in payload or "metrics" not in payload:
            return None
        self._cache[str(path)] = (mtime, payload)
        return payload

    def summaries(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for path in self.paths():
            payload = self._load(path)
            if not payload:
                continue
            trades = payload.get("trades", [])
            if isinstance(trades, int):
                trades = []
            m = payload.get("metrics", {})
            d = payload.get("data", {})
            cc = concentration_read(trades) if isinstance(trades, list) else {}
            out.append(
                {
                    "run_id": payload.get("run_id", path.stem),
                    "file": path.name,
                    "created_at": payload.get("created_at", ""),
                    "symbol": d.get("symbol", ""),
                    "timeframe": d.get("timeframe", ""),
                    "bars": d.get("bars", 0),
                    "start": d.get("start", ""),
                    "end": d.get("end", ""),
                    "provider": d.get("provider", ""),
                    "strategy": payload.get("strategy", {}).get("name", ""),
                    "total_return_pct": m.get("total_return_pct", 0.0),
                    "buy_hold_return_pct": m.get("buy_hold_return_pct", 0.0),
                    "max_drawdown_pct": m.get("max_drawdown_pct", 0.0),
                    "sharpe": m.get("sharpe", 0.0),
                    "trades": m.get("trades", 0),
                    "final_equity": m.get("final_equity", 0.0),
                    "read": verdict(m),
                    "largest_losses": largest_losses(trades) if isinstance(trades, list) else [],
                    "top_losses_share": cc.get("top_losses_share"),
                    "top_wins_share": cc.get("top_wins_share"),
                }
            )
        return out

    def get(self, run_id: str) -> dict[str, Any] | None:
        if not RUN_ID_RE.match(run_id):
            raise BadRequest("malformed run id")
        for path in self.paths():
            if path.stem == run_id or path.name == f"{run_id}.json":
                payload = self._load(path)
                if payload and isinstance(payload.get("trades"), list):
                    m = payload.setdefault("metrics", {})
                    if "largest_losses" not in payload:
                        payload["largest_losses"] = largest_losses(payload["trades"])
                    cc = concentration_read(payload["trades"])
                    m.setdefault("top_losses_share", cc["top_losses_share"])
                    m.setdefault("top_wins_share", cc["top_wins_share"])
                return payload
        return None


# --------------------------------------------------------------------------- #
# Validation + launching a run
# --------------------------------------------------------------------------- #


def _number(payload: dict[str, Any], key: str) -> float | int | None:
    if key not in payload or payload[key] in (None, ""):
        return None
    try:
        value = float(payload[key])
    except (TypeError, ValueError):
        raise BadRequest(f"{key} must be a number") from None
    low, high = NUMERIC_BOUNDS[key]
    if not (low <= value <= high):
        raise BadRequest(f"{key} must be between {low} and {high}")
    return int(value) if key in ("fast", "slow") else value


def build_command(payload: dict[str, Any]) -> list[str]:
    """Validate a request payload and turn it into a CLI argument list."""
    symbol = str(payload.get("symbol", "BTC/USDT")).strip()
    if not SYMBOL_RE.match(symbol):
        raise BadRequest("symbol must look like BTC/USDT")

    timeframe = str(payload.get("timeframe", "1d"))
    if timeframe not in TIMEFRAMES:
        raise BadRequest(f"timeframe must be one of {', '.join(sorted(TIMEFRAMES))}")

    provider = str(payload.get("provider", "auto"))
    if provider not in PROVIDERS:
        raise BadRequest("unknown provider")

    start = str(payload.get("start", "")).strip()
    if start and not re.match(r"^\d{4}-\d{2}-\d{2}$", start):
        raise BadRequest("start must be YYYY-MM-DD")

    # No --out: the CLI names its own file results/<run_id>.json, which is what
    # launch_backtest looks for afterwards.
    cmd = [
        sys.executable,
        str(ROOT / "run_backtest.py"),
        "--symbol", symbol,
        "--timeframe", timeframe,
        "--provider", provider,
        "--quiet",
    ]
    if start:
        cmd += ["--start", start]
    for key, flag in (
        ("cash", "--cash"),
        ("fee_bps", "--fee-bps"),
        ("slippage_bps", "--slippage-bps"),
        ("risk", "--risk"),
        ("fast", "--fast"),
        ("slow", "--slow"),
        ("atr_stop_mult", "--atr-stop-mult"),
        ("trail_mult", "--trail-mult"),
    ):
        value = _number(payload, key)
        if value is not None:
            cmd += [flag, str(value)]
    if payload.get("allow_short"):
        cmd += ["--allow-short"]
    if payload.get("no_trend_filter"):
        cmd += ["--no-trend-filter"]
    return cmd


#: Runs are serialised so two browser tabs cannot race on the results directory.
_RUN_LOCK = threading.Lock()


def launch_backtest(payload: dict[str, Any], results_dir: Path = DEFAULT_RESULTS_DIR) -> dict[str, Any]:
    """Run the CLI synchronously and report the outcome."""
    if not (ROOT / "run_backtest.py").exists():
        raise BadRequest("run_backtest.py not found next to the dashboard")
    cmd = build_command(payload)
    with _RUN_LOCK:
        before = {p.name for p in results_dir.glob("*.json")} if results_dir.exists() else set()
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                timeout=RUN_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise BadRequest(f"backtest exceeded {RUN_TIMEOUT_SECONDS}s") from None

        stdout = proc.stdout or ""
        if proc.returncode != 0:
            lines = (proc.stderr or stdout or "backtest failed").strip().splitlines()
            raise BadRequest(lines[-1] if lines else "backtest failed")

        # Identify the file this run just wrote rather than parsing the report.
        new_files = sorted(
            (p for p in results_dir.glob("*.json") if p.name not in before),
            key=lambda p: p.stat().st_mtime,
        )
        run_id = None
        if new_files:
            try:
                run_id = json.loads(new_files[-1].read_text(encoding="utf-8")).get("run_id")
            except (OSError, json.JSONDecodeError):
                run_id = new_files[-1].stem

    verdict_line = next(
        (ln.strip() for ln in reversed(stdout.splitlines()) if ln.strip().startswith(("Read with care", "No red flags"))),
        "",
    )
    result_payload = {"run_id": run_id, "report": stdout, "verdict": verdict_line}
    # Keep the on-disk result consistent with what the dashboard renders, so a freshly
    # launched run already carries largest_losses + top_losses_share/top_wins_share for
    # the fragility strip without a separate normalization pass. The POST response and
    # the file on disk therefore both include those fields.
    try:
        path = ROOT / "results" / f"{run_id}.json"
        file_payload = json.loads(path.read_text(encoding="utf-8"))
        trades = file_payload.get("trades", [])
        if isinstance(trades, list):
            cc = concentration_read(trades)
            enriched = {
                "largest_losses": largest_losses(trades),
                "metrics": dict(file_payload.get("metrics", {}),
                               top_losses_share=cc["top_losses_share"],
                               top_wins_share=cc["top_wins_share"]),
            }
            file_payload.update(enriched)
            result_payload["largest_losses"] = enriched["largest_losses"]
            result_payload["metrics"] = enriched["metrics"]
            path.write_text(json.dumps(file_payload, indent=2, allow_nan=False), encoding="utf-8")
            result_payload.update(enriched)
    except Exception:
        pass
    return result_payload


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


def make_handler(index: RunIndex) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "quant-dashboard/0.1"
        protocol_version = "HTTP/1.1"

        # -- helpers ---------------------------------------------------- #

        def _send(self, status: int, body: bytes, content_type: str, cache: str = "no-store") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, allow_nan=False).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8")

        def log_message(self, fmt: str, *args: Any) -> None:  # keep the console readable
            if self.path.startswith("/api/run"):
                sys.stderr.write(f"  dashboard: {fmt % args}\n")

        # -- routes ----------------------------------------------------- #

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            try:
                if path in ("/", "/index.html"):
                    return self._static("index.html")
                if path == "/api/runs":
                    return self._json(200, {"runs": index.summaries()})
                if path.startswith("/api/run/"):
                    run_id = path[len("/api/run/") :]
                    payload = index.get(run_id)
                    if payload is None:
                        return self._json(404, {"error": f"run {run_id!r} not found"})
                    if isinstance(payload.get("trades"), list):
                        m = payload.setdefault("metrics", {})
                        if "largest_losses" not in payload:
                            payload["largest_losses"] = largest_losses(payload["trades"])
                        cc = concentration_read(payload["trades"])
                        m.setdefault("top_losses_share", cc["top_losses_share"])
                        m.setdefault("top_wins_share", cc["top_wins_share"])
                    return self._json(200, payload)
                if path == "/api/health":
                    return self._json(200, {"ok": True, "runs": len(index.summaries())})
                return self._json(404, {"error": "not found"})
            except BadRequest as exc:
                return self._json(400, {"error": str(exc)})
            except BrokenPipeError:  # client navigated away mid-response
                return

        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path != "/api/run":
                return self._json(404, {"error": "not found"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0 or length > 64_000:
                    raise BadRequest("empty or oversized request body")
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    raise BadRequest("expected a JSON object")
                result = launch_backtest(payload)
                return self._json(200, result)
            except json.JSONDecodeError:
                return self._json(400, {"error": "invalid JSON body"})
            except BadRequest as exc:
                return self._json(400, {"error": str(exc)})
            except Exception as exc:  # pragma: no cover - defensive
                return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

        def _static(self, name: str) -> None:
            target = (STATIC_DIR / name).resolve()
            if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
                return self._json(404, {"error": "not found"})
            content_type = {
                ".html": "text/html; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".js": "text/javascript; charset=utf-8",
                ".svg": "image/svg+xml",
            }.get(target.suffix, "application/octet-stream")
            self._send(200, target.read_bytes(), content_type)

    return Handler


def find_free_port(preferred: int = DEFAULT_PORT, host: str = "127.0.0.1", attempts: int = 20) -> int:
    """First free port at or after ``preferred``."""
    for port in range(preferred, preferred + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"no free port in {preferred}-{preferred + attempts - 1}")


def serve(
    port: int = DEFAULT_PORT,
    results_dir: Path | str = DEFAULT_RESULTS_DIR,
    host: str = "127.0.0.1",
    open_browser: bool = False,
) -> ThreadingHTTPServer:
    """Start the dashboard. Returns the running server (so callers can stop it)."""
    index = RunIndex(directory=Path(results_dir))
    handler = make_handler(index)
    httpd = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{port}/"
    runs = len(index.summaries())
    print(f" dashboard  {url}  ({runs} saved run{'s' if runs != 1 else ''})")
    print(f" results    {index.directory}")
    if open_browser:
        threading.Timer(0.4, partial(_open, url)).start()
    return httpd


def _open(url: str) -> None:  # pragma: no cover - convenience
    import webbrowser

    webbrowser.open(url)
