"""Strategy interface and the window object strategies are handed each bar.

``BarWindow`` is the only way a strategy sees price data, and it is physically
truncated at the current bar. You cannot read tomorrow's close because the
array you are given does not contain it — no discipline required.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import numpy as np

from quant.broker import Order
from quant.datafeed import Candles


class BarWindow:
    """Read-only slice of history from bar 0 through the current bar.

    ``ts``/``open``/``high``/``low``/``close``/``volume`` are numpy views of the
    underlying arrays, so slicing costs nothing. ``ind(name)`` returns the same
    truncation for a precomputed indicator column.
    """

    __slots__ = ("i", "_ts", "_open", "_high", "_low", "_close", "_volume", "_ind")

    def __init__(self, candles: Candles, indicators: dict[str, np.ndarray], i: int) -> None:
        self.i = i
        self._ts = candles.ts
        self._open = candles.open
        self._high = candles.high
        self._low = candles.low
        self._close = candles.close
        self._volume = candles.volume
        self._ind = indicators

    def __len__(self) -> int:
        return self.i + 1

    @property
    def ts(self) -> np.ndarray:
        return self._ts[: self.i + 1]

    @property
    def open(self) -> np.ndarray:
        return self._open[: self.i + 1]

    @property
    def high(self) -> np.ndarray:
        return self._high[: self.i + 1]

    @property
    def low(self) -> np.ndarray:
        return self._low[: self.i + 1]

    @property
    def close(self) -> np.ndarray:
        return self._close[: self.i + 1]

    @property
    def volume(self) -> np.ndarray:
        return self._volume[: self.i + 1]

    @property
    def now(self) -> int:
        return int(self._ts[self.i])

    def ind(self, name: str) -> np.ndarray:
        try:
            series = self._ind[name]
        except KeyError:
            raise KeyError(f"unknown indicator {name!r}; available: {', '.join(self._ind)}") from None
        return series[: self.i + 1]

    def bar_inputs(self, name: str) -> np.ndarray:
        """Indicator computed on a raw price column (``open/high/low/close``)."""
        series = np.asarray(getattr(self, name), dtype=float)
        return series

    def last(self, arr: np.ndarray, offset: int = 0) -> float:
        """Value ``offset`` bars back (0 = current bar); NaN if not ready."""
        idx = self.i - offset
        if idx < 0 or idx >= arr.size:
            return float("nan")
        return float(arr[idx])


class Strategy(ABC):
    """Base class for a signal generator.

    Lifecycle: ``prepare()`` once over the full series, then ``on_bar()`` per
    bar. ``prepare`` may compute indicators over the whole array because those
    are causal by construction; ``on_bar`` must go through ``BarWindow``.
    """

    name = "strategy"

    #: Named rolling windows the strategy needs; drives warmup length.
    windows: dict[str, int] = {}

    def reset(self) -> None:
        """Clear per-run state. Called by the engine before a fresh run."""

    def prepare(self, candles: Candles) -> dict[str, np.ndarray]:
        """Precompute indicator columns. Default: nothing extra."""
        return {}

    @abstractmethod
    def on_bar(self, w: BarWindow, portfolio: dict[str, Any]) -> list[Order]:
        """Return orders to execute at the next bar's open."""

    def warmup_bars(self) -> int:
        """Bars to skip before trading; longest window plus one for crossovers."""
        return (max(self.windows.values()) if self.windows else 0) + 2

    def describe(self) -> dict[str, Any]:
        """Parameters as JSON-safe data, so every result file stays loadable."""
        params = {
            k: _jsonable(v)
            for k, v in self.__dict__.items()
            if not k.startswith("_") and not callable(v)
        }
        return {"name": self.name, "params": params, "windows": dict(self.windows)}


def _jsonable(value: Any) -> Any:
    """Coerce arbitrary strategy state into something ``json`` can write."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return repr(value)
