"""Does the strategy work for reasons, or did one lucky window flatter it?

A single backtest answers "what happened here". It cannot answer the question
that actually decides whether to risk money, which is whether the result would
survive being asked again somewhere else. This module asks three times:

1. **Breadth** — run the strategy across many markets, timeframes and parameter
   choices, and count how often the result is good *at all*. One cell in fifty
   is the signature of curve fitting; a plateau of neighbouring cells that all
   work is the signature of something real.
2. **Concentration** — is the headline carried by one outlier cell? If the best
   cell supplies most of the aggregate profit, the strategy has one setting, not
   one edge.
3. **Luck** — the harder test. Re-run the same engine with *randomly timed*
   entries that trade the same number of times, hold for comparably long, size
   off the same ATR stop and pay identical fees and slippage. If the real
   signal cannot beat those coin-flip schedules, it is not timing anything.

The last test is the honest one, and it is deliberately unkind: the null
schedule always holds *some* position, so it is fully comparable on the things
that flatter a backtest (trade count, holding time, cost drag, position size).

What none of this does is prove an edge. It reliably exposes the absence of
one, which is the more useful property.

Caveat worth stating: the selection adjustment in
:func:`expected_max_of_draws` treats parameter choices as independent draws. In
reality neighbouring settings are correlated and trade the same bars, so it is
an indication of how much the *best* cell owes to luck, not a formal p-value.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

import numpy as np

from quant.broker import Order
from quant.config import BacktestConfig, Costs, RiskConfig
from quant.datafeed import Candles, DataError, load_candles
from quant.engine import Engine
from quant.indicators import atr
from quant.report import verdict
from quant.strategies import EmaAtrTrend
from quant.strategies.base import BarWindow, Strategy

#: A cell must close at least this many trades before its return means
#: anything. A three-trade backtest is a coin flip with a story attached.
DEFAULT_MIN_TRADES = 15

#: How many random-timing controls to run per market in the luck test.
DEFAULT_NULL_RUNS = 200

#: The reference configuration the luck test is run against. Using the best
#: cell instead would be selection-biased: you would be asking whether the
#: cherry-picked winner beats random timing, which is the wrong question.
BASELINE_PARAMS: dict[str, Any] = {
    "fast": 20,
    "slow": 50,
    "atr_period": 14,
    "atr_stop_mult": 2.5,
    "trail_mult": 3.0,
    "risk_pct": 0.02,
    "max_weight": 1.0,
    "trend_filter": True,
}

_EMA_PAIRS: tuple[tuple[int, int], ...] = ((10, 40), (10, 80), (20, 50), (20, 100), (30, 60))
_RISK_SHAPES: tuple[tuple[float, float], ...] = ((2.0, 2.5), (2.5, 3.0), (4.0, 5.0))


# --------------------------------------------------------------------------- #
# Inputs: what to test and with which settings
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Market:
    """One symbol on one timeframe — a single place the strategy could trade."""

    symbol: str
    timeframe: str

    @property
    def label(self) -> str:
        return f"{self.symbol} {self.timeframe}"


def default_markets() -> list[Market]:
    """A small spread of majors on a slow and a fast clock."""
    return [
        Market("BTC/USDT", "1d"),
        Market("ETH/USDT", "1d"),
        Market("BTC/USDT", "4h"),
        Market("ETH/USDT", "4h"),
    ]


def default_param_grid(risk_pct: float = 0.02) -> list[dict[str, Any]]:
    """15 settings spanning the two things that matter: trend speed and stop width.

    Not a fine grid on purpose. Ten thousand neighbouring settings produce a
    smooth surface you can mine for the best cell, which is the mistake this
    module exists to catch. These are deliberately spaced so the answer is
    "does the neighbourhood work", not "what is the optimum".
    """
    return [
        {
            "fast": fast,
            "slow": slow,
            "atr_period": 14,
            "atr_stop_mult": stop,
            "trail_mult": trail,
            "risk_pct": risk_pct,
            "max_weight": 1.0,
            "trend_filter": True,
        }
        for fast, slow in _EMA_PAIRS
        for stop, trail in _RISK_SHAPES
    ]


def small_param_grid(risk_pct: float = 0.02) -> list[dict[str, Any]]:
    """Three settings spanning the same axes as the full grid, for a quick look."""
    base = {"atr_period": 14, "risk_pct": risk_pct, "max_weight": 1.0, "trend_filter": True}
    return [
        {**base, "fast": 20, "slow": 50, "atr_stop_mult": 2.5, "trail_mult": 3.0},
        {**base, "fast": 10, "slow": 40, "atr_stop_mult": 2.0, "trail_mult": 2.5},
        {**base, "fast": 30, "slow": 60, "atr_stop_mult": 4.0, "trail_mult": 5.0},
    ]


def make_strategy(params: dict[str, Any]) -> EmaAtrTrend:
    """Build the strategy from a parameter dict, defaulting every missing key."""
    merged = {**BASELINE_PARAMS, **params}
    return EmaAtrTrend(
        fast=int(merged["fast"]),
        slow=int(merged["slow"]),
        atr_period=int(merged["atr_period"]),
        atr_stop_mult=float(merged["atr_stop_mult"]),
        trail_mult=float(merged["trail_mult"]),
        risk_pct=float(merged["risk_pct"]),
        max_weight=float(merged["max_weight"]),
        trend_filter=bool(merged["trend_filter"]),
    )


def short_params(params: dict[str, Any]) -> str:
    """Compact label for a parameter set, e.g. ``20/50 s2.5 t3.0``."""
    merged = {**BASELINE_PARAMS, **params}
    return (
        f"{int(merged['fast'])}/{int(merged['slow'])} "
        f"s{float(merged['atr_stop_mult']):.1f} t{float(merged['trail_mult']):.1f}"
    )


# --------------------------------------------------------------------------- #
# The luck test: a strategy that trades on coin flips
# --------------------------------------------------------------------------- #


class RandomEntryStrategy(Strategy):
    """A control with random timing, run through the *same* engine.

    Entries land on bars drawn at random; exits land a randomly sampled holding
    period later. Everything else — ATR-sized position, ATR-scaled stop, fee and
    slippage regime, fill-on-next-open — is shared with the real strategy. So if
    the real one cannot beat this, its *timing* contributes nothing beyond
    being in the market a comparable amount of the time.

    One honest limitation: this controls for activity, holding time and cost
    drag, but not for regime clustering. A real trend system enters after
    sustained moves and a random one does not, which is precisely the
    hypothesis being tested — so that gap is the signal, not a flaw.
    """

    name = "random-entry"

    def __init__(
        self,
        schedule: Sequence[tuple[int, int]],
        *,
        stop_mult: float = 2.5,
        atr_period: int = 14,
        risk_pct: float = 0.02,
        max_weight: float = 1.0,
    ) -> None:
        self.schedule = [(int(a), int(b)) for a, b in schedule]
        self.stop_mult = float(stop_mult)
        self.atr_period = int(atr_period)
        self.risk_pct = float(risk_pct)
        self.max_weight = float(max_weight)
        self.windows = {"atr": self.atr_period}
        self.reset()

    def reset(self) -> None:
        self._k = 0
        self._exit_bar: int | None = None

    def prepare(self, candles: Candles) -> dict[str, np.ndarray]:
        return {"atr": atr(candles.high, candles.low, candles.close, self.atr_period)}

    def on_bar(self, w: BarWindow, portfolio: dict[str, Any]) -> list[Order]:
        if portfolio["position"] == "flat":
            # Skip past any scheduled entry the position never reached — a stop
            # may have closed the trade early, or ATR was not ready yet.
            while self._k < len(self.schedule) and self.schedule[self._k][0] < w.i:
                self._k += 1
            if self._k >= len(self.schedule) or self.schedule[self._k][0] != w.i:
                return []
            _, exit_bar = self.schedule[self._k]
            atr_now = w.last(w.ind("atr"))
            if math.isnan(atr_now) or atr_now <= 0:
                self._k += 1  # cannot size without ATR; skip this slot
                return []
            self._exit_bar = exit_bar
            close = w.last(w.close)
            return [
                Order(
                    action="buy",
                    risk_pct=self.risk_pct,
                    weight=self.max_weight,
                    stop=close - self.stop_mult * atr_now,
                    reason="random entry",
                )
            ]

        if self._exit_bar is not None and w.i >= self._exit_bar:
            self._k += 1
            self._exit_bar = None
            return [Order(action="sell", reason="random exit")]
        return []


def random_schedule(
    n_bars: int,
    *,
    start_bar: int,
    n_trades: int,
    holding_bars: Sequence[int],
    rng: random.Random,
) -> list[tuple[int, int]]:
    """``n_trades`` non-overlapping ``(entry bar, exit bar)`` pairs.

    Entries are spread evenly across the tradable range with a uniform jitter,
    so a random schedule never accidentally clusters in one regime, and holding
    periods are resampled from the real trades' durations.
    """
    last_entry = n_bars - 3  # leave a bar for the fill, a bar to exit
    if n_trades <= 0 or last_entry <= start_bar or not holding_bars:
        return []

    span = last_entry - start_bar
    step = span / n_trades
    entries: list[int] = []
    for k in range(n_trades):
        lo = start_bar + int(math.floor(k * step))
        hi = start_bar + int(math.floor((k + 1) * step)) - 1
        entries.append(rng.randint(lo, max(lo, hi)))

    out: list[tuple[int, int]] = []
    for k, entry in enumerate(entries):
        hold = max(1, int(rng.choice(list(holding_bars))))
        exit_bar = entry + hold
        if k + 1 < len(entries):
            exit_bar = min(exit_bar, entries[k + 1] - 2)  # never overlap
        exit_bar = min(exit_bar, n_bars - 2)
        if exit_bar > entry:
            out.append((entry, exit_bar))
    return out


@dataclass
class NullResult:
    """The real run's return measured against randomly timed schedules."""

    market: Market
    params: dict[str, Any]
    actual_return_pct: float
    actual_trades: int
    null_returns: list[float]
    p_value: float | None
    note: str = ""
    #: How many settings were tried. The selection adjustment asks "how good
    #: would the best of *this many* random schedules look?", which is the
    #: honest bar for the best cell to clear.
    selection_n: int = 1

    @property
    def median_pct(self) -> float | None:
        return float(np.median(self.null_returns)) if self.null_returns else None

    @property
    def p95_pct(self) -> float | None:
        return float(np.percentile(self.null_returns, 95)) if self.null_returns else None

    @property
    def expected_max_pct(self) -> float | None:
        """What the best of ``selection_n`` random schedules would look like."""
        if not self.null_returns:
            return None
        return expected_max_of_draws(self.null_returns, max(1, self.selection_n))

    def to_dict(self) -> dict[str, Any]:
        return _clean(
            {
                "market": self.market.label,
                "params": short_params(self.params),
                "actual_return_pct": self.actual_return_pct,
                "actual_trades": self.actual_trades,
                "null_runs": len(self.null_returns),
                "null_median_pct": self.median_pct,
                "null_p95_pct": self.p95_pct,
                "expected_max_pct": self.expected_max_pct,
                "selection_n": self.selection_n,
                "p_value": self.p_value,
                "note": self.note,
            }
        )


