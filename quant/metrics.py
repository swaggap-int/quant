"""Performance statistics computed from an equity curve and a trade list.

Kept deliberately small and explicit: every number here can be re-derived by
hand from the equity curve, which is the only way a backtest deserves trust.
"""

from __future__ import annotations

import math
from typing import Any, Sequence

import numpy as np


def _safe_div(a: float, b: float, default: float = 0.0) -> float:
    return a / b if b else default


def drawdown_series(equity: np.ndarray) -> np.ndarray:
    """Fractional drawdown at each bar (<= 0)."""
    equity = np.asarray(equity, dtype=float)
    if equity.size == 0:
        return equity
    peak = np.maximum.accumulate(equity)
    with np.errstate(invalid="ignore", divide="ignore"):
        dd = np.where(peak > 0, equity / peak - 1.0, 0.0)
    return np.nan_to_num(dd, nan=0.0)


def max_drawdown(equity: np.ndarray) -> float:
    """Deepest peak-to-trough decline as a negative fraction."""
    dd = drawdown_series(equity)
    return float(dd.min()) if dd.size else 0.0


def drawdown_duration_bars(equity: np.ndarray) -> int:
    """Longest number of bars spent below a previous equity peak."""
    equity = np.asarray(equity, dtype=float)
    peak = np.maximum.accumulate(equity)
    longest = run = 0
    for i in range(equity.size):
        if equity[i] < peak[i] - 1e-12:
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    return int(longest)


def returns_from_equity(equity: np.ndarray) -> np.ndarray:
    """Simple period returns of an equity curve."""
    equity = np.asarray(equity, dtype=float)
    if equity.size < 2:
        return np.empty(0)
    prev = equity[:-1]
    with np.errstate(invalid="ignore", divide="ignore"):
        rets = np.where(prev != 0, equity[1:] / prev - 1.0, 0.0)
    return np.nan_to_num(rets, nan=0.0, posinf=0.0, neginf=0.0)


def annualised_return(equity: np.ndarray, years: float) -> float:
    """Compound annual growth rate implied by start and end equity."""
    equity = np.asarray(equity, dtype=float)
    if equity.size < 2 or years <= 0 or equity[0] <= 0:
        return 0.0
    total = equity[-1] / equity[0]
    if total <= 0:
        return -1.0
    return float(total ** (1.0 / years) - 1.0)


def annualised_vol(returns: np.ndarray, periods_per_year: float) -> float:
    returns = np.asarray(returns, dtype=float)
    if returns.size < 2:
        return 0.0
    return float(np.std(returns, ddof=1) * math.sqrt(periods_per_year))


def _is_negligible(value: float, scale: float) -> bool:
    """True when ``value`` is floating-point noise next to ``scale``.

    Dividing by a standard deviation that is meant to be zero but rounds to
    1e-18 produces a Sharpe ratio of 1e17, which is worse than useless.
    """
    return not math.isfinite(value) or value <= 1e-12 * max(abs(scale), 1e-12)


def sharpe(returns: np.ndarray, periods_per_year: float, risk_free: float = 0.0) -> float:
    """Annualised Sharpe ratio on per-bar returns."""
    returns = np.asarray(returns, dtype=float)
    if returns.size < 2:
        return 0.0
    excess = returns - risk_free / periods_per_year
    mean = float(np.mean(excess))
    sd = float(np.std(excess, ddof=1))
    if _is_negligible(sd, mean):
        return 0.0
    return float(mean / sd * math.sqrt(periods_per_year))


def sortino(returns: np.ndarray, periods_per_year: float, risk_free: float = 0.0) -> float:
    """Annualised Sortino ratio: mean return over downside deviation."""
    returns = np.asarray(returns, dtype=float)
    if returns.size < 2:
        return 0.0
    excess = returns - risk_free / periods_per_year
    downside = excess[excess < 0.0]
    if downside.size == 0:
        return 0.0
    mean = float(np.mean(excess))
    dd = float(math.sqrt(np.mean(np.square(downside))))
    if _is_negligible(dd, mean):
        return 0.0
    return float(mean / dd * math.sqrt(periods_per_year))


