"""Bollinger-band mean reversion: buy a pullback that starts to recover.

This is deliberately not another breakout. The entry waits for price to close
back above the lower Bollinger band after closing below it, and only takes the
trade while price is above a slow trend filter. The exit is the middle band
(the mean) or the ATR stop, whichever comes first.

The family is long-only and uses the same execution contract as the trend
strategies: closed-bar signal, next-open fill, ATR-sized position, fees and
slippage charged by the engine.
"""

from __future__ import annotations

import numpy as np

from quant.broker import Order
from quant.datafeed import Candles
from quant.indicators import atr, ema, rolling_std, sma
from quant.strategies.base import BarWindow, Strategy


class BollingerReversion(Strategy):
    """Long-only lower-band reversion with a slow trend filter and ATR stop."""

    name = "bollinger-reversion"

    def __init__(
        self,
        period: int = 20,
        entry_k: float = 2.0,
        trend_period: int = 200,
        atr_period: int = 14,
        atr_stop_mult: float = 2.5,
        risk_pct: float = 0.02,
        max_weight: float = 1.0,
        trend_filter: bool = True,
    ) -> None:
        if period < 2:
            raise ValueError("period must be at least 2")
        if entry_k <= 0:
            raise ValueError("entry_k must be positive")
        if trend_period < 1:
            raise ValueError("trend_period must be positive")
        if atr_stop_mult <= 0:
            raise ValueError("atr_stop_mult must be positive")
        if risk_pct <= 0:
            raise ValueError("risk_pct must be positive")
        self.period = int(period)
        self.entry_k = float(entry_k)
        self.trend_period = int(trend_period)
        self.atr_period = int(atr_period)
        self.atr_stop_mult = float(atr_stop_mult)
        self.risk_pct = float(risk_pct)
        self.max_weight = float(max_weight)
        self.trend_filter = bool(trend_filter)
        self.windows = {"bb": self.period, "trend": self.trend_period, "atr": self.atr_period}

    def prepare(self, candles: Candles) -> dict[str, np.ndarray]:
        close = candles.close
        middle = sma(close, self.period)
        spread = rolling_std(close, self.period)
        lower = middle - self.entry_k * spread
        trend = ema(close, self.trend_period)
        return {
            "middle": middle,
            "lower": lower,
            "trend": trend,
            "atr": atr(candles.high, candles.low, close, self.atr_period),
        }

    def on_bar(self, w: BarWindow, portfolio: dict[str, object]) -> list[Order]:
        close = w.last(w.close)
        middle = w.last(w.ind("middle"))
        lower = w.last(w.ind("lower"))
        trend = w.last(w.ind("trend"))
        atr_now = w.last(w.ind("atr"))
        if any(np.isnan(x) for x in (close, middle, lower, trend, atr_now)) or atr_now <= 0:
            return []
        if w.i < max(self.windows.values()) + 1:
            return []

        if portfolio["position"] == "flat":
            if self.trend_filter and close <= trend:
                return []
            previous_close = w.last(w.close, 1)
            previous_lower = w.last(w.ind("lower"), 1)
            if np.isnan(previous_close) or np.isnan(previous_lower):
                return []
            # The bar before the signal was below the band; this bar closes
            # back above it, which is the first evidence of mean reversion.
            if previous_close <= previous_lower and close > lower:
                stop = close - self.atr_stop_mult * atr_now
                if stop <= 0:
                    return []
                return [
                    Order(
                        action="buy",
                        risk_pct=self.risk_pct,
                        weight=self.max_weight,
                        stop=stop,
                        reason=f"bollinger{self.period} reclaim",
                    )
                ]
            return []

        if close >= middle:
            return [Order(action="sell", reason="bollinger mean exit")]
        return []
