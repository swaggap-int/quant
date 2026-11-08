"""Donchian breakout baseline: buy new N-bar highs, exit new M-bar lows.

Second family to compare against EMA-ATR trend (roadmap: compare ideas,
not tunings). Same execution contract: closed-bar signals, next-open fills,
ATR stop + sizing so cost comparison stays honest.
"""

from __future__ import annotations

import numpy as np

from quant.broker import Order
from quant.datafeed import Candles
from quant.indicators import atr
from quant.strategies.base import BarWindow, Strategy


class DonchianBreakout(Strategy):
    """Long-only N-bar breakout with ATR stop and M-bar exit."""

    name = "donchian-breakout"

    def __init__(
        self,
        entry_n: int = 20,
        exit_m: int = 10,
        atr_period: int = 14,
        atr_stop_mult: float = 2.5,
        risk_pct: float = 0.02,
        max_weight: float = 1.0,
    ) -> None:
        if entry_n <= exit_m:
            raise ValueError("entry_n must exceed exit_m")
        if risk_pct <= 0:
            raise ValueError("risk_pct must be positive")
        self.entry_n = entry_n
        self.exit_m = exit_m
        self.atr_period = atr_period
        self.atr_stop_mult = atr_stop_mult
        self.risk_pct = risk_pct
        self.max_weight = max_weight
        self.windows = {"break_hi": entry_n, "break_lo": exit_m, "atr": atr_period}
        self.reset()

    def reset(self) -> None:
        self._in_pos = False

    def prepare(self, candles: Candles) -> dict[str, np.ndarray]:
        hi = candles.high
        lo = candles.low
        n, m = self.entry_n, self.exit_m
        bh = np.full_like(hi, np.nan, dtype=float)
        bl = np.full_like(lo, np.nan, dtype=float)
        for i in range(len(hi)):
            if i + 1 >= n:
                bh[i] = float(np.max(hi[i + 1 - n : i + 1]))
            if i + 1 >= m:
                bl[i] = float(np.min(lo[i + 1 - m : i + 1]))
        return {"break_hi": bh, "break_lo": bl, "atr": atr(hi, lo, candles.close, self.atr_period)}

    def on_bar(self, w: BarWindow, portfolio: dict[str, Any]) -> list[Order]:
        close = w.last(w.close)
        atr_now = w.last(w.ind("atr"))
        if np.isnan(close) or np.isnan(atr_now) or atr_now <= 0:
            return []
        if w.i < max(self.entry_n, self.exit_m, self.atr_period) + 1:
            return []
        # breakout level excludes current bar (yesterday's channel)
        hi_prev = w.ind("break_hi")[w.i - 1]
        lo_prev = w.ind("break_lo")[w.i - 1]
        if np.isnan(hi_prev) or np.isnan(lo_prev):
            return []
        if portfolio["position"] == "flat":
            if close > hi_prev:
                stop = close - self.atr_stop_mult * atr_now
                return [Order(action="buy", risk_pct=self.risk_pct, weight=self.max_weight, stop=stop, reason=f"donchian{self.entry_n} breakout")]
            return []
        if close < lo_prev:
            return [Order(action="sell", reason="donchian exit")]
        return []
