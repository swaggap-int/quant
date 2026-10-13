"""Configuration objects for a backtest run."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Literal

# Canonical timeframe strings -> milliseconds per bar.
TIMEFRAME_MS: dict[str, int] = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
    "1w": 604_800_000,
}

# Bars per year, used to annualise returns and volatility.
# Crypto trades every day of the year, so a daily bar is 365/year, not 252.
PERIODS_PER_YEAR: dict[str, float] = {
    "1m": 525_600,
    "5m": 105_120,
    "15m": 35_040,
    "1h": 8_760,
    "4h": 2_190,
    "1d": 365,
    "1w": 52,
}

Timeframe = Literal["1m", "5m", "15m", "1h", "4h", "1d", "1w"]


def timeframe_ms(timeframe: str) -> int:
    """Milliseconds per bar for a canonical timeframe string."""
    try:
        return TIMEFRAME_MS[timeframe]
    except KeyError:
        raise ValueError(
            f"unsupported timeframe {timeframe!r}; expected one of "
            f"{', '.join(sorted(TIMEFRAME_MS, key=lambda t: TIMEFRAME_MS[t]))}"
        ) from None


def periods_per_year(timeframe: str) -> float:
    """Bars per year for a canonical timeframe string."""
    try:
        return PERIODS_PER_YEAR[timeframe]
    except KeyError:
        raise ValueError(
            f"unsupported timeframe {timeframe!r}; expected one of "
            f"{', '.join(sorted(PERIODS_PER_YEAR, key=lambda t: TIMEFRAME_MS[t]))}"
        ) from None


@dataclass(frozen=True)
class Costs:
    """Trading frictions charged on every fill.

    Defaults mirror a small Binance spot account: 10 bps taker fee and a
    further 5 bps of slippage per side. Together that is 30 bps of round-trip
    friction, which is the difference between a promising backtest and a real
    one, so do not zero these out unless you are probing signal quality.
    """

    fee_bps: float = 10.0
    slippage_bps: float = 5.0

    @property
    def fee_rate(self) -> float:
        return self.fee_bps / 10_000.0

    @property
    def slippage_rate(self) -> float:
        return self.slippage_bps / 10_000.0

    @property
    def round_trip_bps(self) -> float:
        """Total friction for entering and exiting once, in basis points."""
        return 2 * (self.fee_bps + self.slippage_bps)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["round_trip_bps"] = self.round_trip_bps
        return d


@dataclass(frozen=True)
class RiskConfig:
    """Position sizing and exposure limits.

    ``risk_per_trade`` is the fraction of equity lost if the stop is hit
    exactly. Position size is solved from the entry-to-stop distance, so wider
    stops automatically mean smaller size.
    """

    risk_per_trade: float = 0.01
    max_position_pct: float = 1.0
    allow_short: bool = False

    def __post_init__(self) -> None:
        if not 0 < self.risk_per_trade <= 1:
            raise ValueError("risk_per_trade must be in (0, 1]")
        if not 0 < self.max_position_pct <= 1:
            raise ValueError("max_position_pct must be in (0, 1]")


@dataclass
class BacktestConfig:
    """Everything needed to reproduce a run."""

    symbol: str = "BTC/USDT"
    timeframe: str = "1d"
    start: str | None = None
    end: str | None = None
    initial_cash: float = 10_000.0
    costs: Costs = field(default_factory=Costs)
    risk: RiskConfig = field(default_factory=RiskConfig)
    provider: str = "auto"
    # Market orders are priced at the next bar's open. Filling on the signal
    # bar's own close is a common way to flatter a backtest; we do not do it.
    execution: Literal["next_open"] = "next_open"
    allow_unfilled_at_end: bool = True

    def __post_init__(self) -> None:
        timeframe_ms(self.timeframe)  # validate eagerly
        if self.initial_cash <= 0:
            raise ValueError("initial_cash must be positive")

    @property
    def bar_ms(self) -> int:
        return timeframe_ms(self.timeframe)

    @property
    def periods_per_year(self) -> float:
        return periods_per_year(self.timeframe)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "start": self.start,
            "end": self.end,
            "initial_cash": self.initial_cash,
            "costs": self.costs.to_dict(),
            "risk": asdict(self.risk),
            "provider": self.provider,
            "execution": self.execution,
        }
