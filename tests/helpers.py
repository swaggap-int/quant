"""Shared test helpers: synthetic candles and a scripted strategy."""

from __future__ import annotations

from typing import Any, Iterable, Sequence

import numpy as np

from quant.broker import Order
from quant.datafeed import Candles
from quant.strategies.base import BarWindow, Strategy

DAY_MS = 86_400_000
START_MS = 1_600_000_000_000  # 2020-09-13, a Sunday, for no particular reason


def candles_from(
    rows: Sequence[Sequence[float]],
    symbol: str = "TEST/USDT",
    timeframe: str = "1d",
    start_ms: int = START_MS,
) -> Candles:
    """Build candles from ``(open, high, low, close, volume)`` tuples."""
    n = len(rows)
    if n == 0:
        empty = np.empty(0, dtype=float)
        return Candles(
            ts=np.empty(0, dtype=np.int64),
            open=empty.copy(), high=empty.copy(), low=empty.copy(),
            close=empty.copy(), volume=empty.copy(),
            symbol=symbol, timeframe=timeframe, provider="test",
        )
    data = np.asarray(rows, dtype=float)
    return Candles(
        ts=(start_ms + np.arange(n, dtype=np.int64) * DAY_MS),
        open=data[:, 0].copy(),
        high=data[:, 1].copy(),
        low=data[:, 2].copy(),
        close=data[:, 3].copy(),
        volume=data[:, 4].copy() if data.shape[1] > 4 else np.ones(n, dtype=float),
        symbol=symbol,
        timeframe=timeframe,
        provider="test",
    )


def flat_bars(n: int, price: float = 100.0, spread: float = 1.0) -> list[tuple[float, float, float, float, float]]:
    """``n`` identical bars around ``price``, range never touching a stop."""
    return [(price, price + spread, price - spread, price, 1.0) for _ in range(n)]


class ScriptedStrategy(Strategy):
    """Emits pre-scripted orders on specific bar indices. For engine tests."""

    name = "scripted"

    def __init__(self, script: dict[int, Iterable[Order]] | None = None, windows: dict[str, int] | None = None) -> None:
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.windows = windows or {}
        self.reset()

    def reset(self) -> None:
        self.seen: list[int] = []
        self.portfolio_views: list[dict[str, Any]] = []

    def prepare(self, candles: Candles) -> dict[str, np.ndarray]:
        return {}

    def on_bar(self, w: BarWindow, portfolio: dict[str, Any]) -> list[Order]:
        self.seen.append(w.i)
        self.portfolio_views.append(dict(portfolio))
        return list(self.script.get(w.i, []))