def compute_metrics(
    equity: np.ndarray,
    ts: np.ndarray,
    *,
    initial_cash: float,
    periods_per_year: float,
    bar_ms: int,
    trades: Sequence[dict[str, Any]] = (),
    fills: Sequence[dict[str, Any]] = (),
    exposure: np.ndarray | None = None,
    benchmark: np.ndarray | None = None,
) -> dict[str, Any]:
    """Summarise a run. All rates are fractions unless suffixed ``_pct``."""
    equity = np.asarray(equity, dtype=float)
    n = equity.size
    years = max(n * bar_ms / (365 * 86_400_000), 1e-9)
    rets = returns_from_equity(equity)
    dd = drawdown_series(equity)
    mdd = float(dd.min()) if dd.size else 0.0
    cagr = annualised_return(equity, years)

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_win = float(sum(t["pnl"] for t in wins))
    gross_loss = float(-sum(t["pnl"] for t in losses))
    fees = float(sum(float(f.get("fee", 0.0)) for f in fills))
    turnover = float(sum(abs(float(f.get("notional", 0.0))) for f in fills))
    avg_equity = float(np.mean(equity)) if n else initial_cash

    metrics: dict[str, Any] = {
        "start_equity": float(equity[0]) if n else initial_cash,
        "final_equity": float(equity[-1]) if n else initial_cash,
        "total_return": float(_safe_div(equity[-1], equity[0], 1.0) - 1.0) if n else 0.0,
        "total_return_pct": float((_safe_div(equity[-1], equity[0], 1.0) - 1.0) * 100.0) if n else 0.0,
        "cagr": cagr,
        "cagr_pct": cagr * 100.0,
        "years": years,
        "bars": int(n),
        "annual_vol": annualised_vol(rets, periods_per_year),
        "sharpe": sharpe(rets, periods_per_year),
        "sortino": sortino(rets, periods_per_year),
        "max_drawdown": mdd,
        "max_drawdown_pct": mdd * 100.0,
        "max_drawdown_bars": drawdown_duration_bars(equity),
        "calmar": float(_safe_div(cagr, abs(mdd))),
        "days_in_drawdown_pct": float(np.mean(dd < -1e-12) * 100.0) if n else 0.0,
    }

    hits = len(wins)
    n_trades = len(trades)
    metrics.update(
        {
            "trades": n_trades,
            "win_rate": float(_safe_div(hits, n_trades)),
            "win_rate_pct": float(_safe_div(hits, n_trades) * 100.0),
            "profit_factor": float(_safe_div(gross_win, gross_loss)) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0),
            "gross_profit": gross_win,
            "gross_loss": gross_loss,
            "net_pnl": gross_win - gross_loss,
            "expectancy": float(_safe_div(gross_win - gross_loss, n_trades)),
            "avg_win": float(_safe_div(gross_win, len(wins))),
            "avg_loss": float(-_safe_div(gross_loss, len(losses))) if losses else 0.0,
            "best_trade": float(max((t["pnl"] for t in trades), default=0.0)),
            "worst_trade": float(min((t["pnl"] for t in trades), default=0.0)),
            "avg_bars_held": float(_safe_div(sum(t.get("bars_held", 0) for t in trades), n_trades)),
            "avg_r_multiple": float(_safe_div(sum(t.get("r_multiple", 0.0) for t in trades), n_trades)),
        }
    )

    metrics.update(
        {
            "fees_paid": fees,
            "fees_pct_of_start": float(_safe_div(fees, initial_cash) * 100.0),
            "turnover_multiple": float(_safe_div(turnover, avg_equity)),
            "bars_in_market_pct": float(np.mean(np.asarray(exposure) > 1e-9) * 100.0) if exposure is not None and len(exposure) else 0.0,
        }
    )

    if benchmark is not None and len(benchmark):
        bench = np.asarray(benchmark, dtype=float)
        metrics["buy_hold_return"] = float(_safe_div(bench[-1], bench[0], 1.0) - 1.0)
        metrics["buy_hold_return_pct"] = float((_safe_div(bench[-1], bench[0], 1.0) - 1.0) * 100.0)
        metrics["buy_hold_cagr"] = annualised_return(bench, years)
        metrics["buy_hold_max_drawdown"] = max_drawdown(bench)
        metrics["excess_return_pct"] = metrics["total_return_pct"] - metrics["buy_hold_return_pct"]
        metrics["excess_cagr_pct"] = (cagr - metrics["buy_hold_cagr"]) * 100.0
    return metrics


