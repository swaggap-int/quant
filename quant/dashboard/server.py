"""A tiny stdlib web server that reads ``results/*.json`` and serves the UI.

No web framework, no build step, no CDN. It does three things:

* ``GET /``                 the single-page dashboard
* ``GET /api/runs``         index of every saved run, with headline metrics
* ``GET /api/run/<run_id>`` one full run (equity series, trades, fills, metrics)
* ``GET /api/paper``        paper-bot health plus the last few daily runs
* ``GET /api/walkforward``  the rolling walk-forward reading, from the saved study
* ``POST /api/walkforward/run``  rerun the study with its current settings
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
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from quant.paper_state import DEFAULT_MISSING_RUNS_OK_SECONDS, health_verdict
from quant.report import verdict
from quant.metrics import largest_losses, concentration_read

ROOT = Path(__file__).resolve().parent.parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_RESULTS_DIR = ROOT / "results"
DEFAULT_PORT = 8765
RUN_TIMEOUT_SECONDS = 600

DEFAULT_PAPER_DIR = DEFAULT_RESULTS_DIR / "paper"
PAPER_RUN_LIMIT = 5

WALKFORWARD_GLOB = "walkforward_rolling_*.json"
FAMILY_LABELS = {"ema": "EMA-ATR", "donchian": "Donchian"}
WALKFORWARD_TIMEOUT_SECONDS = 600

#: One study at a time: two runs would race on the same output file.
_WALKFORWARD_LOCK = threading.Lock()

RUN_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,120}$")
PAPER_STAMP_RE = re.compile(r"_(\d{8}T\d{6}Z)\.json$")
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


def paper_stamp_ms(path: Path) -> int:
    """Run time encoded in a paper-log filename, or 0 when it has none."""
    match = PAPER_STAMP_RE.search(path.name)
    if not match:
        return 0
    try:
        stamp = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ")
    except ValueError:
        return 0
    return int(stamp.replace(tzinfo=timezone.utc).timestamp() * 1000)


HEARTBEAT_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(\S.*)$")


def read_heartbeat(log: Path, now_ms: int) -> dict[str, Any] | None:
    """The newest line of ``heartbeat.log``, as a delivery result rather than prose.

    ``tasks/send_heartbeat.sh`` stamps each attempt with a local-time ``date``,
    so the parse assumes this machine's zone. A URL-less run logs a ``skip:``
    line and must not read as an attempt that failed to deliver: the two mean
    different things when the off-machine watcher goes quiet.
    """
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for raw in reversed(lines):
        line = raw.strip()
        match = HEARTBEAT_STAMP.match(line)
        if match is None:
            continue
        try:
            at = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").astimezone()
        except ValueError:
            continue
        detail = match.group(2)
        return {
            "at_iso": at.isoformat(timespec="seconds"),
            "age_seconds": round((now_ms - int(at.timestamp() * 1000)) / 1000.0),
            "ok": detail.startswith("sent ok ping"),
            "failed": detail.startswith("FAILED"),
            "skipped": detail.startswith("skip:"),
            "detail": detail,
        }
    return None


@dataclass
class PaperIndex:
    """The scheduler's view of the paper bot, read from ``results/paper/``.

    The health verdict comes from ``quant.paper_state``, the same rule the
    runner's ``--health`` probe uses, so the dashboard cannot drift into a
    different answer about whether the bot is alive.
    """

    directory: Path = DEFAULT_PAPER_DIR
    _cache: dict[str, tuple[float, dict[str, Any]]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self._cache is None:
            self._cache = {}

    def _read(self, path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
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
        if not isinstance(payload, dict):
            return None
        self._cache[str(path)] = (mtime, payload)
        return payload

    def _run_paths(self) -> list[Path]:
        """Dated run logs, newest first. State files are never runs."""
        if not self.directory.exists():
            return []
        dated: list[tuple[int, Path]] = []
        for path in self.directory.glob("*.json"):
            if path.name in ("last_run.json", "last_failure.json"):
                continue
            stamp = paper_stamp_ms(path)
            if stamp:
                dated.append((stamp, path))
        return [path for _, path in sorted(dated, key=lambda item: item[0], reverse=True)]

    @staticmethod
    def _summary(payload: dict[str, Any], fallback_id: str) -> dict[str, Any]:
        cash = (payload.get("config") or {}).get("initial_cash")
        equity = payload.get("equity_final")
        ret = None
        if isinstance(cash, (int, float)) and cash and isinstance(equity, (int, float)):
            ret = round((equity / cash - 1.0) * 100.0, 2)
        return {
            "run_id": payload.get("run_id") or fallback_id,
            "created_at": payload.get("created_at") or "",
            "data_end": payload.get("data_end_iso") or "",
            "bars": payload.get("bar_count") or 0,
            "trades": payload.get("trade_count") or 0,
            "equity": equity if isinstance(equity, (int, float)) else None,
            "return_pct": ret,
            "freshness_ok": bool(payload.get("freshness_ok", True)),
            "verdict": payload.get("verdict") or "",
        }

    def snapshot(
        self,
        limit: int = PAPER_RUN_LIMIT,
        requested_missing: float = DEFAULT_MISSING_RUNS_OK_SECONDS,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        last_run = self._read(self.directory / "last_run.json")
        last_failure = self._read(self.directory / "last_failure.json")
        verdict = health_verdict(last_run, requested_missing, now_ms)

        runs: list[dict[str, Any]] = []
        for path in self._run_paths():
            if len(runs) >= max(1, int(limit)):
                break
            payload = self._read(path)
            if payload is None:
                continue
            runs.append(self._summary(payload, path.stem))

        # A run saved through --out may not carry a dated name; the state file
        # still proves it happened, so show that rather than an empty list.
        if not runs and last_run is not None:
            runs.append(
                {
                    "run_id": last_run.get("run_id") or "last-run",
                    "created_at": last_run.get("created_at") or "",
                    "data_end": last_run.get("data_end_iso") or "",
                    "bars": 0,
                    "trades": last_run.get("trade_count") or 0,
                    "equity": last_run.get("equity_final"),
                    "return_pct": last_run.get("total_return_pct"),
                    "freshness_ok": bool(last_run.get("freshness_ok", True)),
                    "verdict": last_run.get("verdict") or "",
                }
            )

        health = {
            "healthy": verdict["healthy"],
            "reason": verdict["reason"],
            "checked_at_iso": datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).isoformat(timespec="seconds"),
            "requested_max_missing_seconds": verdict["requested_max_missing_seconds"],
            "effective_max_missing_seconds": verdict["effective_max_missing_seconds"],
            "last_run": last_run,
            "last_failure": last_failure,
            "heartbeat": read_heartbeat(self.directory / "heartbeat.log", now_ms),
        }
        return {"health": health, "runs": runs}


def _study_number(value: Any) -> int | float | None:
    """A number from a study file, or ``None`` when it is missing or not one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _plain(text: Any) -> str:
    """Study prose uses ``--`` for a dash; the page should not show a typewriter's."""
    if not isinstance(text, str):
        return ""
    return text.replace(" -- ", " \u2014 ").strip()