def _clean(obj: Any) -> Any:
    """Recursively replace non-finite floats with null.

    A profit factor of ``inf`` (no losing trades) and a NaN p-value are honest
    values but not valid JSON, and a study that cannot be saved is not a study.
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    return obj


def _p_value(actual: float, nulls: Sequence[float]) -> float:
    """Share of random schedules that matched or beat the real one.

    The ``+1`` in numerator and denominator is the standard smoothed estimator:
    with a finite sample you cannot honestly report a probability of zero.
    """
    if not nulls:
        return float("nan")
    better = sum(1 for r in nulls if r >= actual)
    return (better + 1.0) / (len(nulls) + 1.0)


def expected_max_of_draws(samples: Sequence[float], n_draws: int) -> float:
    """E[max of ``n_draws`` iid draws] from an empirical distribution.

    This is the selection adjustment that stops "best of 15 settings" from
    being read as skill. Given the observed spread of outcomes, how good would
    the *best* of this many tries look if the strategy were doing nothing?

    Exact for the empirical CDF via E[max] = sum_i x_i * (F_i^G - F_{i-1}^G),
    which reduces to the sample mean when G = 1. Assumes the draws are
    independent; neighbouring parameter sets are not, so read it as a scale
    rather than a threshold.
    """
    n = len(samples)
    if n == 0 or n_draws <= 0:
        return float("nan")
    xs = sorted(float(s) for s in samples)
    total = 0.0
    prev = 0.0
    for i, x in enumerate(xs, start=1):
        f = i / n
        total += x * (f**n_draws - prev**n_draws)
        prev = f
    return total


def null_test(
    market: Market,
    candles: Candles,
    params: dict[str, Any] | None = None,
    *,
    base_config: BacktestConfig | None = None,
    replicates: int = DEFAULT_NULL_RUNS,
    seed: int = 0,
    selection_n: int = 1,
) -> NullResult:
    """Run the real strategy, then ``replicates`` randomly timed controls."""
    params = dict(params or BASELINE_PARAMS)
    cfg = _market_config(base_config, market)
    strategy = make_strategy(params)
    try:
        real = Engine(cfg, strategy).run(candles)
    except ValueError as exc:
        return NullResult(market, params, float("nan"), 0, [], None, note=str(exc), selection_n=selection_n)

    trades = real.trades
    if not trades:
        return NullResult(
            market, params, real.metrics["total_return_pct"], 0, [], None,
            note="the real strategy never traded, so there is no schedule to control for",
            selection_n=selection_n,
        )

    holding = [max(1, int(t.get("bars_held") or 1)) for t in trades]
    rng = random.Random(seed)
    returns: list[float] = []
    for _ in range(replicates):
        schedule = random_schedule(
            len(candles),
            start_bar=strategy.warmup_bars(),
            n_trades=len(trades),
            holding_bars=holding,
            rng=rng,
        )
        control = RandomEntryStrategy(
            schedule,
            stop_mult=params["atr_stop_mult"],
            atr_period=params["atr_period"],
            risk_pct=params["risk_pct"],
            max_weight=params["max_weight"],
        )
        try:
            res = Engine(cfg, control).run(candles)
        except ValueError:
            continue
        returns.append(float(res.metrics["total_return_pct"]))

    actual = float(real.metrics["total_return_pct"])
    return NullResult(
        market=market,
        params=params,
        actual_return_pct=actual,
        actual_trades=len(trades),
        null_returns=returns,
        p_value=None if not returns else _p_value(actual, returns),
        selection_n=selection_n,
    )


# --------------------------------------------------------------------------- #
# The sweep
# --------------------------------------------------------------------------- #


@dataclass
class SweepCell:
    """One (market, parameter set) result — or the reason it could not run."""

    market: Market
    params: dict[str, Any]
    metrics: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def label(self) -> str:
        return f"{self.market.label}  {short_params(self.params)}"

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def excess_pct(self) -> float:
        return float(self.metrics.get("excess_return_pct", 0.0))

    @property
    def trades(self) -> int:
        return int(self.metrics.get("trades", 0))

    def good(self, min_trades: int = DEFAULT_MIN_TRADES) -> bool:
        """Traded enough to be meaningful *and* beat buy & hold in its window."""
        return self.ok and self.trades >= min_trades and self.excess_pct > 0

    @property
    def clean(self) -> bool:
        """No red flags from the same rulebook the single-run report uses."""
        return self.ok and not verdict(self.metrics).startswith("Read with care")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "market": self.market.label,
            "params": {k: v for k, v in self.params.items()},
            "params_label": short_params(self.params),
            "error": self.error,
        }
        if self.ok:
            m = self.metrics
            out["result"] = {
                "total_return_pct": m.get("total_return_pct"),
                "buy_hold_return_pct": m.get("buy_hold_return_pct"),
                "excess_return_pct": m.get("excess_return_pct"),
                "sharpe": m.get("sharpe"),
                "max_drawdown_pct": m.get("max_drawdown_pct"),
                "trades": m.get("trades"),
                "win_rate_pct": m.get("win_rate_pct"),
                "profit_factor": m.get("profit_factor"),
                "bars_in_market_pct": m.get("bars_in_market_pct"),
            }
            out["good"] = self.good()
            out["clean"] = self.clean
        return _clean(out)


@dataclass
class SweepReport:
    """The whole sweep plus its aggregate read."""

    cells: list[SweepCell]
    nulls: list[NullResult]
    grid_size: int
    window: dict[str, Any]
    summary: dict[str, Any]
    #: Market label -> bars, kept so the luck test can reuse the download.
    data: dict[str, Candles] = field(default_factory=dict)

    def __str__(self) -> str:
        return format_sweep_report(self)

    def to_dict(self) -> dict[str, Any]:
        return _clean(
            {
                "window": self.window,
                "grid_size": self.grid_size,
                "summary": self.summary,
                "cells": [c.to_dict() for c in self.cells],
                "nulls": [n.to_dict() for n in self.nulls],
                "null_reference": short_params(self.nulls[0].params) if self.nulls else None,
            }
        )


def _market_config(base: BacktestConfig | None, market: Market) -> BacktestConfig:
    """Copy the base config onto one market, so cells share every other setting."""
    if base is None:
        return BacktestConfig(symbol=market.symbol, timeframe=market.timeframe)
    return BacktestConfig(
        symbol=market.symbol,
        timeframe=market.timeframe,
        start=base.start,
        end=base.end,
        initial_cash=base.initial_cash,
        costs=Costs(fee_bps=base.costs.fee_bps, slippage_bps=base.costs.slippage_bps),
        risk=RiskConfig(
            risk_per_trade=base.risk.risk_per_trade,
            max_position_pct=base.risk.max_position_pct,
            allow_short=base.risk.allow_short,
        ),
        provider=base.provider,
    )


def run_sweep(
    markets: Sequence[Market],
    grid: Sequence[dict[str, Any]],
    *,
    base_config: BacktestConfig | None = None,
    min_trades: int = DEFAULT_MIN_TRADES,
    loader: Callable[[Market], Candles] | None = None,
    refresh: bool = False,
    verbose: bool = True,
    on_progress: Callable[[str], None] | None = None,
) -> SweepReport:
    """Run every parameter set on every market, reusing each market's bars.

    Data is fetched once per market and reused across the whole grid, so the
    grid is nearly free compared with the download. A market that cannot be
    loaded marks all of its cells as errored rather than sinking the sweep —
    a missing venue is data about the world, not a crash.
    """
    if loader is None:
        def loader(m: Market) -> Candles:  # type: ignore[misc]
            cfg = _market_config(base_config, m)
            return load_candles(
                m.symbol, m.timeframe, cfg.start, cfg.end,
                provider=cfg.provider, refresh=refresh, verbose=verbose,
            )

    cells: list[SweepCell] = []
    loaded: dict[str, Candles] = {}
    total = len(markets) * len(grid)
    done = 0

    for market in markets:
        cfg = _market_config(base_config, market)
        try:
            candles = loader(market)
        except (DataError, ValueError) as exc:
            for params in grid:
                cells.append(SweepCell(market, dict(params), error=str(exc)))
                done += 1
            if on_progress:
                on_progress(f"  {market.label:<16} no data: {exc}")
            continue
        loaded[market.label] = candles
        if on_progress:
            on_progress(f"  {market.label:<16} {len(candles)} bars  {candles.start_iso} -> {candles.end_iso}")
        for params in grid:
            strategy = make_strategy(params)
            try:
                result = Engine(cfg, strategy).run(candles)
                metrics = dict(result.metrics)
            except ValueError as exc:
                cells.append(SweepCell(market, dict(params), error=str(exc)))
            else:
                cells.append(SweepCell(market, dict(params), metrics=metrics))
            done += 1
            if on_progress and done % 10 == 0:
                on_progress(f"  ... {done}/{total} runs")

    # Report the window that was actually tested, not the one that was asked for:
    # with no explicit dates the data layer picks its own, and "now" tells nobody.
    starts = [c.start_iso for c in loaded.values()]
    ends = [c.end_iso for c in loaded.values()]
    window = {
        "start": (base_config.start if base_config and base_config.start else (min(starts) if starts else None)),
        "end": (base_config.end if base_config and base_config.end else (max(ends) if ends else None)),
        "provider": (base_config.provider if base_config else "auto"),
        "fee_bps": (base_config.costs.fee_bps if base_config else 10.0),
        "slippage_bps": (base_config.costs.slippage_bps if base_config else 5.0),
    }
    summary = summarize(cells, [], grid_size=len(grid), min_trades=min_trades)
    return SweepReport(
        cells=cells, nulls=[], grid_size=len(grid), window=window, summary=summary, data=loaded
    )


def run_study(
    markets: Sequence[Market],
    grid: Sequence[dict[str, Any]],
    *,
    base_config: BacktestConfig | None = None,
    min_trades: int = DEFAULT_MIN_TRADES,
    loader: Callable[[Market], Candles] | None = None,
    refresh: bool = False,
    verbose: bool = True,
    null_runs: int = DEFAULT_NULL_RUNS,
    seed: int = 0,
    reference_params: dict[str, Any] | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> SweepReport:
    """Breadth sweep plus the luck test, sharing one download per market."""
    report = run_sweep(
        markets, grid,
        base_config=base_config, min_trades=min_trades,
        loader=loader, refresh=refresh, verbose=verbose, on_progress=on_progress,
    )
    if null_runs <= 0:
        return report

    for market in markets:
        candles = report.data.get(market.label)
        if candles is None:
            continue
        if on_progress:
            on_progress(f"  luck test: {market.label} x{null_runs} random schedules")
        report.nulls.append(
            null_test(
                market, candles, reference_params,
                base_config=base_config, replicates=null_runs, seed=seed,
                selection_n=len(grid),
            )
        )
    report.summary = summarize(report.cells, report.nulls, grid_size=len(grid), min_trades=min_trades)
    return report


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #


def _describe(values: Sequence[float]) -> dict[str, float]:
    if not values:
        return {"median": 0.0, "p25": 0.0, "p75": 0.0, "min": 0.0, "max": 0.0}
    arr = np.asarray(values, dtype=float)
    return {
        "median": float(np.median(arr)),
        "p25": float(np.percentile(arr, 25)),
        "p75": float(np.percentile(arr, 75)),
        "min": float(arr.min()),
        "max": float(arr.max()),
    }


def summarize(
    cells: Sequence[SweepCell],
    nulls: Sequence[NullResult] = (),
    *,
    grid_size: int | None = None,
    min_trades: int = DEFAULT_MIN_TRADES,
) -> dict[str, Any]:
    """Turn a pile of cells into the "how often was it good" answers."""
    ok = [c for c in cells if c.ok]
    errored = [c for c in cells if not c.ok]
    good = [c for c in ok if c.good(min_trades)]
    clean = [c for c in ok if c.clean]
    positive = [c for c in ok if c.metrics.get("total_return_pct", 0.0) > 0]
    excess = _describe([c.excess_pct for c in ok])
    n = len(ok)

    positive_excess = sum(c.excess_pct for c in ok if c.excess_pct > 0)
    best = max(ok, key=lambda c: c.excess_pct, default=None)
    worst = min(ok, key=lambda c: c.excess_pct, default=None)

    # Per-market rollup, in the order the markets were given.
    by_market: list[dict[str, Any]] = []
    seen: list[Market] = []
    for c in cells:
        if c.market not in seen:
            seen.append(c.market)
    for market in seen:
        group = [c for c in ok if c.market == market]
        market_best = max(group, key=lambda c: c.excess_pct, default=None)
        row = {
            "market": market.label,
            "cells": len([c for c in cells if c.market == market]),
            "ran": len(group),
            "beat_buy_hold": sum(1 for c in group if c.good(min_trades)),
            "beat_buy_hold_pct": 100.0 * sum(1 for c in group if c.good(min_trades)) / len(group) if group else 0.0,
            "median_excess_pct": _describe([c.excess_pct for c in group])["median"],
            "median_sharpe": _describe([float(c.metrics.get("sharpe", 0.0)) for c in group])["median"],
            "trades": sum(c.trades for c in group),
            "best_label": short_params(market_best.params) if market_best else "",
            "best_excess_pct": market_best.excess_pct if market_best else 0.0,
            "best_total_return_pct": (
                float(market_best.metrics.get("total_return_pct", 0.0)) if market_best else 0.0
            ),
        }
        null = next((x for x in nulls if x.market == market), None)
        if null is not None:
            row.update(
                {
                    "actual_return_pct": null.actual_return_pct,
                    "null_median_pct": null.median_pct,
                    "null_p95_pct": null.p95_pct,
                    "p_value": null.p_value,
                    "expected_max_pct": null.expected_max_pct,
                    "null_note": null.note,
                }
            )
        by_market.append(row)

    p_values = [r["p_value"] for r in by_market if r.get("p_value") is not None]
    summary: dict[str, Any] = {
        "planned": len(cells),
        "ran": n,
        "errored": len(errored),
        "completed_pct": 100.0 * n / len(cells) if cells else 0.0,
        "grid_size": grid_size if grid_size is not None else len(cells),
        "min_trades": min_trades,
        "beat_buy_hold": len(good),
        "beat_buy_hold_pct": 100.0 * len(good) / n if n else 0.0,
        "clean": len(clean),
        "clean_pct": 100.0 * len(clean) / n if n else 0.0,
        "positive": len(positive),
        "positive_pct": 100.0 * len(positive) / n if n else 0.0,
        "median_excess_pct": excess["median"],
        "p25_excess_pct": excess["p25"],
        "p75_excess_pct": excess["p75"],
        "median_sharpe": _describe([float(c.metrics.get("sharpe", 0.0)) for c in ok])["median"],
        "median_total_return_pct": _describe([float(c.metrics.get("total_return_pct", 0.0)) for c in ok])["median"],
        "median_max_drawdown_pct": _describe([float(c.metrics.get("max_drawdown_pct", 0.0)) for c in ok])["median"],
        "median_bars_in_market_pct": _describe([float(c.metrics.get("bars_in_market_pct", 0.0)) for c in ok])["median"],
        "total_trades": sum(c.trades for c in ok),
        "best": None
        if best is None
        else {
            "label": best.label,
            "excess_pct": best.excess_pct,
            "total_return_pct": float(best.metrics.get("total_return_pct", 0.0)),
            "params": best.params,
        },
        "worst": None if worst is None else {"label": worst.label, "excess_pct": worst.excess_pct},
        "best_share_pct": 100.0 * best.excess_pct / positive_excess if (best and positive_excess > 0) else 0.0,
        "median_p_value": float(np.median(p_values)) if p_values else None,
        "by_market": by_market,
        "errors": _group_errors(errored),
    }
    summary["verdict"] = sweep_verdict(summary)
    return summary


def _group_errors(errored: Sequence[SweepCell]) -> list[dict[str, Any]]:
    """One row per distinct failure, not one per cell that hit it.

    Every setting on a market that cannot load fails with the same message; a
    report that repeats it fifteen times hides the other problems.
    """
    grouped: list[dict[str, Any]] = []
    for cell in errored:
        for row in grouped:
            if row["market"] == cell.market.label and row["error"] == cell.error:
                row["cells"] += 1
                break
        else:
            grouped.append({"market": cell.market.label, "error": cell.error, "cells": 1})
    return grouped


def sweep_verdict(summary: dict[str, Any]) -> str:
    """One paragraph, deliberately conservative, on breadth and luck.

    Reads the aggregate, never a single cell's return. The point is to say
    whether the result generalises, and to name what would falsify it.
    """
    n = summary.get("ran", 0)
    if not n:
        return "Nothing ran, so there is nothing to conclude. Check the data errors above."

    beat = summary.get("beat_buy_hold_pct", 0.0)
    median_excess = summary.get("median_excess_pct", 0.0)
    best_share = summary.get("best_share_pct", 0.0)
    p_value = summary.get("median_p_value")

    if beat >= 60 and median_excess > 0:
        breadth = (
            f"It beat buy & hold in {beat:.0f}% of the {n} market/setting combinations with a "
            f"median excess of {median_excess:+.1f}%, which is breadth rather than a single lucky cell."
        )
    elif beat >= 30:
        breadth = (
            f"It beat buy & hold in {beat:.0f}% of the {n} combinations and lost to it in the rest "
            f"(median excess {median_excess:+.1f}%). That is a fragile result: which side of the line "
            "you land on depends mostly on which setting you happened to pick."
        )
    else:
        breadth = (
            f"It beat buy & hold in only {beat:.0f}% of the {n} combinations (median excess "
            f"{median_excess:+.1f} points). As a way to beat simply holding, it does not work on any "
            "market or setting tested here — which is what a low-exposure system looks like in a "
            "market that mostly rose."
        )

    if best_share >= 40:
        concentration = (
            f" The single best cell supplies {best_share:.0f}% of all the positive excess in the "
            "sweep, so the headline number is one outlier, not a plateau."
        )
    elif best_share > 0:
        concentration = f" No single cell dominates the excess (best is {best_share:.0f}% of it)."
    else:
        concentration = " Not one cell produced positive excess to concentrate."

    if p_value is None:
        luck = " The random-timing control was not run, so nothing here rules out luck."
    elif p_value <= 0.10:
        luck = (
            f" Random entries drawing the same number of trades, holding times, size, stops and costs "
            f"matched or beat it only {p_value * 100:.0f}% of the time (median across markets), so the "
            "timing carries information."
        )
    elif p_value <= 0.5:
        luck = (
            f" Random timing matched or beat it {p_value * 100:.0f}% of the time. The signal is weak: "
            "real but not distinguishable from a coin flip on one window."
        )
    else:
        luck = (
            f" Random timing matched or beat it {p_value * 100:.0f}% of the time, so the entries are "
            "not adding anything a random schedule would not have got."
        )

    # Breadth and the luck test can point in opposite directions, and that is not
    # a contradiction to paper over: a low-exposure system can beat random timing
    # and still lose to simply holding in a market that only went up. Say so.
    if p_value is not None and p_value <= 0.10 and beat < 30:
        reconciliation = (
            " Both readings hold at once: the entries carry information against random timing, but a "
            "system that is flat most of the time still trails buy & hold in a market that rose. "
            "Judge it on risk-adjusted terms, not on headline return."
        )
    elif p_value is not None and p_value > 0.5 and beat >= 60:
        reconciliation = (
            " That breadth is suspect given the luck test: when random timing does as well, the gain "
            "looks like exposure rather than signal."
        )
    else:
        reconciliation = ""
    return breadth + concentration + luck + reconciliation


# --------------------------------------------------------------------------- #
# Console report
# --------------------------------------------------------------------------- #


def _pct(x: float, places: int = 1) -> str:
    return f"{x:+.{places}f}%"


def _day(value: Any, fallback: str = "n/a") -> str:
    """Trim an ISO timestamp to its date, for the header line."""
    if not value:
        return fallback
    return str(value)[:10]


def _num(x: float | None, places: int = 2, dash: str = "n/a") -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return dash
    return f"{x:.{places}f}"


def _rule(width: int = 82, char: str = "-") -> str:
    return char * width


def format_sweep_report(report: SweepReport) -> str:
    """The sweep as a terminal-first read: breadth first, then luck."""
    s = report.summary
    lines: list[str] = []
    lines.append(_rule(82, "="))
    n_markets = len(s["by_market"])
    lines.append(" ROBUSTNESS SWEEP  -  does it work anywhere other than the window you chose?")
    lines.append(
        f" {n_markets} market{'s' if n_markets != 1 else ''} x {s['grid_size']} settings = "
        f"{s['planned']} runs   "
        f"window {_day(report.window.get('start'))} -> {_day(report.window.get('end'))}"
    )
    lines.append(
        f" costs fee {report.window['fee_bps']:g}bps + slip {report.window['slippage_bps']:g}bps per side   "
        f"min trades to count {s['min_trades']}"
    )
    lines.append(_rule(82, "="))

    if s["ran"]:
        lines.extend(_summary_block(s))
        lines.extend(_concentration_block(s))
        lines.append(" BY MARKET")
        has_luck = any(r.get("p_value") is not None for r in s["by_market"])
        header = f"  {'market':<16}{'cells':>6}{'beat b&h':>10}{'median excess':>15}{'median Sharpe':>15}{'trades':>8}"
        if has_luck:
            header += f"{'luck p':>8}"
        lines.append(header)
        for r in s["by_market"]:
            row = (
                f"  {r['market']:<16}{r['ran']:>6}"
                f"{r['beat_buy_hold']:>4} {r['beat_buy_hold_pct']:>4.0f}%"
                f"{_pct(r['median_excess_pct']):>15}"
                f"{_num(r['median_sharpe']):>15}"
                f"{r['trades']:>8}"
            )
            if has_luck:
                row += f"{_num(r.get('p_value'), 2):>8}"
            lines.append(row)
        lines.extend(_luck_block(s))

    if s["errors"]:
        lines.append(" DATA ERRORS")
        for e in s["errors"]:
            label = f"  {e['market']}"
            if e.get("cells", 1) > 1:
                label += f" ({e['cells']} settings)"
            wrapped = _wrap(str(e["error"]), 70)
            lines.append(f"{label}: {wrapped[0]}")
            for cont in wrapped[1:]:
                lines.append(f"      {cont}")

    lines.append(" VERDICT")
    for chunk in _wrap(s["verdict"], 78):
        lines.append(f"  {chunk}")
    lines.append(_rule(82, "="))
    return "\n".join(lines)


def _summary_block(s: dict[str, Any]) -> list[str]:
    lines = [" HOW OFTEN WAS IT GOOD?"]
    lines.append(f"  runs completed          {s['ran']:>5} / {s['planned']}   ({s['completed_pct']:.0f}%)")
    if s["errored"]:
        lines.append(f"  could not run           {s['errored']:>5}   (see data errors below)")
    lines.append(
        f"  beat buy & hold         {s['beat_buy_hold']:>5}   {s['beat_buy_hold_pct']:>5.1f}%"
        f"   <- the headline"
    )
    lines.append(f"  no red flags at all     {s['clean']:>5}   {s['clean_pct']:>5.1f}%")
    lines.append(f"  made money              {s['positive']:>5}   {s['positive_pct']:>5.1f}%")
    lines.append(
        f"  median excess vs b&h    {_pct(s['median_excess_pct']):>10}   "
        f"(p25 {_pct(s['p25_excess_pct'])}, p75 {_pct(s['p75_excess_pct'])} points)"
    )
    lines.append(
        f"  median Sharpe           {_num(s['median_sharpe']):>10}   "
        f"median drawdown {s['median_max_drawdown_pct']:.1f}%   "
        f"in market {s['median_bars_in_market_pct']:.0f}%"
    )
    lines.append(f"  closed trades           {s['total_trades']:>10}")
    return lines


def _concentration_block(s: dict[str, Any]) -> list[str]:
    """Is one setting carrying the whole result? That is a curve-fit tell."""
    if not (s["best"] and s["worst"]):
        return []
    lines = [" CONCENTRATION"]
    lines.append(f"  best    {s['best']['label']:<34} {_pct(s['best']['excess_pct'])} excess")
    lines.append(f"  worst   {s['worst']['label']:<34} {_pct(s['worst']['excess_pct'])} excess")
    if s["best_share_pct"] > 0:
        lines.append(
            f"  best cell holds {s['best_share_pct']:.0f}% of all positive excess   "
            "(high = one setting is carrying the story)"
        )
    else:
        lines.append("  no cell produced positive excess to concentrate")
    return lines


def _luck_block(s: dict[str, Any]) -> list[str]:
    """The random-timing control: the part of the report that can falsify the rest."""
    nulls = [r for r in s["by_market"] if r.get("null_median_pct") is not None]
    if not nulls:
        return []
    g = s["grid_size"]
    lines = [" LUCK TEST  (random entries, same trade count / holding / size / stops / costs)"]
    lines.append(
        f"  {'market':<16}{'reference':>10}{'random median':>15}{'random p95':>12}"
        f"{'p-value':>9}{f'best of {g} rnd':>15}{'best cell':>11}"
    )
    for r in nulls:
        lines.append(
            f"  {r['market']:<16}{_pct(r['actual_return_pct']):>10}"
            f"{_pct(r['null_median_pct']):>15}"
            f"{_pct(r['null_p95_pct']):>12}"
            f"{_num(r.get('p_value'), 2):>9}"
            f"{_pct(r.get('expected_max_pct', 0.0)):>15}"
            f"{_pct(r.get('best_total_return_pct', 0.0)):>11}"
        )
    lines.append(f"  'reference' is the baseline setting, never the best one. 'best of {g} rnd' is")
    lines.append(f"  what the pick of {g} random schedules looks like; if that clears 'best cell',")
    lines.append("  the winner is selection rather than signal.")
    for r in nulls:
        if r.get("null_note"):
            lines.append(f"  note {r['market']}: {r['null_note']}")
    return lines


def _wrap(text: str, width: int) -> list[str]:
    """Greedy wrap, because the verdict is prose and the terminal is narrow."""
    words = text.split()
    out: list[str] = []
    line = ""
    for word in words:
        if not line:
            line = word
        elif len(line) + 1 + len(word) <= width:
            line += " " + word
        else:
            out.append(line)
            line = word
    if line:
        out.append(line)
    return out
