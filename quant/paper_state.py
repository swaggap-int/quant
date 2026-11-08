"""The paper bot's health rule, shared by the CLI and the dashboard.

``paper_trade.py --health`` and the dashboard's paper card must never disagree
about whether the scheduler is alive, so the rule lives here once instead of
being copied into each reader.

Two windows decide it:

* the requested ``--health-max-missing-seconds`` (one hour by default), and
* a floor of 1.5 bars for the run's timeframe, because a daily-bar bot can only
  be expected to run about once a day. The floor can only widen the window,
  never tighten it, so intraday runs stay as strict as before.
"""

from __future__ import annotations

from typing import Any, Mapping

DEFAULT_MISSING_RUNS_OK_SECONDS = 3600.0

BAR_MS_BY_TIMEFRAME = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 21_600_000,
    "1d": 86_400_000,
    "1w": 604_800_000,
}


def cadence_floor_seconds(timeframe: str) -> float:
    """Minimum health window a timeframe can justify (1.5 bars)."""
    bar_ms = BAR_MS_BY_TIMEFRAME.get(timeframe or "")
    if not bar_ms:
        return 0.0
    return 1.5 * bar_ms / 1000.0


def effective_missing_seconds(timeframe: str, requested: float) -> float:
    """The requested window, widened to the timeframe's cadence floor."""
    return max(float(requested), cadence_floor_seconds(timeframe))


def health_verdict(
    last_run: Mapping[str, Any] | None,
    requested_missing: float,
    now_ms: int,
) -> dict[str, Any]:
    """Decide whether the paper bot is healthy from one last-run record.

    ``last_run`` is the decoded ``last_run.json`` (the runner passes its
    dataclass ``__dict__``, the dashboard passes the parsed file), so both
    readers evaluate exactly the same fields.
    """
    requested = float(requested_missing)
    effective = requested
    if last_run is not None:
        effective = effective_missing_seconds(str(last_run.get("timeframe") or ""), requested)

    healthy = False
    if last_run is None:
        reason = "no successful run yet"
    else:
        try:
            run_ms = int(last_run.get("run_timestamp_ms") or 0)
        except (TypeError, ValueError):
            run_ms = 0
        age_s = (now_ms - run_ms) / 1000.0
        success = bool(last_run.get("success"))
        fresh = bool(last_run.get("freshness_ok"))
        if age_s > effective:
            reason = f"no successful run in {age_s:.0f}s (max {effective:.0f}s)"
        elif success and fresh:
            healthy = True
            reason = "last run successful and data fresh"
        elif success:
            reason = "last run succeeded but data was stale"
        else:
            reason = f"last run failed with exit {last_run.get('exit_code')}"

    return {
        "healthy": healthy,
        "reason": reason,
        "requested_max_missing_seconds": requested,
        "effective_max_missing_seconds": effective,
    }
