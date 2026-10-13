"""A small, honest event-driven backtesting engine for crypto strategies.

Design goals:
  * No lookahead. Strategies only ever see history up to the current bar.
  * Realistic costs. Taker fees and slippage are charged on every fill.
  * Boring dependencies. numpy for math, stdlib for everything else.
"""

from quant.config import BacktestConfig, Costs, RiskConfig, Timeframe
from quant.datafeed import Candles, load_candles
from quant.engine import BacktestResult, Engine
from quant.strategies import EmaAtrTrend, Strategy

__all__ = [
    "BacktestConfig",
    "BacktestResult",
    "Candles",
    "Costs",
    "EmaAtrTrend",
    "Engine",
    "RiskConfig",
    "Strategy",
    "Timeframe",
    "load_candles",
]

__version__ = "0.1.0"
