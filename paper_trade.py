#!/usr/bin/env python
"""Automated paper-trading runner with a scheduler-facing health state.

This is still a paper-only replay. It does not send real orders.

The important change in this version is not the paper logic. It is that the
runner now keeps a small, machine-readable health state so a scheduler can ask
one question: “is the paper bot alive and recently healthy?”

What changed
------------
* Every successful run writes results/paper/last_run.json.
* Every hard failure also writes results/paper/last_failure.json with the
  failure kind and the reason, but only when the failure is the kind a
  scheduler should notice: stale data, no-action window, or a real error.
* `paper_trade.py --health` is the scheduler-facing probe. It reads the state
  files and emits one JSON blob plus an exit code that a job can react to.
* `paper_trade.py --status` is the human-facing summary of the same state.
* A silent failure is no longer silent: if no successful run has happened in
  too long, the health probe reports unhealthy even when there is no explicit
  last-failure record.

What this version still does not do
------------------------------------
* Trade live.
* Manage its own schedule.
* Write anything that could be mistaken for a real order.

Invocation
----------
    python paper_trade.py                 # run one automated paper episode
    python paper_trade.py --json
    python paper_trade.py --health       # scheduler probe: is the bot healthy?
    python paper_trade.py --status       # human summary of last state
    python paper_trade.py --live-would-run   # refuse the live path explicitly
    python paper_trade.py --list-skills  # readonly probe of account-skill dirs
    python paper_trade.py --skill-read quant/config.py
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from quant.config import BacktestConfig, Costs, RiskConfig
from quant.datafeed import Candles, DataError, load_candles, _now_ms
from quant.engine import Engine
from quant.paper_state import DEFAULT_MISSING_RUNS_OK_SECONDS, health_verdict
from quant.report import format_report, verdict
from quant.strategies import EmaAtrTrend

HERE = Path(__file__).resolve().parent
RESULTS_DIR = HERE / "results" / "paper"
SKILL_DIR = Path.home() / ".config" / "freebuff-desktop" / "account-skills"


DEFAULT_BARS = 60
DEFAULT_MAX_STALE_SECONDS = 1800.0
DEFAULT_MAX_FUTURE_SECONDS = 600.0
DEFAULT_PRUNE_KEEP_DAYS = 30.0

# Run logs are named <run_id>_<YYYYMMDDTHHMMSSZ>.json. Only files with that
# dated suffix are eligible for retention pruning, so the health state files
# and anything hand-made are never touched.
RUN_STAMP_RE = re.compile(r"_(\d{8}T\d{6}Z)\.json$")


# ---------------------------------------------------------------------------
# Health state files
# ---------------------------------------------------------------------------

LAST_RUN_PATH = RESULTS_DIR / "last_run.json"
LAST_FAILURE_PATH = RESULTS_DIR / "last_failure.json"


@dataclass
class LastRun:
    run_id: str
    created_at: str
    success: bool
    exit_code: int
    mode: str
    paper_only: bool
    symbol: str
    timeframe: str
    config_hash: str
    data_end_iso: str
    data_end_ms: int
    run_timestamp_ms: int
    freshness_ok: bool
    trade_count: int
    exposure_bars: int
    stops_hit: int
    equity_final: float
    total_return_pct: float
    verdict: str
    note: str = ""


@dataclass
class LastFailure:
    created_at: str
    exit_code: int
    kind: str
    reason: str
    symbol: str = ""
    timeframe: str = ""
    config_hash: str = ""
    data_end_iso: str = ""
    run_id: str = ""


# ---------------------------------------------------------------------------
# Paper-run record
# ---------------------------------------------------------------------------

@dataclass
class PaperDecision:
    ts: int
    timeframe: str
    bar_index: int
    close: float
    equity_before: float
    equity_after: float
    position_before: str
    decision_type: str
    decision: dict[str, Any] = field(default_factory=dict)
    fills: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "timeframe": self.timeframe,
            "bar_index": self.bar_index,
            "close": self.close,
            "equity_before": self.equity_before,
            "equity_after": self.equity_after,
            "position_before": self.position_before,
            "decision_type": self.decision_type,
            "decision": self.decision,
            "fills": [dict(f) for f in self.fills],
            "notes": list(self.notes),
        }


@dataclass
class PaperRun:
    run_id: str
    created_at: str
    config: BacktestConfig
    strategy_name: str
    strategy_params: dict[str, Any]
    data: dict[str, Any]
    bar_count: int
    decisions: list[PaperDecision]
    fills: list[dict[str, Any]]
    trade_count: int
    exposure_bars: int
    equity_final: float
    equity_series: dict[str, list[float]]
    stops_hit: int
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    mode: str = "paper_fixed_window"
    paper_only: bool = True
    config_hash: str = ""
    data_end_iso: str = ""
    data_end_ms: int = 0
    run_timestamp_ms: int = 0
    freshness_ok: bool = True
    freshness_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "created_at": self.created_at,
            "mode": self.mode,
            "paper_only": self.paper_only,
            "config": self.config.to_dict(),
            "strategy": {
                "name": self.strategy_name,
                "params": self.strategy_params,
            },
            "data": self.data,
            "config_hash": self.config_hash,
            "data_end_iso": self.data_end_iso,
            "data_end_ms": self.data_end_ms,
            "run_timestamp_ms": self.run_timestamp_ms,
            "freshness_ok": self.freshness_ok,
            "freshness_reason": self.freshness_reason,
            "bar_count": self.bar_count,
            "trade_count": self.trade_count,
            "exposure_bars": self.exposure_bars,
            "stops_hit": self.stops_hit,
            "equity_final": self.equity_final,
            "equity_series": self.equity_series,
            "fills": [dict(f) for f in self.fills],
            "decisions": [d.to_dict() for d in self.decisions],
            "notes": list(self.notes),
            "errors": list(self.errors),
            "verdict": self._verdict(),
        }

    def _total_return_pct(self) -> float:
        if self.equity_final == 0.0 or not self.data.get("start_equity"):
            return 0.0
        return (self.equity_final / float(self.data["start_equity"]) - 1.0) * 100.0

    def _verdict(self) -> str:
        if self.trade_count:
            return verdict(self._metrics())
        return "Read with care: only 0 trades \u2014 too few to be calm about."

    def _metrics(self) -> dict[str, Any]:
        return {
            "total_return_pct": self._total_return_pct(),
            "sharpe": None,
            "max_drawdown_pct": None,
            "trades": self.trade_count,
            "win_rate_pct": None,
            "profit_factor": None,
            "buy_hold_return_pct": None,
            "excess_return_pct": None,
            "bars_in_market_pct": (self.exposure_bars / self.bar_count * 100.0)
            if self.bar_count
            else 0.0,
        }

    def save_run(self, path: Path | str) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), indent=2, allow_nan=False),
            encoding="utf-8",
        )
        return path


# ---------------------------------------------------------------------------
# Build the strategy the same way the backtester does
# ---------------------------------------------------------------------------

def build_paper_strategy(args: argparse.Namespace) -> EmaAtrTrend:
    return EmaAtrTrend(
        fast=args.fast,
        slow=args.slow,
        atr_period=args.atr_period,
        atr_stop_mult=args.atr_stop_mult,
        trail_mult=args.trail_mult,
        risk_pct=args.risk,
        max_weight=args.max_position,
        trend_filter=not args.no_trend_filter,
    )


# ---------------------------------------------------------------------------
# Reuse the existing engine exactly, then extract a readable paper log
# ---------------------------------------------------------------------------

def run_paper_fixed_window(
    args: argparse.Namespace,
    *,
    now_ms: int,
    max_stale_seconds: float,
    max_future_seconds: float,
) -> PaperRun:
    config = BacktestConfig(
        symbol=args.symbol,
        timeframe=args.timeframe,
        start=args.start,
        end=args.end,
        initial_cash=args.cash,
        costs=Costs(fee_bps=args.fee_bps, slippage_bps=args.slippage_bps),
        risk=RiskConfig(
            risk_per_trade=args.risk,
            max_position_pct=args.max_position,
            allow_short=args.allow_short,
        ),
        provider=args.provider,
    )

    strategy = build_paper_strategy(args)

    candles = load_candles(
        config.symbol,
        config.timeframe,
        config.start,
        config.end,
        provider=config.provider,
        refresh=args.refresh,
        csv_path=args.csv,
        verbose=not args.quiet,
    )

    if len(candles) < 10:
        raise ValueError(f"need at least 10 bars for a paper window, got {len(candles)}")

    data_end_ms = int(candles.ts[-1])
    freshness_reason = ""
    freshness_ok = True

    age_seconds = (now_ms - data_end_ms) / 1000.0
    future_seconds = (data_end_ms - now_ms) / 1000.0
    # A daily bar only closes once per 86400s; a flat 1800s limit would fail
    # every 1d run by design. Floor the limit at 1.5 bars so intraday stays
    # strict but 1d/1w get a sane window. Explicit --max-stale-seconds can
    # only raise above the floor, never tighten below it.
    effective_max_stale = max(float(max_stale_seconds), 1.5 * float(candles.bar_ms) / 1000.0)
    if future_seconds > max_future_seconds:
        freshness_ok = False
        freshness_reason = (
            f"data ends {future_seconds:.0f}s in the future; "
            f"max allowed {max_future_seconds:.0f}s"
        )
    elif age_seconds > effective_max_stale:
        freshness_ok = False
        freshness_reason = (
            f"data ends {age_seconds:.0f}s ago; "
            f"max stale {effective_max_stale:.0f}s "
            f"(requested {float(max_stale_seconds):.0f}s, 1.5-bar floor {1.5 * float(candles.bar_ms) / 1000.0:.0f}s)"
        )

    window = candles.slice(
        end_ms=candles.ts[-1] + candles.bar_ms,
        start_ms=candles.ts[-1] - (args.bars - 1) * candles.bar_ms,
    )
    if len(window) < 10:
        raise ValueError(f"replay window too short after slicing: {len(window)} bars")

    result = Engine(config, strategy).run(window)

    n = len(window)
    equity = result.equity
    cash = result.cash
    exposure = result.exposure

    decisions: list[PaperDecision] = []
    stops_hit = 0
    exposure_bars = 0
    notes: list[str] = []
    errors: list[str] = []

    fill_by_ts: dict[int, list[dict[str, Any]]] = {}
    for f in result.fills:
        fill_by_ts.setdefault(int(f["ts"]), []).append(dict(f))

    for i in range(n):
        equity_before = float(cash[i]) if i > 0 else float(config.initial_cash)
        equity_after = float(equity[i])
        if exposure[i] > 0:
            exposure_bars += 1

        dec = PaperDecision(
            ts=int(window.ts[i]),
            timeframe=window.timeframe,
            bar_index=i,
            close=float(window.close[i]),
            equity_before=equity_before,
            equity_after=equity_after,
            position_before="flat" if exposure[i] == 0 else "exposed",
            decision_type="bar_processed",
            notes=[],
        )
        fills_here = fill_by_ts.get(int(window.ts[i]), [])
        if fills_here:
            dec.fills = fills_here
            dec.decision_type = "fill"
            for f in fills_here:
                if f.get("reason", "").startswith("stop"):
                    stops_hit += 1
        else:
            dec.decision_type = "no_action"
        decisions.append(dec)

    notes.extend(result.notes)

    return PaperRun(
        run_id=result.run_id,
        created_at=result.created_at,
        config=config,
        strategy_name=strategy.name,
        strategy_params=strategy.describe(),
        data={
            "symbol": window.symbol,
            "timeframe": window.timeframe,
            "provider": window.provider,
            "bars": len(window),
            "start": window.start_iso,
            "end": window.end_iso,
            "start_equity": float(config.initial_cash),
            "data_end_ms": data_end_ms,
        },
        bar_count=n,
        decisions=decisions,
        fills=[dict(f) for f in result.fills],
        trade_count=len(result.trades),
        exposure_bars=exposure_bars,
        equity_final=float(equity[-1]),
        equity_series=result.equity_series(max_points=200),
        stops_hit=stops_hit,
        notes=notes,
        errors=errors,
        config_hash=_stable_config_tag(config),
        data_end_iso=window.end_iso,
        data_end_ms=data_end_ms,
        run_timestamp_ms=now_ms,
        freshness_ok=freshness_ok,
        freshness_reason=freshness_reason,
    )


# ---------------------------------------------------------------------------
# Guardrails + failure kinds
# ---------------------------------------------------------------------------

def _stable_config_tag(config: BacktestConfig) -> str:
    return _stable_config_json_str(config)


def _stable_config_json_str(config: BacktestConfig) -> str:
    payload = {
        "symbol": config.symbol,
        "timeframe": config.timeframe,
        "start": config.start,
        "end": config.end,
        "initial_cash": config.initial_cash,
        "fee_bps": config.costs.fee_bps,
        "slippage_bps": config.costs.slippage_bps,
        "risk_per_trade": config.risk.risk_per_trade,
        "max_position_pct": config.risk.max_position_pct,
        "allow_short": config.risk.allow_short,
        "provider": config.provider,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _parse_stable_config_json_str(value: str) -> dict[str, Any] | str:
    """Mirror the canonical config tag so the health state stores the actual
    config object, not a JSON string inside a JSON string."""
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value
    if isinstance(parsed, dict):
        return parsed
    return value


def guardrails_ok(run: PaperRun, args: argparse.Namespace) -> list[str]:
    problems: list[str] = []
    if not run.paper_only:
        problems.append("paper_only flag is not true; refusing money-adjacent output")
    if not run.freshness_ok:
        problems.append("data freshness failed: " + run.freshness_reason)
    if run.errors:
        problems.append("paper run recorded errors: " + "; ".join(run.errors))
    if run.trade_count == 0:
        problems.append("no trades in this paper window; decide whether that is expected")
    if run.bar_count < 10:
        problems.append("paper window is suspiciously short")
    if getattr(args, "paper_only", True) is not True:
        problems.append(
            "paper_only is not locked to true in this version; "
            "live execution is not wired yet"
        )
    return problems


def hard_failure_kind(run: PaperRun) -> str | None:
    """Return the fail kind a scheduler should care about, or None.

    Quiet windows are valid paper output: a flat 60-bar stretch with no
    setup is still a successful replay (guardrails_ok keeps a soft note so
    a human sees it). Only stale data and real errors page the scheduler.
    """
    if not run.freshness_ok:
        return "stale_data"
    if run.errors:
        return "run_error"
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="paper_trade.py",
        description="Automated paper-trading runner with a scheduler-facing health state.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    data = p.add_argument_group("data")
    data.add_argument("--symbol", default="BTC/USDT", help="pair, e.g. BTC/USDT")
    data.add_argument(
        "--timeframe", default="1d", choices=["1m", "5m", "15m", "1h", "4h", "1d", "1w"]
    )
    data.add_argument("--start", default=None, help="YYYY-MM-DD (inclusive)")
    data.add_argument("--end", default=None, help="YYYY-MM-DD (inclusive); default now")
    data.add_argument("--provider", default="auto", help="data provider")
    data.add_argument("--csv", default=None, help="read bars from a local CSV (no network)")
    data.add_argument("--refresh", action="store_true", help="ignore the cache and refetch")
    data.add_argument(
        "--bars",
        type=int,
        default=DEFAULT_BARS,
        help="number of recent bars to replay for the fixed-window paper run",
    )
    data.add_argument(
        "--max-stale-seconds",
        type=float,
        default=DEFAULT_MAX_STALE_SECONDS,
        help="fail an automated run when the data is older than this",
    )
    data.add_argument(
        "--max-future-seconds",
        type=float,
        default=DEFAULT_MAX_FUTURE_SECONDS,
        help="fail an automated run when the latest bar ends in the future by more than this",
    )

    money = p.add_argument_group("account (paper)")
    money.add_argument("--cash", type=float, default=10_000.0, help="starting paper equity")
    money.add_argument("--fee-bps", type=float, default=10.0, help="taker fee per side, bps")
    money.add_argument(
        "--slippage-bps", type=float, default=5.0, help="slippage per fill, bps"
    )
    money.add_argument("--risk", type=float, default=0.02, help="equity fraction risked per trade")
    money.add_argument(
        "--max-position", type=float, default=1.0, help="max notional as fraction of equity"
    )
    money.add_argument("--allow-short", action="store_true", help="allow short entries (no funding)")

    strat = p.add_argument_group("strategy: EMA cross + ATR trailing stop")
    strat.add_argument("--fast", type=int, default=20)
    strat.add_argument("--slow", type=int, default=50)
    strat.add_argument("--atr-period", type=int, default=14)
    strat.add_argument("--atr-stop-mult", type=float, default=2.5)
    strat.add_argument("--trail-mult", type=float, default=3.0)
    strat.add_argument("--no-trend-filter", action="store_true", help="trade crosses without the slow-EMA filter")

    out = p.add_argument_group("output")
    out.add_argument(
        "--out",
        default=None,
        help="paper log path (default results/paper/<run_id>.json)",
    )
    out.add_argument("--no-save", action="store_true", help="print only, write nothing")
    out.add_argument(
        "--prune-older-than-days",
        type=float,
        default=DEFAULT_PRUNE_KEEP_DAYS,
        help="after a successful save, delete dated run logs older than this (0 keeps everything)",
    )
    out.add_argument("--json", action="store_true", help="emit the full paper log to stdout")
    out.add_argument("--quiet", action="store_true", help="suppress the data/cache chatter")

    gate = p.add_argument_group("automation gates (first version)")
    gate.add_argument(
        "--paper-only",
        action="store_true",
        default=True,
        help="locked on for this version: never send a real order",
    )
    gate.add_argument(
        "--live-would-run",
        action="store_true",
        help="flag-only probe: refuses and explains the live step instead of running it",
    )
    gate.add_argument(
        "--cron-safe",
        action="store_true",
        default=True,
        help="keep the automated runner quiet enough to call from a scheduler",
    )

    health = p.add_argument_group("scheduler health")
    health.add_argument(
        "--health",
        action="store_true",
        help="emit scheduler health JSON to stdout and exit",
    )
    health.add_argument(
        "--status",
        action="store_true",
        help="print a human summary of the last run/failure, then exit",
    )
    health.add_argument(
        "--health-max-missing-seconds",
        type=float,
        default=DEFAULT_MISSING_RUNS_OK_SECONDS,
        help="treat the bot as unhealthy if no successful run in this long",
    )

    skills = p.add_argument_group("account skills (readonly probe)")
    skills.add_argument(
        "--list-skills",
        action="store_true",
        help="list the account-skill directories found under ~/.config/freebuff-desktop/account-skills",
    )
    skills.add_argument(
        "--skill-read",
        default=None,
        metavar="PATH",
        help="print the contents of a skill file (SKILL.md or any other repo path) to stdout",
    )
    skills.add_argument(
        "--health-force-mode",
        default=None,
        help="placeholder probe for a future manual override of the health verdict",
    )

    return p


def write_last_run(run: PaperRun, exit_code: int) -> LastRun:
    lr = LastRun(
        run_id=run.run_id,
        created_at=run.created_at,
        success=exit_code == 0,
        exit_code=exit_code,
        mode=run.mode,
        paper_only=run.paper_only,
        symbol=run.data["symbol"],
        timeframe=run.data["timeframe"],
        config_hash=_stable_config_json_str(run.config),
        data_end_iso=run.data_end_iso,
        data_end_ms=run.data_end_ms,
        run_timestamp_ms=run.run_timestamp_ms,
        freshness_ok=run.freshness_ok,
        trade_count=run.trade_count,
        exposure_bars=run.exposure_bars,
        stops_hit=run.stops_hit,
        equity_final=run.equity_final,
        total_return_pct=run._total_return_pct(),
        verdict=run._verdict(),
        note="ok",
    )
    LAST_RUN_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAST_RUN_PATH.write_text(json.dumps(_clean(lr.__dict__), indent=2, allow_nan=False), encoding="utf-8")
    return lr


def write_last_failure(run: PaperRun | None, *, exit_code: int, kind: str, reason: str) -> LastFailure:
    lf = LastFailure(
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        exit_code=exit_code,
        kind=kind,
        reason=reason,
        symbol=run.data["symbol"] if run else "",
        timeframe=run.data["timeframe"] if run else "",
        config_hash=_stable_config_json_str(run.config) if run else "",
        data_end_iso=run.data_end_iso if run else "",
        run_id=run.run_id if run else "",
    )
    LAST_FAILURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAST_FAILURE_PATH.write_text(json.dumps(_clean(lf.__dict__), indent=2, allow_nan=False), encoding="utf-8")
    return lf


def _healthy_after_successful_run(args: argparse.Namespace, run: PaperRun, exit_code: int, hard_fail: bool) -> bool:
    """One source of truth for the scheduler-facing healthy verdict.

    This is intentionally stricter than the human-readable report: a run only
    makes the bot healthy when it both (1) exited zero and (2) passed the
    paper guardrails and (3) is not flagged as a scheduler-noticeable hard
    failure.
    """
    if hard_fail:
        return False
    if exit_code != 0:
        return False
    problems = guardrails_ok(run, args)
    if problems:
        return False
    return True


def _run_stamp_ms(path: Path) -> int | None:
    """Return the UTC run time encoded in a paper log filename, if any."""
    match = RUN_STAMP_RE.search(path.name)
    if not match:
        return None
    try:
        stamp = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ")
    except ValueError:
        return None
    return int(stamp.replace(tzinfo=timezone.utc).timestamp() * 1000)


def prune_paper_logs(
    results_dir: Path,
    keep_days: float,
    *,
    protect: Path | None = None,
    now_ms: int | None = None,
) -> list[Path]:
    """Delete dated paper logs older than keep_days, keeping recent history.

    Only files whose name carries a run stamp are considered, so
    last_run.json, last_failure.json, the scheduler log and anything without a
    parseable date are left alone. keep_days <= 0 disables pruning.
    """
    if keep_days <= 0:
        return []
    if now_ms is None:
        now_ms = _now_ms()
    cutoff_ms = now_ms - int(keep_days * 86_400 * 1000)
    protected = protect.resolve() if protect is not None else None

    removed: list[Path] = []
    for path in sorted(results_dir.glob("*.json")):
        if path.name in (LAST_RUN_PATH.name, LAST_FAILURE_PATH.name):
            continue
        if protected is not None and path.resolve() == protected:
            continue
        stamp_ms = _run_stamp_ms(path)
        if stamp_ms is None or stamp_ms >= cutoff_ms:
            continue
        try:
            path.unlink()
        except OSError:
            continue
        removed.append(path)
    return removed


def health_blob(args: argparse.Namespace) -> dict[str, Any]:
    now_ms = _now_ms()
    last_run = _read_last_run()
    last_failure = _read_last_failure()

    verdict = health_verdict(
        last_run.__dict__ if last_run is not None else None,
        float(args.health_max_missing_seconds),
        now_ms,
    )

    body: dict[str, Any] = {
        "healthy": verdict["healthy"],
        "reason": verdict["reason"],
        "checked_at_ms": now_ms,
        "checked_at_iso": datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).isoformat(timespec="seconds"),
        "health_max_missing_seconds": verdict["requested_max_missing_seconds"],
        "effective_health_max_missing_seconds": verdict["effective_max_missing_seconds"],
        "last_run": last_run.__dict__ if last_run else None,
        "last_failure": last_failure.__dict__ if last_failure else None,
    }
    return body


def health_json_text(blob: dict[str, Any]) -> str:
    return json.dumps(blob, indent=2, allow_nan=False)


def _read_last_run() -> LastRun | None:
    if not LAST_RUN_PATH.exists():
        return None
    try:
        d = json.loads(LAST_RUN_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(d, dict):
        return None
    return LastRun(**{k: d.get(k) for k in LastRun.__dataclass_fields__})


def _read_last_failure() -> LastFailure | None:
    if not LAST_FAILURE_PATH.exists():
        return None
    try:
        d = json.loads(LAST_FAILURE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(d, dict):
        return None
    return LastFailure(**{k: d.get(k) for k in LastFailure.__dataclass_fields__})


def _find_skill_dirs() -> list[dict[str, Any]]:
    """Enumerate the account-skill directories that are actually present on disk.

    This is intentionally narrow: it reports the localhost layout of the skill
    bundle the user already synced, not a web search and not a proxy for the
    installed desktop app. It exists so an automated agent can answer "what
    skills are available right now" without hard-coding a path it may not own.
    """
    out: list[dict[str, Any]] = []
    if not SKILL_DIR.is_dir():
        return out
    try:
        for account_dir in sorted(SKILL_DIR.iterdir()):
            if not account_dir.is_dir():
                continue
            try:
                for skill_dir in sorted(account_dir.iterdir()):
                    if not skill_dir.is_dir():
                        continue
                    leaves = sorted(p.name for p in skill_dir.iterdir())
                    skill = {
                        "account_key": account_dir.name,
                        "skill_dir_name": skill_dir.name,
                        "skill_dir": str(skill_dir),
                        "has_skill_md": (skill_dir / "SKILL.md").is_file(),
                        "leaves": leaves,
                    }
                    out.append(skill)
            except OSError:
                continue
    except OSError:
        return out
    return out


def _clean(obj: Any) -> Any:
    if isinstance(obj, float):
        return obj if obj == obj and obj != float("inf") and obj != float("-inf") else None
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    return obj


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.live_would_run:
        print("error: live execution is not wired in this version.", file=sys.stderr)
        print("next step: add a separate paper_trade_live.py behind its own", file=sys.stderr)
        print("paper-only flag, exchange adapter, and human confirmation gates.", file=sys.stderr)
        return 3

    if args.health:
        blob = health_blob(args)
        print(health_json_text(blob))
        return 0 if blob["healthy"] else 1

    if args.status:
        print(status_text(args))
        return 0

    if args.list_skills:
        print(skill_summary_text())
        return 0

    if args.health_force_mode:
        print("error: --health-force-mode is not wired in this version.", file=sys.stderr)
        return 3

    if args.skill_read:
        skill_path = Path(args.skill_read)
        try:
            print(skill_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            print(f"error: skill file not found: {skill_path}", file=sys.stderr)
            return 2
        except OSError as exc:
            print(f"error: cannot read skill file: {exc}", file=sys.stderr)
            return 2
        return 0

    if args.live_would_run:
        print("error: live execution is not wired in this version.", file=sys.stderr)
        print("next step: add a separate paper_trade_live.py behind its own", file=sys.stderr)
        print("paper-only flag, exchange adapter, and human confirmation gates.", file=sys.stderr)
        return 3

    now_ms = _now_ms()

    try:
        run = run_paper_fixed_window(
            args,
            now_ms=now_ms,
            max_stale_seconds=args.max_stale_seconds,
            max_future_seconds=args.max_future_seconds,
        )
    except (DataError, ValueError) as exc:
        write_last_failure(None, exit_code=2, kind="error", reason=str(exc))
        print(f"paper error: {exc}", file=sys.stderr)
        return 2

    problems = guardrails_ok(run, args)
    if problems:
        print("paper guardrails:", file=sys.stderr)
        for line in problems:
            print(f"  - {line}", file=sys.stderr)

    hard_fail = False
    fail_kind = hard_failure_kind(run)
    if fail_kind == "stale_data":
        hard_fail = True
    elif fail_kind == "no_trades":
        hard_fail = True

    if hard_fail:
        write_last_failure(run, exit_code=5, kind=fail_kind or "fail", reason=problems[0] if problems else "hard failure")
        if not args.json:
            print(format_paper_run(run))
            print("paper job failed:", file=sys.stderr)
            for line in problems:
                print(f"  - {line}", file=sys.stderr)
        return 5

    if args.json:
        print(json.dumps(run.to_dict(), indent=2, allow_nan=False))
    else:
        print(format_paper_run(run))
        if problems:
            print("paper guardrail notes:", file=sys.stderr)
            for line in problems:
                print(f"  - {line}", file=sys.stderr)

    if not args.no_save:
        path = Path(args.out) if args.out else RESULTS_DIR / f"{run.run_id}.json"
        if args.json:
            print(f"saved: {path}", file=sys.stderr)
        else:
            print(f"paper log -> {path}")
        run.save_run(path)

    write_last_run(run, exit_code=0)

    if not args.no_save and args.prune_older_than_days > 0:
        removed = prune_paper_logs(
            RESULTS_DIR,
            args.prune_older_than_days,
            protect=Path(args.out).resolve() if args.out else None,
            now_ms=now_ms,
        )
        if removed:
            print(
                f"pruned {len(removed)} paper log(s) older than "
                f"{args.prune_older_than_days:g} day(s)",
                file=sys.stderr,
            )

    return 0


def format_paper_run(run: PaperRun) -> str:
    cfg = run.config
    now_ms = _now_ms()
    age_s = (now_ms - run.data_end_ms) / 1000.0 if run.data_end_ms else None
    future_s = (run.data_end_ms - now_ms) / 1000.0 if run.data_end_ms else None

    top = [
        f"paper run  {run.run_id}",
        f"mode       {run.mode}  (paper_only={run.paper_only})",
        f"strategy   {run.strategy_name}  {run.strategy_params}",
        f"data       {run.data['symbol']} {run.data['timeframe']}  "
        f"{run.data['bars']} bars  {run.data['start']} -> {run.data['end']}  ({run.data['provider']})",
        f"config     cash={cfg.initial_cash:,.0f}  fee={cfg.costs.fee_bps}+"
        f"{cfg.costs.slippage_bps} bps  risk={cfg.risk.risk_per_trade:.2%}  "
        f"maxpos={cfg.risk.max_position_pct:.0%}",
        f"window     {run.bar_count} bars replayed",
        f"result     {run.trade_count} trade(s), {run.exposure_bars} bar(s) exposed, "
        f"{run.stops_hit} stop(s) hit",
        f"equity     {run.equity_final:,.2f}  ({run._total_return_pct():+.2f}%)",
    ]
    if age_s is not None:
        if run.freshness_ok:
            top.append(f"freshness  data age {age_s:.0f}s  ok")
        else:
            top.append(f"freshness  data age {age_s:.0f}s  FAIL ({run.freshness_reason})")
    if future_s is not None and future_s > 0:
        top.append(f"freshness  data ends {future_s:.0f}s in the future")
    top.append("")
    if run.notes:
        top.append("notes")
        for line in run.notes:
            top.append(f"  - {line}")
        top.append("")
    if run.errors:
        top.append("errors")
        for line in run.errors:
            top.append(f"  - {line}")
        top.append("")
    return "\n".join(top)


def status_text(args: argparse.Namespace) -> str:
    blob = health_blob(args)
    requested = blob["health_max_missing_seconds"]
    effective = blob["effective_health_max_missing_seconds"]
    missing_line = f"max_missing_seconds {requested:.0f}"
    if effective != requested:
        missing_line += f"  (effective {effective:.0f} for a slow-bar schedule)"
    lines = [
        "paper bot state",
        f"healthy   {blob['healthy']}",
        f"reason    {blob['reason']}",
        f"checked   {blob['checked_at_iso']}",
        missing_line,
        "",
    ]
    lr = blob.get("last_run")
    if lr:
        lines += [
            "last run",
            f"  id        {lr['run_id']}",
            f"  success   {lr['success']}",
            f"  exit_code {lr['exit_code']}",
            f"  symbol    {lr['symbol']} {lr['timeframe']}",
            f"  config    {lr['config_hash']}",
            f"  data_end  {lr['data_end_iso']}",
            f"  trades    {lr['trade_count']}  exposed {lr['exposure_bars']}  stops {lr['stops_hit']}",
            f"  equity    {lr['equity_final']:,.2f}  ({lr['total_return_pct']:+.2f}%)",
            f"  verdict   {lr['verdict']}",
            "",
        ]
    lf = blob.get("last_failure")
    if lf:
        lines += [
            "last failure",
            f"  created  {lf['created_at']}",
            f"  exit_code {lf['exit_code']}",
            f"  kind     {lf['kind']}",
            f"  reason   {lf['reason']}",
            "",
        ]
    return "\n".join(lines)


def skill_summary_text() -> str:
    rows = _find_skill_dirs()
    if not rows:
        return "no account-skill directories found under ~/.config/freebuff-desktop/account-skills"
    lines = ["account skills on disk (readonly probe)"]
    for r in rows:
        md = "yes" if r["has_skill_md"] else "no"
        leaf_note = ""
        if r["leaves"]:
            leaf_note = f"  contents ({len(r['leaves'])}): {', '.join(r['leaves'])}"
        lines.append(f"  {r['account_key']}/{r['skill_dir_name']:40s}  SKILL.md: {md}{leaf_note}")
    lines.append("")
    lines.append(f"total: {len(rows)} skill directory/directories")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
