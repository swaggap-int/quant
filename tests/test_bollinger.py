"""Bollinger reversion should buy recoveries, not falling prices."""

from __future__ import annotations

import numpy as np

from quant.strategies import BollingerReversion
from quant.strategies.base import BarWindow
from tests.helpers import candles_from


def _candles(closes: list[float]):
    return candles_from([(c, c + 1.0, c - 1.0, c, 1.0) for c in closes])


def _window(indicators: dict[str, np.ndarray], i: int, closes: list[float]) -> BarWindow:
    candles = _candles(closes)
    return BarWindow(candles, indicators, i)


def _base_indicators(n: int = 6) -> dict[str, np.ndarray]:
    return {
        "middle": np.array([np.nan, np.nan, 10.0, 9.5, 10.0, 10.5][:n]),
        "lower": np.array([np.nan, np.nan, 11.0, 8.5, 9.0, 9.5][:n]),
        "trend": np.array([np.nan, np.nan, 8.0, 8.0, 8.0, 8.0][:n]),
        "atr": np.array([np.nan, np.nan, 1.0, 1.0, 1.0, 1.0][:n]),
    }


def test_reclaims_lower_band_only_after_prior_close_was_below_it() -> None:
    closes = [12.0, 11.0, 10.0, 9.0, 10.0, 11.0]
    strategy = BollingerReversion(period=2, trend_period=2, atr_period=2, atr_stop_mult=2.5)
    window = _window(_base_indicators(), 3, closes)

    orders = strategy.on_bar(window, {"position": "flat"})

    assert len(orders) == 1
    assert orders[0].action == "buy"
    assert orders[0].stop == 6.5
    assert "reclaim" in orders[0].reason


def test_does_not_buy_against_the_slow_trend_filter() -> None:
    closes = [12.0, 11.0, 10.0, 9.0, 10.0, 11.0]
    indicators = _base_indicators()
    indicators["trend"] = np.array([np.nan, np.nan, 9.0, 10.0, 10.0, 10.0])
    strategy = BollingerReversion(period=2, trend_period=2, atr_period=2)
    window = _window(indicators, 3, closes)

    assert strategy.on_bar(window, {"position": "flat"}) == []


def test_exits_at_the_middle_band() -> None:
    closes = [12.0, 11.0, 10.0, 9.0, 10.0, 11.0]
    strategy = BollingerReversion(period=2, trend_period=2, atr_period=2)
    window = _window(_base_indicators(), 4, closes)

    orders = strategy.on_bar(window, {"position": "long"})

    assert len(orders) == 1
    assert orders[0].action == "sell"
    assert orders[0].reason == "bollinger mean exit"


def test_prepare_builds_the_expected_causal_arrays() -> None:
    closes = [100.0 + i for i in range(80)]
    candles = candles_from([(c, c + 1.0, c - 1.0, c, 1.0) for c in closes])
    strategy = BollingerReversion(period=20, trend_period=30, atr_period=14)
    indicators = strategy.prepare(candles)

    assert set(indicators) == {"middle", "lower", "trend", "atr"}
    assert all(len(series) == len(candles) for series in indicators.values())
    assert np.isnan(indicators["middle"][18])
    assert not np.isnan(indicators["middle"][19])
    assert np.isnan(indicators["trend"][28])
    assert not np.isnan(indicators["trend"][29])
    assert np.all(indicators["lower"][19:] <= indicators["middle"][19:])