@dataclass
class WalkforwardIndex:
    """The rolling walk-forward study, read from the newest saved run.

    The study file already carries the per-family counts and the plain verdict,
    so the dashboard serves that reading rather than recomputing one from the
    folds: two surfaces disagreeing about the same study would be worse than
    either one alone.
    """

    directory: Path = DEFAULT_RESULTS_DIR

    def _candidates(self) -> list[Path]:
        if not self.directory.exists():
            return []
        dated: list[tuple[float, Path]] = []
        for path in self.directory.glob(WALKFORWARD_GLOB):
            if not path.is_file():
                continue
            try:
                dated.append((path.stat().st_mtime, path))
            except OSError:
                continue
        return [path for _, path in sorted(dated, key=lambda item: item[0], reverse=True)]

    def _parsed(self) -> list[tuple[Path, dict[str, Any]]]:
        """Valid studies, newest first. Other files in ``results/`` are not studies."""
        out: list[tuple[Path, dict[str, Any]]] = []
        for path in self._candidates():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict) or payload.get("study") != "rolling_walkforward":
                continue
            headline = payload.get("headline")
            if not isinstance(headline, dict):
                continue
            if not any(isinstance(read, dict) for read in headline.values()):
                continue
            out.append((path, payload))
        return out

    @staticmethod
    def _markets(payload: dict[str, Any]) -> list[str]:
        symbols = payload.get("symbols")
        symbols = symbols if isinstance(symbols, list) else []
        return [
            str(row["symbol"])
            for row in symbols
            if isinstance(row, dict) and row.get("symbol")
        ]

    @classmethod
    def _describe(cls, path: Path, payload: dict[str, Any]) -> dict[str, Any]:
        """Enough to name a saved study in a picker without loading the folds."""
        headline = payload.get("headline")
        headline = headline if isinstance(headline, dict) else {}
        keys = [key for key, read in headline.items() if isinstance(read, dict)]
        windows = [
            _study_number(read.get("windows"))
            for read in headline.values()
            if isinstance(read, dict)
        ]
        windows = [value for value in windows if isinstance(value, (int, float))]
        try:
            generated = datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.utc
            ).isoformat(timespec="seconds")
        except OSError:
            generated = ""
        return {
            "file": path.name,
            "generated_at": generated,
            "timeframe": payload.get("timeframe") or "",
            "folds": _study_number(payload.get("folds")),
            "families": keys,
            "family_labels": [
                FAMILY_LABELS.get(key, key.replace("-", " ").title()) for key in keys
            ],
            "markets": cls._markets(payload),
            "windows": max(windows) if windows else None,
        }

    @staticmethod
    def _pick(family: str, symbols: list[Any]) -> tuple[str | None, int | float | None]:
        """The setting chosen most often across markets, and its share of folds.

        The per-market summaries each carry their own modal pick; pooling the raw
        pick lists keeps a family that behaves differently on one market from
        looking unanimous.
        """
        counts: dict[str, int] = {}
        total = 0
        for row in symbols:
            if not isinstance(row, dict):
                continue
            families = row.get("families")
            read = families.get(family) if isinstance(families, dict) else None
            picks = read.get("picks") if isinstance(read, dict) else None
            if not isinstance(picks, list):
                continue
            for pick in picks:
                if isinstance(pick, str) and pick:
                    counts[pick] = counts.get(pick, 0) + 1
                    total += 1
        if not total:
            return None, None
        best = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0]
        return best[0], round(100.0 * best[1] / total, 1)

    @classmethod
    def _families(cls, headline: dict[str, Any], symbols: list[Any]) -> list[dict[str, Any]]:
        families: list[dict[str, Any]] = []
        for key, read in headline.items():
            if not isinstance(read, dict):
                continue
            pick, pick_pct = cls._pick(key, symbols)
            families.append(
                {
                    "key": key,
                    "label": FAMILY_LABELS.get(key, key.replace("-", " ").title()),
                    "windows": _study_number(read.get("windows")),
                    "beat": _study_number(read.get("beat_buy_hold")),
                    "risk_beat": _study_number(read.get("risk_adjusted_beat")),
                    "risk_windows": _study_number(read.get("risk_adjusted_windows")),
                    "median_excess": _study_number(read.get("median_test_excess")),
                    "pick": pick,
                    "pick_pct": pick_pct,
                    "verdict": _plain(read.get("verdict")),
                }
            )
        return families

    def snapshot(self, file: str | None = None) -> dict[str, Any]:
        """The chosen study's reading, plus the list of saved studies.

        ``file`` must be one of the names from that list; it is compared against
        the directory listing rather than joined onto a path, so a query string
        cannot point outside ``results/``.
        """
        entries = self._parsed()
        studies = [self._describe(path, payload) for path, payload in entries]
        if not entries:
            return {"available": False, "families": [], "verdict": None, "studies": []}

        chosen = entries[0]
        if file:
            match = next(((path, payload) for path, payload in entries if path.name == file), None)
            if match is None:
                return {
                    "available": False,
                    "families": [],
                    "verdict": None,
                    "studies": studies,
                    "missing": file,
                }
            chosen = match

        path, payload = chosen
        headline = payload["headline"]
        symbols = payload.get("symbols") if isinstance(payload.get("symbols"), list) else []
        families = self._families(headline, symbols)
        if not families:
            return {
                "available": False,
                "families": [],
                "verdict": None,
                "studies": studies,
                "missing": path.name,
            }

        markets = self._markets(payload)
        first = symbols[0] if symbols and isinstance(symbols[0], dict) else {}
        windows = [f["windows"] for f in families if isinstance(f["windows"], (int, float))]
        reads = {f["verdict"] for f in families if f["verdict"]}

        try:
            generated = datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.utc
            ).isoformat(timespec="seconds")
        except OSError:
            generated = ""

        return {
            "available": True,
            "file": path.name,
            "generated_at": generated,
            "timeframe": payload.get("timeframe") or "",
            "folds": _study_number(payload.get("folds")),
            "windows": max(windows) if windows else None,
            "markets": markets,
            "first_train": first.get("first_train") or "",
            "last_test": first.get("last_test") or "",
            "families": families,
            # One sentence only when every family says the same thing;
            # otherwise the page shows each family's own read.
            "verdict": next(iter(reads)) if len(reads) == 1 else None,
            "studies": studies,
        }


