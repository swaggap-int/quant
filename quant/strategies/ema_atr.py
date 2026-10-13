"""A trend-following baseline: EMA cross entry, ATR trailing stop.

The rules, in the order they are evaluated on each bar close:

1. Trend filter — only go long when close is above the slow EMA.
2. Entry — the fast EMA crosses above the slow EMA.
3. Initial stop — entry minus ``atr_stop_mult`` x ATR(14).
4. Size — risk ``risk_pct`` of equity to that stop.
5. Trail — the stop ratchets up to ``high_since_entry - trail_mult`` x ATR and
   never moves down.
6. Exit — fast EMA crosses back below the slow EMA, or the stop is hit.

This is the honest version of the strategy most "quant algorithm" tutorials
show: it pays fees and slippage, sizes off volatility, and cannot see the
future.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from quant.broker import Order
from quant.datafeed import Candles
from quant.indicators import atr, ema
from quant.strategies.base import BarWindow, Strategy


class EmaAtrTrend(Strategy):
    """Long-only EMA crossover with a volatility-scaled trailing stop."""

    name = "ema-atr-trend"

    def __init__(
        self,
        fast: int = 20,
        slow: int = 50,
        atr_period: int = 14,
        atr_stop_mult: float = 2.5,
        trail_mult: float = 3.0,
        risk_pct: float = 0.02,
        max_weight: float = 1.0,
        trend_filter: bool = True,
    ) -> None:
        if fast >= slow:
            raise ValueError("fast EMA must be shorter than slow EMA")
        if risk_pct <= 0:
            raise ValueError("risk_pct must be positive")
        self.fast = fast
        self.slow = slow
        self.atr_period = atr_period
        self.atr_stop_mult = atr_stop_mult
        self.trail_mult = trail_mult
        self.risk_pct = risk_pct
        self.max_weight = max_weight
        self.trend_filter = trend_filter
        self.windows = {"ema_fast": fast, "ema_slow": slow, "atr": atr_period}
        self.reset()

    def reset(self) -> None:
        self._peak_high = float("nan")

    def prepare(self, candles: Candles) -> dict[str, np.ndarray]:
        return {
            "ema_fast": ema(candles.close, self.fast),
            "ema_slow": ema(candles.close, self.slow),
            "atr": atr(candles.high, candles.low, candles.close, self.atr_period),
        }

    # -- helpers ----------------------------------------------------------- #

    def _crossed_up(self, w: BarWindow) -> bool:
        fast, slow = w.ind("ema_fast"), w.ind("ema_slow")
        if w.i < 1:
            return False
        a, b = w.last(fast, 1), w.last(slow, 1)
        c, d = w.last(fast, 0), w.last(slow, 0)
        if any(np.isnan(x) for x in (a, b, c, d)):
            return False
        return a <= b and c > d

    def _crossed_down(self, w: BarWindow) -> bool:
        fast, slow = w.ind("ema_fast"), w.ind("ema_slow")
        if w.i < 1:
            return False
        a, b = w.last(fast, 1), w.last(slow, 1)
        c, d = w.last(fast, 0), w.last(slow, 0)
        if any(np.isnan(x) for x in (a, b, c, d)):
            return False
        return a >= b and c < d

    # -- main loop --------------------------------------------------------- #

    def on_bar(self, w: BarWindow, portfolio: dict[str, Any]) -> list[Order]:
        close = w.last(w.close)
        atr_now = w.last(w.ind("atr"))
        slow = w.last(w.ind("ema_slow"))
        if np.isnan(close) or np.isnan(atr_now) or np.isnan(slow) or atr_now <= 0:
            return []

        if portfolio["position"] == "flat":
            self._peak_high = float("nan")
            trend_ok = (not self.trend_filter) or close > slow
            if trend_ok and self._crossed_up(w):
                stop = close - self.atr_stop_mult * atr_now
                self._peak_high = w.last(w.high)
                return [
                    Order(
                        action="buy",
                        risk_pct=self.risk_pct,
                        weight=self.max_weight,
                        stop=stop,
                        reason=f"ema{self.fast}/{self.slow} cross up",
                    )
                ]
            return []

        # In a position: ratchet the stop, then look for the exit signal.
        high = w.last(w.high)
        self._peak_high = high if np.isnan(self._peak_high) else max(self._peak_high, high)
        orders: list[Order] = []
        trail = self._peak_high - self.trail_mult * atr_now
        current = portfolio.get("stop")
        new_stop = trail if current is None else max(float(current), trail)
        if new_stop > 0:
            orders.append(Order(action="set_stop", stop=new_stop, reason="atr trail"))

        entry = float(portfolio.get("entry_price") or 0.0)
        if self._crossed_down(w) or (entry and close < entry - 3.0 * atr_now):
            orders.append(Order(action="sell", reason="trend exit"))
        return orders
