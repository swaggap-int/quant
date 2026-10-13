"""The event loop: one pass over the bars, no revisiting, no peeking ahead.

Bar-by-bar order of operations, which is the whole ballgame:

1. Orders queued at the previous close fill at this bar's **open**.
2. The open position's excursion (how far for/against) is updated on this bar.
3. A resting stop is tested against this bar's low/high; gaps fill at the open.
4. Equity is marked at this bar's **close**.
5. The strategy sees a window ending at this bar and may queue orders for the
   next open. Orders still queued after the final bar never fill.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from quant.broker import Broker
from quant.config import BacktestConfig
from quant.datafeed import Candles, load_candles
from quant.metrics import compute_metrics, drawdown_series, monthly_returns
from quant.strategies.base import BarWindow, Strategy

#: How many equity points to keep in the result JSON for the dashboard.
DEFAULT_JSON_POINTS = 1500

HONESTY_NOTES = (
    "Fills assume taker pricing on every order; no maker rebates, no partial fills.",
    "Stops assume the bar's low/high was reachable; gaps fill at the open and slip further.",
    "Backtests cannot see your leverage limits, exchange outages or your own hesitation.",
)


@dataclass
class BacktestResult:
    """Everything a run produced, ready to serialise or inspect."""

    run_id: str
    created_at: str
    config: BacktestConfig
    strategy: dict[str, Any]
    candles: Candles
    equity: np.ndarray
    cash: np.ndarray
    exposure: np.ndarray
    benchmark: np.ndarray
    drawdown: np.ndarray
    trades: list[dict[str, Any]]
    fills: list[dict[str, Any]]
    notes: list[str]
    metrics: dict[str, Any]
    monthly: list[dict[str, Any]] = field(default_factory=list)

    def __str__(self) -> str:
        from quant.report import format_report

        return format_report(self)

    # -- serialisation ----------------------------------------------------- #

    def equity_series(self, max_points: int = DEFAULT_JSON_POINTS) -> dict[str, list[float]]:
        """Equity, benchmark and drawdown, thinned to at most ``max_points``."""
        n = len(self.equity)
        if n <= max_points:
            idx = np.arange(n)
        else:
            idx = np.unique(np.linspace(0, n - 1, max_points).astype(int))
        return {
            "ts": [int(self.candles.ts[i]) for i in idx],
            "equity": [float(self.equity[i]) for i in idx],
            "benchmark": [float(self.benchmark[i]) for i in idx],
            "drawdown_pct": [float(self.drawdown[i] * 100.0) for i in idx],
            "close": [float(self.candles.close[i]) for i in idx],
        }

    def to_dict(self, *, max_points: int = DEFAULT_JSON_POINTS, include_fills: bool = True) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "created_at": self.created_at,
            "config": self.config.to_dict(),
            "strategy": self.strategy,
            "data": {
                "symbol": self.candles.symbol,
                "timeframe": self.candles.timeframe,
                "provider": self.candles.provider,
                "bars": len(self.candles),
                "start": self.candles.start_iso,
                "end": self.candles.end_iso,
            },
            "metrics": self.metrics,
            "monthly": self.monthly,
            "series": self.equity_series(max_points),
            "trades": self.trades,
            "fills": self.fills if include_fills else [],
            "notes": self.notes,
        }

    def save(self, path: str | Path, *, max_points: int = DEFAULT_JSON_POINTS) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(max_points=max_points), indent=2, allow_nan=False), encoding="utf-8")
        return path


class Engine:
    """Runs one strategy over one set of candles."""

    def __init__(self, config: BacktestConfig, strategy: Strategy) -> None:
        self.config = config
        self.strategy = strategy

    def run(self, candles: Candles | None = None) -> BacktestResult:
        cfg = self.config
        if candles is None:
            candles = load_candles(
                cfg.symbol,
                cfg.timeframe,
                cfg.start,
                cfg.end,
                provider=cfg.provider,
            )
        if len(candles) < 3:
            raise ValueError(f"need at least 3 bars to backtest, got {len(candles)}")

        self.strategy.reset()
        indicators = self.strategy.prepare(candles)
        warmup = min(self.strategy.warmup_bars(), max(len(candles) - 2, 0))

        broker = Broker(
            initial_cash=cfg.initial_cash,
            costs=cfg.costs,
            risk=cfg.risk,
            bar_ms=cfg.bar_ms,
        )

        n = len(candles)
        equity = np.empty(n, dtype=float)
        cash = np.empty(n, dtype=float)
        exposure = np.zeros(n, dtype=float)

        for i in range(n):
            ts = int(candles.ts[i])
            o, h, l, c = (float(candles.open[i]), float(candles.high[i]),
                          float(candles.low[i]), float(candles.close[i]))

            broker.process_open(ts, o, h, l)
            broker.update_excursion(h, l)
            broker.check_stop(ts, o, h, l)

            eq = broker.equity(c)
            equity[i] = eq
            cash[i] = broker.cash
            exposure[i] = broker.position_value(c) / eq if eq > 0 else 0.0

            if i >= warmup:
                window = BarWindow(candles, indicators, i)
                for order in self.strategy.on_bar(window, broker.snapshot(c)):
                    broker.submit(order)

        benchmark = cfg.initial_cash * candles.close / float(candles.close[0])
        dd = drawdown_series(equity)

        notes = list(HONESTY_NOTES)
        for reason, count in sorted(broker.rejections.items()):
            notes.append(f"Skipped {count} order(s): {reason}.")
        if broker.pending:
            notes.append(f"{len(broker.pending)} order(s) queued on the final bar never filled.")
        if not broker.position.is_flat:
            pos = broker.position
            notes.append(
                f"Position still open at the end ({pos.side} {abs(pos.qty):.6f} @ {pos.entry_price:,.2f}); "
                "its unrealised PnL sits in the final equity figure but not in the trade list."
            )

        metrics = compute_metrics(
            equity,
            candles.ts,
            initial_cash=cfg.initial_cash,
            periods_per_year=cfg.periods_per_year,
            bar_ms=cfg.bar_ms,
            trades=broker.trades,
            fills=broker.fills,
            exposure=exposure,
            benchmark=benchmark,
        )
        slug = candles.symbol.replace("/", "").replace("-", "").upper()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return BacktestResult(
            run_id=f"{slug}_{candles.timeframe}_{self.strategy.name}_{stamp}",
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            config=cfg,
            strategy=self.strategy.describe(),
            candles=candles,
            equity=equity,
            cash=cash,
            exposure=exposure,
            benchmark=benchmark,
            drawdown=dd,
            trades=broker.trades,
            fills=broker.fills,
            notes=notes,
            metrics=metrics,
            monthly=monthly_returns(candles.ts, equity),
        )