def build_walkforward_command(
    payload: dict[str, Any], out_dir: Path | None = None
) -> list[str]:
    """Validate a rerun request and turn it into a ``run_walkforward.py`` command.

    The dashboard only replays the settings the saved study already used, but the
    values still come from the browser, so each one is bounded here rather than
    trusted. With ``out_dir`` the run is pinned to a dated file in that directory,
    so the dashboard reads back exactly the study it just wrote.
    """
    timeframe = str(payload.get("timeframe", "4h"))
    if timeframe not in TIMEFRAMES:
        raise BadRequest(f"timeframe must be one of {', '.join(sorted(TIMEFRAMES))}")

    try:
        folds = int(payload.get("folds", 5))
    except (TypeError, ValueError):
        raise BadRequest("folds must be a whole number") from None
    if not 3 <= folds <= 10:
        raise BadRequest("folds must be between 3 and 10")

    family = str(payload.get("family", "both"))
    if family not in ("ema", "donchian", "both"):
        raise BadRequest("family must be ema, donchian or both")

    raw_symbols = str(payload.get("symbols", "BTC/USDT,ETH/USDT"))
    symbols = [part.strip().upper() for part in raw_symbols.split(",") if part.strip()]
    if not symbols or len(symbols) > 4:
        raise BadRequest("symbols must list 1 to 4 pairs")
    for symbol in symbols:
        if not SYMBOL_RE.match(symbol):
            raise BadRequest(f"symbol {symbol!r} must look like BTC/USDT")

    start = str(payload.get("start", "")).strip()
    if start and not re.match(r"^\d{4}-\d{2}-\d{2}$", start):
        raise BadRequest("start must be YYYY-MM-DD")

    cmd = [
        sys.executable,
        str(ROOT / "run_walkforward.py"),
        "--symbols", ",".join(symbols),
        "--timeframe", timeframe,
        "--folds", str(folds),
        "--family", family,
    ]
    if start:
        cmd += ["--start", start]
    if out_dir is not None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        name = f"walkforward_rolling_{timeframe.replace('/', '-')}_{stamp}.json"
        cmd += ["--out", str(Path(out_dir) / name)]
    return cmd


