"""
Live-clock utilities for the simulated-live runner.

This module is intentionally framework-free at the boundary:
BarClock is pure time arithmetic, BarClosedPolicy is a tiny guard,
and CandleStreamAdapter turns a Candles object into a one-bar-at-a-time
stream. make_bar_closed_stream wires them together for the live path.
"""

import dataclasses
from typing import Iterator, Optional, Sequence

from quant.datafeed import Candles


@dataclasses.dataclass(frozen=True)
class BarBoundary:
    index: int
    start_ms: int
    end_ms: int


class BarClock:
    """
    Pure time helpers for a single candle timeframe.

    It knows nothing about strategies or markets; it only answers
    "where are the bar boundaries around this timestamp?".
    """

    def __init__(self, timeframe: str, bar_ms: int, t0: int = 0) -> None:
        self.timeframe = timeframe
        self.bar_ms = int(bar_ms)
        self.t0 = int(t0)  # open timestamp of bar 0; anchors index math to real time

    def bar_start_ms(self, index: int) -> int:
        return self.t0 + index * self.bar_ms

    def bar_end_ms(self, index: int) -> int:
        return self.t0 + (index + 1) * self.bar_ms

    def bar_start_ts(self, index: int) -> int:
        return int(self.bar_start_ms(index))

    def bar_end_ts(self, index: int) -> int:
        return int(self.bar_end_ms(index))

    def is_bar_closed_at(self, index: int, now_ms: int) -> bool:
        return now_ms >= self.bar_end_ms(index)

    def current_bar_index_for(self, now_ms: int) -> int:
        if now_ms < self.t0:
            return -1
        return (int(now_ms) - self.t0) // self.bar_ms

    def bar_boundary(self, index: int) -> BarBoundary:
        return BarBoundary(index=index, start_ms=self.bar_start_ms(index), end_ms=self.bar_end_ms(index))


class BarClosedPolicy:
    """
    Simple guard for the live path.

    The default stance is conservative: do not evaluate a strategy on a
    bar that is not yet closed. This keeps the simulated-live path honest
    about what it means to "act on a bar".
    """

    def __init__(self, allow_open_bar_trading: bool = False) -> None:
        self.allow_open_bar_trading = bool(allow_open_bar_trading)

    def is_safe_to_evaluate(self, bar_closed: bool) -> bool:
        if self.allow_open_bar_trading:
            return True
        return bool(bar_closed)


@dataclasses.dataclass
class LiveBar:
    index: int
    ts: int
    open_: float
    close_: float
    high: float
    low: float
    volume: float
    bar_closed: bool

    def to_bar_view(self, indicators: dict[str, Sequence[float]] | None = None) -> dict[str, object]:
        return dict(
            index=self.index,
            ts=self.ts,
            open_=self.open_,
            close_=self.close_,
            high=self.high,
            low=self.low,
            volume=self.volume,
            bar_closed=self.bar_closed,
            indicators=(indicators if indicators is not None else {}),
        )


class CandleStreamAdapter:
    """
    Expose a Candles object as a one-bar-at-a-time stream.

    This is the simplest possible adapter: it walks the historical candles
    in order and reports each one once, including whether the bar is already
    closed at stream creation time.
    """

    def __init__(
        self,
        candles: Candles,
        now_ms: Optional[int] = None,
        *,
        bar_closed_policy: Optional[BarClosedPolicy] = None,
    ) -> None:
        self._candles = candles
        t0 = int(candles.ts[0]) if len(candles) else 0
        self._clock = BarClock(timeframe=candles.timeframe, bar_ms=candles.bar_ms, t0=t0)
        if now_ms is None:
            now_ms = int(self._clock.bar_end_ts(len(candles) - 1))
        self._now_ms = int(now_ms)
        self._policy = bar_closed_policy or BarClosedPolicy()
        self._position: int = 0

    def __bool__(self) -> bool:
        return self._position < len(self._candles)

    def __len__(self) -> int:
        return max(0, len(self._candles) - self._position)

    def peek(self) -> LiveBar | None:
        if not self:
            return None
        return self._build_bar(self._position)

    def advance(self) -> LiveBar | None:
        if not self:
            return None
        bar = self._build_bar(self._position)
        self._position += 1
        return bar

    def _build_bar(self, index: int) -> LiveBar:
        c = self._candles
        bar_closed = self._clock.is_bar_closed_at(index, self._now_ms)
        return LiveBar(
            index=index,
            ts=int(c.ts[index]),
            open_=float(c.open[index]),
            close_=float(c.close[index]),
            high=float(c.high[index]),
            low=float(c.low[index]),
            volume=float(c.volume[index]),
            bar_closed=bar_closed,
        )


def make_bar_closed_stream(
    candles: Candles,
    now_ms: Optional[int] = None,
    *,
    bar_closed_policy: Optional[BarClosedPolicy] = None,
) -> CandleStreamAdapter:
    """
    Wire a historical candles set into a bar-closed-aware stream.

    This is the main entry point for Milestone 2: it gives the live runner a
    stream whose bars report whether they are closed at the chosen "now",
    and the policy keeps the runner from acting on an open bar unless it opts
    in explicitly.
    """
    return CandleStreamAdapter(
        candles=candles,
        now_ms=now_ms,
        bar_closed_policy=bar_closed_policy,
    )
