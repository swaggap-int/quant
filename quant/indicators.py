"""Technical indicators on plain numpy arrays.

Every function returns an array the same length as its input, left-padded with
``NaN`` for the warmup period. Nothing here looks forward: the value at index
``i`` depends only on indices ``<= i``.

Wilder's smoothing is used for ATR and RSI (the original definitions), not a
plain exponential moving average, because the two differ enough to matter when
you size positions off ATR.
"""

from __future__ import annotations

import numpy as np

Array = np.ndarray


def _as_float(x: Array | list[float]) -> Array:
    return np.asarray(x, dtype=float)


def sma(values: Array, period: int) -> Array:
    """Simple moving average."""
    values = _as_float(values)
    if period < 1:
        raise ValueError("period must be >= 1")
    out = np.full(values.shape, np.nan, dtype=float)
    if values.size < period:
        return out
    # Cumulative-sum trick, then subtract; cheap and exact enough for prices.
    csum = np.cumsum(np.insert(values, 0, 0.0))
    out[period - 1 :] = (csum[period:] - csum[:-period]) / period
    return out


def ema(values: Array, period: int) -> Array:
    """Exponential moving average, seeded with the first ``period`` SMA."""
    values = _as_float(values)
    if period < 1:
        raise ValueError("period must be >= 1")
    out = np.full(values.shape, np.nan, dtype=float)
    if values.size < period:
        return out
    alpha = 2.0 / (period + 1.0)
    prev = float(np.mean(values[:period]))
    out[period - 1] = prev
    for i in range(period, values.size):
        prev = alpha * float(values[i]) + (1.0 - alpha) * prev
        out[i] = prev
    return out


def rma(values: Array, period: int) -> Array:
    """Wilder's smoothing (a.k.a. RMA / SMMA): EMA with alpha = 1/period."""
    values = _as_float(values)
    if period < 1:
        raise ValueError("period must be >= 1")
    out = np.full(values.shape, np.nan, dtype=float)
    if values.size < period:
        return out
    prev = float(np.mean(values[:period]))
    out[period - 1] = prev
    for i in range(period, values.size):
        prev = (prev * (period - 1) + float(values[i])) / period
        out[i] = prev
    return out


def true_range(high: Array, low: Array, close: Array) -> Array:
    """True range; index 0 is high - low since there is no prior close."""
    high, low, close = _as_float(high), _as_float(low), _as_float(close)
    if not (high.shape == low.shape == close.shape):
        raise ValueError("high, low and close must have the same shape")
    out = np.empty(high.shape, dtype=float)
    if high.size == 0:
        return out
    out[0] = high[0] - low[0]
    if high.size > 1:
        prev_close = close[:-1]
        out[1:] = np.maximum.reduce(
            [high[1:] - low[1:], np.abs(high[1:] - prev_close), np.abs(low[1:] - prev_close)]
        )
    return out


def atr(high: Array, low: Array, close: Array, period: int = 14) -> Array:
    """Average true range using Wilder's smoothing."""
    return rma(true_range(high, low, close), period)


def rsi(close: Array, period: int = 14) -> Array:
    """Relative strength index (Wilder)."""
    close = _as_float(close)
    out = np.full(close.shape, np.nan, dtype=float)
    if close.size < period + 1:
        return out
    delta = np.diff(close)
    gain = np.clip(delta, 0.0, None)
    loss = np.clip(-delta, 0.0, None)
    avg_gain = rma(gain, period)
    avg_loss = rma(loss, period)
    # avg_* are indexed one behind close because diff() shortens the series.
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = np.where(avg_loss == 0.0, np.inf, avg_gain / avg_loss)
        rsi_vals = 100.0 - (100.0 / (1.0 + rs))
    rsi_vals = np.where((avg_loss == 0.0) & (avg_gain == 0.0), 50.0, rsi_vals)
    out[period:] = rsi_vals[period - 1 :]
    return out


def rolling_std(values: Array, period: int, ddof: int = 1) -> Array:
    """Rolling sample standard deviation."""
    values = _as_float(values)
    out = np.full(values.shape, np.nan, dtype=float)
    if values.size < period or period < 2:
        return out
    for i in range(period - 1, values.size):
        out[i] = float(np.std(values[i - period + 1 : i + 1], ddof=ddof))
    return out


def donchian(high: Array, low: Array, period: int) -> tuple[Array, Array]:
    """Rolling maximum high and minimum low over ``period`` bars."""
    high, low = _as_float(high), _as_float(low)
    upper = np.full(high.shape, np.nan, dtype=float)
    lower = np.full(low.shape, np.nan, dtype=float)
    for i in range(period - 1, high.size):
        upper[i] = float(np.max(high[i - period + 1 : i + 1]))
        lower[i] = float(np.min(low[i - period + 1 : i + 1]))
    return upper, lower


def highest(values: Array, period: int) -> Array:
    upper, _ = donchian(values, values, period)
    return upper


def lowest(values: Array, period: int) -> Array:
    _, lower = donchian(values, values, period)
    return lower