def launch_walkforward(
    payload: dict[str, Any], results_dir: Path = DEFAULT_RESULTS_DIR
) -> dict[str, Any]:
    """Rerun the study synchronously and return its fresh reading."""
    if not (ROOT / "run_walkforward.py").exists():
        raise BadRequest("run_walkforward.py not found next to the dashboard")
    cmd = build_walkforward_command(payload, out_dir=Path(results_dir))
    with _WALKFORWARD_LOCK:
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                timeout=WALKFORWARD_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise BadRequest(
                f"walk-forward exceeded {WALKFORWARD_TIMEOUT_SECONDS}s"
            ) from None

    if proc.returncode != 0:
        lines = (proc.stderr or proc.stdout or "walk-forward failed").strip().splitlines()
        raise BadRequest(lines[-1] if lines else "walk-forward failed")

    snapshot = WalkforwardIndex(directory=Path(results_dir)).snapshot()
    if not snapshot["available"]:
        raise BadRequest("the run finished but no study file could be read")
    return snapshot


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


RESULTS_ROOT = Path(__file__).resolve().parent.parent.parent / "results"


def _results_path(path: str) -> Path | None:
    """Resolve a /results/... request onto disk under the results directory.

    The dashboard uses this only for the enriched excess/breadth read, so the path
    is intentionally narrow: it must sit under the results directory and end with .json.

    RESULTS_ROOT is read from the *module* at call time so that tests (and any other
    caller) can monkeypatch ``quant.dashboard.server.RESULTS_ROOT`` and have their
    patch honored for the lifetime of a Handler class instance.
    """
    import quant.dashboard.server as _mod
    root = _mod.RESULTS_ROOT
    # The route prefix is /results/, so strip one leading segment.
    # Avoid double-counting "results/" when joining onto RESULTS_ROOT.
    stripped = path.lstrip("/")
    if stripped.startswith("results/"):
        stripped = stripped[len("results/"):]
    elif stripped == "results":
        stripped = ""
    safe = (root / stripped).resolve() if stripped else root.resolve()
    try:
        safe.relative_to(root.resolve())
    except ValueError:
        return None
    if safe.suffix != ".json":
        return None
    if not safe.is_file():
        return None
    return safe