def monthly_returns(ts: np.ndarray, equity: np.ndarray) -> list[dict[str, Any]]:
    """Calendar-month returns (a small table the dashboard can show)."""
    if equity.size < 2:
        return []
    from datetime import datetime, timezone

    buckets: dict[str, list[float]] = {}
    for i in range(equity.size):
        key = datetime.fromtimestamp(int(ts[i]) / 1000, tz=timezone.utc).strftime("%Y-%m")
        buckets.setdefault(key, []).append(float(equity[i]))
    months = sorted(buckets)
    out: list[dict[str, Any]] = []
    prev_close = None
    for key in months:
        closes = buckets[key]
        first, last = closes[0], closes[-1]
        base = prev_close if prev_close is not None else first
        out.append({"month": key, "return_pct": float((last / base - 1.0) * 100.0) if base else 0.0})
        prev_close = last
    return out


def largest_losses(trades: Sequence[dict[str, Any]], *, k: int = 5) -> list[dict[str, Any]]:
    """The worst realized trades, for an at-a-glance fragility read."""
    neg = [t for t in trades if float(t.get("pnl", 0.0)) <= 0]
    neg.sort(key=lambda t: float(t.get("pnl", 0.0)))
    return [
        {
            "side": t.get("side"),
            "entry_ts": t.get("entry_ts"),
            "exit_ts": t.get("exit_ts"),
            "entry_price": float(t.get("entry_price", 0.0)),
            "exit_price": float(t.get("exit_price", 0.0)),
            "pnl": float(t.get("pnl", 0.0)),
            "pnl_pct": float(t.get("return_pct", 0.0)),
            "bars_held": int(t.get("bars_held", 0)),
            "exit_reason": t.get("exit_reason"),
        }
        for t in neg[:k]
    ]


def concentration_read(trades: Sequence[dict[str, Any]], *, top_k: int = 3) -> dict[str, Any]:
    """A small structural read of whether the result leans on a few trades.

    Returns the share of net PnL explained by the best ``top_k`` winning trades
    and the share explained by the worst ``top_k`` losing trades. If the result is
    carried by one or two outliers, that is worth saying plainly.
    """
    wins = sorted([t for t in trades if float(t.get("pnl", 0.0)) > 0], key=lambda t: float(t.get("pnl", 0.0)), reverse=True)
    losses = sorted([t for t in trades if float(t.get("pnl", 0.0)) <= 0], key=lambda t: float(t.get("pnl", 0.0)))
    gross_profit = float(sum(float(t.get("pnl", 0.0)) for t in wins))
    gross_loss = float(sum(float(t.get("pnl", 0.0)) for t in losses))
    net = gross_profit + gross_loss
    best_share = float(_safe_div(sum(float(t.get("pnl", 0.0)) for t in wins[:top_k]), net)) if net else 0.0
    worst_share = float(_safe_div(sum(float(t.get("pnl", 0.0)) for t in losses[:top_k]), net)) if net else 0.0
    if net < 0:
        best_share, worst_share = -worst_share, -best_share
    return {
        "top_k": top_k,
        "top_wins_share": best_share,
        "top_losses_share": worst_share,
        "winners_total": gross_profit,
        "losers_total": gross_loss,
        "net": net,
        "n_wins": len(wins),
        "n_losses": len(losses),
    }
