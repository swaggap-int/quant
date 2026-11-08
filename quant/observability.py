"""Observability for the live paper loop (Milestone 6).

Separate from the historical dashboard: structured events, measurable
traces, alert evaluation, and a live paper ops view payload. Dependency-free
so the runner and venue can emit without import cycles.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class LogEvent:
    seq: int
    ts_ms: int
    level: str  # debug | info | warn | error
    kind: str  # bar | decision | order | fill | guard_trip | error | retry | health
    message: str
    fields: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dict(seq=self.seq, ts_ms=self.ts_ms, level=self.level,
                    kind=self.kind, message=self.message, fields=dict(self.fields))


class LiveLogger:
    """Append-only structured log with kind/level query."""

    def __init__(self) -> None:
        self._events: list[LogEvent] = []
        self._seq = 0

    def log(self, kind: str, message: str, level: str = "info",
            ts_ms: int | None = None, **fields: Any) -> LogEvent:
        self._seq += 1
        ev = LogEvent(seq=self._seq,
                      ts_ms=int(ts_ms if ts_ms is not None else time.time() * 1000),
                      level=level, kind=kind, message=message, fields=dict(fields))
        self._events.append(ev)
        return ev

    # convenience shorthands
    def bar(self, message: str, **fields: Any) -> LogEvent:
        return self.log("bar", message, level="debug", **fields)

    def decision(self, message: str, **fields: Any) -> LogEvent:
        return self.log("decision", message, **fields)

    def order(self, message: str, **fields: Any) -> LogEvent:
        return self.log("order", message, **fields)

    def fill(self, message: str, **fields: Any) -> LogEvent:
        return self.log("fill", message, **fields)

    def guard_trip(self, message: str, **fields: Any) -> LogEvent:
        return self.log("guard_trip", message, level="warn", **fields)

    def error(self, message: str, **fields: Any) -> LogEvent:
        return self.log("error", message, level="error", **fields)

    def retry(self, message: str, **fields: Any) -> LogEvent:
        return self.log("retry", message, level="warn", **fields)

    def query(self, kind: str | None = None, level: str | None = None) -> list[LogEvent]:
        return [e for e in self._events
                if (kind is None or e.kind == kind) and (level is None or e.level == level)]

    def count(self, kind: str | None = None, level: str | None = None) -> int:
        return len(self.query(kind, level))

    def to_list(self, last_n: int | None = None) -> list[dict[str, Any]]:
        evs = self._events[-last_n:] if last_n else self._events
        return [e.to_dict() for e in evs]


@dataclass
class RunTrace:
    bars_processed: int = 0
    bars_evaluated: int = 0
    bars_skipped: int = 0
    orders_placed: int = 0
    orders_filled: int = 0
    guard_trips: int = 0
    errors: int = 0
    retries: int = 0
    downtime_bars: int = 0
    last_ts: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return dict(bars_processed=self.bars_processed, bars_evaluated=self.bars_evaluated,
                    bars_skipped=self.bars_skipped, orders_placed=self.orders_placed,
                    orders_filled=self.orders_filled, guard_trips=self.guard_trips,
                    errors=self.errors, retries=self.retries,
                    downtime_bars=self.downtime_bars, last_ts=self.last_ts)


def build_trace(outcome: Any, logger: LiveLogger | None = None,
                downtime_bars: int = 0) -> RunTrace:
    """Measurable trace from a run outcome plus optional logger counts."""
    return RunTrace(
        bars_processed=int(getattr(outcome, "bars_seen", 0) or 0),
        bars_evaluated=int(getattr(outcome, "closed_bars_evaluated", 0) or 0),
        bars_skipped=int(getattr(outcome, "open_bars_skipped", 0) or 0),
        orders_placed=int(getattr(outcome, "orders_placed", 0) or 0),
        orders_filled=len(getattr(outcome, "fills", []) or []),
        guard_trips=len(getattr(outcome, "policy_trips", []) or []),
        errors=int(getattr(outcome, "strategy_errors", 0) or 0) + (logger.count("error") if logger else 0),
        retries=logger.count("retry") if logger else 0,
        downtime_bars=downtime_bars,
        last_ts=(list(getattr(outcome, "ts", None) or []) or [None])[-1],
    )


def evaluate_alerts(trace: RunTrace, consistency_problems: list[str] | None = None,
                    *, max_errors: int = 1, max_guard_trips: int = 3,
                    max_downtime_bars: int = 5) -> list[dict[str, Any]]:
    """Alert rules with explicit thresholds; empty means healthy."""
    alerts: list[dict[str, Any]] = []
    if trace.errors >= max_errors and max_errors > 0 and trace.errors > 0:
        alerts.append(dict(name="repeated-errors", severity="page",
                           detail=f"{trace.errors} strategy/loop error(s)"))
    if trace.guard_trips >= max_guard_trips and trace.guard_trips > 0:
        alerts.append(dict(name="guard-trips", severity="warn",
                           detail=f"{trace.guard_trips} guard trip(s)"))
    for p in consistency_problems or []:
        alerts.append(dict(name="state-mismatch", severity="page", detail=p))
    if trace.downtime_bars >= max_downtime_bars and trace.downtime_bars > 0:
        alerts.append(dict(name="process-health", severity="warn",
                           detail=f"{trace.downtime_bars} bar(s) without progress"))
    return alerts


def build_ops_view(*, run_id: str, trace: RunTrace, alerts: list[dict[str, Any]],
                   account: dict[str, Any] | None = None,
                   equity_tail: dict[str, Any] | None = None,
                   policy_trips: list[dict[str, Any]] | None = None,
                   recent_events: list[dict[str, Any]] | None = None,
                   consistency: list[str] | None = None,
                   killed: bool = False) -> dict[str, Any]:
    """Live paper ops payload — deliberately not the historical dashboard shape."""
    health = "halted" if killed else ("degraded" if alerts else "ok")
    return dict(
        mode="live_paper_ops",
        run_id=run_id,
        health=health,
        killed=bool(killed),
        trace=trace.to_dict(),
        alerts=list(alerts),
        account=dict(account or {}),
        equity_tail=dict(equity_tail or {}),
        policy_trips=list(policy_trips or []),
        recent_events=list(recent_events or [])[-50:],
        consistency=list(consistency or []),
    )