def _excess_sources() -> tuple[Path, Path, Path]:
    """Return the three source files the excess/breadth panel depends on.

    Read at call time from the module so that tests can monkeypatch
    ``quant.dashboard.server.RESULTS_ROOT`` and have a single place to patch.
    """
    import quant.dashboard.server as _mod
    root = _mod.RESULTS_ROOT
    return (
        root / "status.json",
        root / "explore_notes.json",
        root / "robustness.json",
    )


_EXCESS_SOURCES = _excess_sources()


def _excess_sources_fresh(out: Path) -> bool:
    """Is the excess snapshot worth serving?

    The dashboard must not surface a frozen read whose backing sources have since
    changed, and must not surface one whose sources are missing. Missing sources are
    the common case on a fresh checkout before any study has run.

    The source set is read at call time from the module so ``_EXCESS_SOURCES`` can be
    monkeypatched by tests without rebuilding Handler classes.
    """
    if not out.is_file():
        return False
    try:
        out_mtime = out.stat().st_mtime
    except OSError:
        return False
    for src in _EXCESS_SOURCES:
        if not src.is_file():
            return False
        try:
            if src.stat().st_mtime > out_mtime:
                return False
        except OSError:
            return False
    return True


def make_handler(index: RunIndex) -> type[BaseHTTPRequestHandler]:
    paper = PaperIndex(directory=index.directory / "paper")
    walkforward = WalkforwardIndex(directory=index.directory)

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
            parsed = urlsplit(self.path)
            path = parsed.path
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
                if path == "/api/paper":
                    return self._json(200, paper.snapshot())
                if path == "/api/walkforward":
                    requested = (parse_qs(parsed.query).get("file") or [""])[0]
                    return self._json(200, walkforward.snapshot(requested or None))
                if path == "/api/health":
                    return self._json(200, {"ok": True, "runs": len(index.summaries())})
                # the excess/breadth panel reads the frozen source from disk through here
                if path.startswith("/results/dashboard_excess.json"):
                    file = _results_path(path)
                    if file is None:
                        return self._json(404, {"error": "not found"})
                    if not _excess_sources_fresh(file):
                        return self._json(404, {"error": "excess snapshot is stale or its sources are missing; rebuild results/dashboard_excess.json"})
                    try:
                        data = json.loads(file.read_text(encoding="utf-8"))
                    except Exception as exc:
                        return self._json(500, {"error": f"could not read {file.name}: {exc}"})
                    return self._json(200, data)
                if path == "/script.js":
                    return self._static("script.js")
                # Anything else under /results/... that isn't dashboard_excess.json is a
                # legitimate 404 (not a stale guard), so the browser can keep retrying.
                if path.startswith("/results/"):
                    file = _results_path(path)
                    if file is None:
                        return self._json(404, {"error": "results file not found"})
                    if not file.is_file():
                        return self._json(404, {"error": "results file not found"})
                    try:
                        data = json.loads(file.read_text(encoding="utf-8"))
                    except Exception as exc:
                        return self._json(500, {"error": f"could not read {file.name}: {exc}"})
                    return self._json(200, data)
                return self._json(404, {"error": "not found"})
            except BadRequest as exc:
                return self._json(400, {"error": str(exc)})
            except BrokenPipeError:  # client navigated away mid-response
                return

        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 64_000:
                raise BadRequest("empty or oversized request body")
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            if not isinstance(payload, dict):
                raise BadRequest("expected a JSON object")
            return payload

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            try:
                if path == "/api/walkforward/run":
                    return self._json(
                        200, launch_walkforward(self._body(), results_dir=index.directory)
                    )
                if path != "/api/run":
                    return self._json(404, {"error": "not found"})
                result = launch_backtest(self._body())
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
