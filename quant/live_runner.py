"""Simulated-live runner: same strategy code, same fill model as the backtester.

Milestone 2 glue between the clock/bar-stream adapters and the pricing
engine used by the backtester. Replays historical candles one bar at a time,
evaluates the strategy only when the bar is closed, and uses the same broker
execution path (next-open fills, fees, slippage, stops) so fills remain
comparable across the two paths.

Paper-only and replay-only: no exchange connection, no live market data.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Optional, Sequence

import numpy as np

from quant.broker import Broker
from quant.config import BacktestConfig, Costs, RiskConfig, periods_per_year
from quant.datafeed import Candles
from quant.live_clock import BarClosedPolicy, make_bar_closed_stream
from quant.metrics import compute_metrics, drawdown_series
from quant.strategies.base import BarWindow, Strategy


@dataclasses.dataclass
class LiveRunConfig:
    symbol: str = "TEST/USDT"
    timeframe: str = "1d"
    initial_cash: float = 10_000.0
    costs: Costs | None = None
    risk: RiskConfig | None = None
    warmup_bars: int = 2
    now_ms: Optional[int] = None
    bar_closed_policy: BarClosedPolicy | None = None

    @classmethod
    def from_backtest(cls, cfg: BacktestConfig) -> "LiveRunConfig":
        return cls(
            symbol=cfg.symbol,
            timeframe=cfg.timeframe,
            initial_cash=cfg.initial_cash,
            costs=cfg.costs,
            risk=cfg.risk,
            warmup_bars=2,
        )


def _coerce_live_config(cfg: Any | None) -> LiveRunConfig:
    if cfg is None:
        return LiveRunConfig()
    if isinstance(cfg, LiveRunConfig):
        return cfg
    if isinstance(cfg, BacktestConfig):
        return LiveRunConfig.from_backtest(cfg)
    # duck-typed config with the same field names
    try:
        return LiveRunConfig(
            symbol=getattr(cfg, "symbol", "TEST/USDT"),
            timeframe=getattr(cfg, "timeframe", "1d"),
            initial_cash=getattr(cfg, "initial_cash", 10_000.0),
            costs=getattr(cfg, "costs", None),
            risk=getattr(cfg, "risk", None),
            warmup_bars=int(getattr(cfg, "warmup_bars", 2)),
            now_ms=getattr(cfg, "now_ms", None),
        )
    except Exception:
        return LiveRunConfig()


@dataclasses.dataclass
class LiveRunPolicy:
    """Live guards that can stop or restrict a run after it starts."""

    max_position_qty: Optional[float] = None
    max_position_pct: Optional[float] = None
    max_exposure_pct: Optional[float] = None
    max_orders_per_bar: Optional[int] = None
    max_orders_per_run: Optional[int] = None
    daily_loss_limit_pct: Optional[float] = None
    killed: bool = False

    def is_position_allowed(self, current_qty: float) -> bool:
        if self.killed:
            return False
        if self.max_position_qty is not None and abs(current_qty) > self.max_position_qty:
            return False
        return True

    def blocks_order(self, order_qty: float) -> bool:
        if self.killed:
            return True
        if self.max_position_qty is not None and abs(order_qty) >= self.max_position_qty:
            return True
        return False

    def check_daily_loss(self, equity_series: Sequence[float], latest_equity: float) -> bool:
        if self.killed:
            return False
        if self.daily_loss_limit_pct is None:
            return True
        if not equity_series:
            return True
        prior = list(equity_series[:-1]) if len(equity_series) > 1 else list(equity_series)
        peak = max(prior) if prior else latest_equity
        if not peak:
            return True
        drop_pct = (peak - latest_equity) / peak
        return drop_pct <= self.daily_loss_limit_pct

    def exposure_allowed(self, exposure: float) -> bool:
        if self.killed:
            return False
        if self.max_exposure_pct is None:
            return True
        return exposure <= self.max_exposure_pct + 1e-9


@dataclasses.dataclass
class LiveOutcome:
    config: LiveRunConfig
    trades: list[dict[str, Any]]
    fills: list[dict[str, Any]]
    equity: list[float]
    exposure: list[float]
    cash: list[float]
    notes: list[str]
    bars_seen: int
    closed_bars_evaluated: int
    open_bars_skipped: int
    run_id: str
    policy_trips: list[dict[str, Any]]
    ts: list[int] | None = None
    orders_placed: int = 0
    strategy_errors: int = 0

    def _series_arrays(self, max_points: Optional[int] = None):
        eq = list(self.equity)
        ts = list(self.ts) if self.ts is not None else list(range(len(eq)))
        if max_points is not None and len(eq) > max_points:
            eq = eq[-max_points:]
            ts = ts[-max_points:]
        dd = (drawdown_series(np.asarray(eq, dtype=float)) * 100.0).tolist() if eq else []
        return ts, eq, [float(x) for x in dd]

    def to_dict(self, max_points: Optional[int] = None) -> dict[str, Any]:
        ts, eq, dd = self._series_arrays(max_points)
        return dict(
            mode="simulated_live",
            run_id=self.run_id,
            bars_seen=self.bars_seen,
            closed_bars_evaluated=self.closed_bars_evaluated,
            open_bars_skipped=self.open_bars_skipped,
            final_equity=float(self.equity[-1]) if self.equity else 0.0,
            data=dict(bars=self.bars_seen),
            metrics=self._metrics(),
            notes=list(self.notes),
            policy_trips=list(self.policy_trips),
            series=dict(ts=ts, equity=eq, drawdown_pct=dd),
        )

    def _metrics(self) -> dict[str, Any]:
        try:
            ppy = periods_per_year(self.config.timeframe)
        except Exception:
            ppy = 365.0
        try:
            from quant.config import timeframe_ms

            bar_ms = timeframe_ms(self.config.timeframe)
        except Exception:
            bar_ms = 86_400_000
        eq = np.asarray(self.equity if self.equity else [self.config.initial_cash], dtype=float)
        ts_arr = np.asarray(self.ts if self.ts is not None else list(range(len(eq))), dtype=np.int64)
        return compute_metrics(
            eq,
            ts_arr,
            initial_cash=self.config.initial_cash,
            periods_per_year=ppy,
            bar_ms=bar_ms,
            trades=self.trades,
            fills=self.fills,
            exposure=np.asarray(self.exposure if self.exposure else np.zeros(len(eq)), dtype=float),
        )

    def equity_series(self, max_points: Optional[int] = None) -> dict[str, Any]:
        ts, eq, dd = self._series_arrays(max_points)
        return dict(ts=ts, equity=eq, drawdown_pct=dd)


class LiveRunner:
    """One-pass live replay over historical candles (Engine fill semantics)."""

    def __init__(
        self,
        config: Any | None = None,
        strategy: Strategy | None = None,
        broker: Optional[Broker] = None,
        bar_closed_policy: Optional[BarClosedPolicy] = None,
        run_policy: Optional[LiveRunPolicy] = None,
        policy: Optional[LiveRunPolicy] = None,
        logger: Any | None = None,
    ) -> None:
        self.config = _coerce_live_config(config)
        self.strategy = strategy
        self._broker = broker
        self._bar_closed_policy = bar_closed_policy or BarClosedPolicy()
        self._run_policy = run_policy if run_policy is not None else policy
        self._killed = bool(self._run_policy.killed) if self._run_policy else False
        self._logger = logger

    def _emit(self, kind: str, message: str, level: str = "info", **fields: Any) -> None:
        logger = self._logger
        if logger is None:
            return
        try:
            log = getattr(logger, "log", None)
            if callable(log):
                log(kind, message, level=level, **fields)
            else:
                append = getattr(logger, "append", None)
                if callable(append):
                    append(dict(kind=kind, message=message, level=level, **fields))
        except Exception:
            pass

    def kill(self) -> None:
        self._killed = True
        if self._run_policy is not None:
            self._run_policy.killed = True

    def is_alive(self) -> bool:
        return not self._killed

    def run(self, candles: Candles, now_ms: Optional[int] = None) -> LiveOutcome:
        if self.strategy is None:
            raise ValueError("LiveRunner needs a strategy")
        if self._killed:
            self._emit("health", "runner killed before run", level="error")
            return LiveOutcome(
                config=self.config,
                trades=[],
                fills=[],
                equity=[],
                exposure=[],
                cash=[],
                notes=["runner killed before run"],
                bars_seen=0,
                closed_bars_evaluated=0,
                open_bars_skipped=0,
                run_id="killed",
                policy_trips=[],
                ts=[],
                orders_placed=0,
                strategy_errors=0,
            )
        cfg = self.config
        cost_cfg = cfg.costs or Costs()
        risk_cfg = cfg.risk or RiskConfig()
        now = now_ms if now_ms is not None else cfg.now_ms

        self.strategy.reset()
        indicators = self.strategy.prepare(candles)
        warmup = min(self.strategy.warmup_bars(), max(len(candles) - 2, 0))

        broker = self._broker or Broker(
            initial_cash=cfg.initial_cash,
            costs=cost_cfg,
            risk=risk_cfg,
            bar_ms=candles.bar_ms,
        )

        n = len(candles)
        equity: list[float] = []
        exposure: list[float] = []
        cash: list[float] = []
        notes: list[str] = []
        policy_trips: list[dict[str, Any]] = []
        bars_seen = 0
        closed_evaluated = 0
        open_skipped = 0
        total_orders = 0
        strategy_errors = 0
        fills_seen = len(broker.fills)

        def trip(reason: str, ts_val: int) -> None:
            notes.append(reason)
            policy_trips.append(dict(reason=reason, kind="warn", ts=int(ts_val)))
            self._emit("guard_trip", reason, level="warn", ts=ts_val)

        # Bar-closed stream: robust to the clock using index time by also
        # checking real candle timestamps when now_ms is provided.
        stream = None
        stream_closed: dict[int, bool] = {}
        if now is not None:
            try:
                stream = make_bar_closed_stream(
                    candles, now_ms=now, bar_closed_policy=self._bar_closed_policy
                )
                tmp = []
                while stream:
                    b = stream.advance()
                    tmp.append(b)
                    stream_closed[b.index] = bool(b.bar_closed)
            except Exception:
                stream_closed = {}

        for i in range(n):
            ts_i = int(candles.ts[i])
            o = float(candles.open[i])
            h = float(candles.high[i])
            low = float(candles.low[i])
            c = float(candles.close[i])
            bars_seen += 1

            broker.process_open(ts_i, o, h, low)
            broker.update_excursion(h, low)
            broker.check_stop(ts_i, o, h, low)
            for f in broker.fills[fills_seen:]:
                self._emit("fill", f"filled {f.get('side')} {f.get('qty')} @ {f.get('price')}", ts=f.get("ts", ts_i), **{k: f.get(k) for k in ("side", "qty", "price", "reason") if k in f})
            fills_seen = len(broker.fills)

            eq = broker.equity(c)
            equity.append(float(eq))
            cash.append(float(broker.cash))
            exposure.append(float(broker.position_value(c) / eq) if eq > 0 else 0.0)

            if i < warmup:
                continue

            if now is not None:
                closed = stream_closed.get(i, (ts_i + candles.bar_ms) <= int(now))
                allowed = self._bar_closed_policy.is_safe_to_evaluate(bool(closed))
                if not allowed:
                    open_skipped += 1
                    continue

            if self._killed or (self._run_policy and self._run_policy.killed):
                trip("killed: runner stopped, order not sent", ts_i)
                continue

            closed_evaluated += 1
            window = BarWindow(candles, indicators, i)
            snap = broker.snapshot(c)
            try:
                orders = self.strategy.on_bar(window, snap) or []
            except Exception as exc:
                strategy_errors += 1
                notes.append(f"strategy error at bar {i}: {exc}")
                self._emit("error", f"strategy error at bar {i}: {exc}", level="error", ts=ts_i, bar=i)
                orders = []
            if orders:
                self._emit("decision", f"{len(orders)} order(s) at bar {i}", ts=ts_i, bar=i, n=len(orders))
            seen_this_bar: set[tuple[Any, ...]] = set()
            submitted_this_bar = 0
            for order in orders:
                qty = float(getattr(order, "qty", 0.0) or 0.0)
                fp = (order.action, qty, getattr(order, "stop", None), order.reason)
                if fp in seen_this_bar:
                    trip("duplicate order blocked", ts_i)
                    continue
                seen_this_bar.add(fp)
                if self._run_policy and self._run_policy.blocks_order(qty):
                    trip("policy blocked order", ts_i)
                    continue
                if self._run_policy and not self._run_policy.exposure_allowed(float(snap.get("exposure", 0.0))):
                    if order.action == "buy":
                        trip("exposure guard blocked order", ts_i)
                        continue
                if self._run_policy and self._run_policy.max_orders_per_bar is not None and submitted_this_bar >= self._run_policy.max_orders_per_bar:
                    trip("rate limit blocked order (per-bar)", ts_i)
                    continue
                if self._run_policy and self._run_policy.max_orders_per_run is not None and total_orders >= self._run_policy.max_orders_per_run:
                    trip("rate limit blocked order (per-run)", ts_i)
                    continue
                if self._killed:
                    trip("killed: runner stopped, order not sent", ts_i)
                    continue
                broker.submit(order)
                submitted_this_bar += 1
                total_orders += 1
                self._emit("order", f"submitted {order.action} qty={getattr(order, 'qty', None)} ({order.reason})", ts=ts_i, bar=i, action=order.action, qty=getattr(order, "qty", None), reason=order.reason)

            if self._run_policy and not self._run_policy.check_daily_loss(equity, float(eq)):
                trip("daily loss limit trip", ts_i)
                self._killed = True
                self._run_policy.killed = True

        if broker.pending:
            notes.append(f"{len(broker.pending)} order(s) queued on the final bar never filled.")
        if len(candles) < warmup + 1:
            notes.append("not enough bars to warm up")

        run_id = f"live-{cfg.symbol}-{cfg.timeframe}-{now or 0}"
        return LiveOutcome(
            config=cfg,
            trades=list(broker.trades),
            fills=list(broker.fills),
            equity=equity,
            exposure=exposure,
            cash=cash,
            notes=notes,
            bars_seen=bars_seen,
            closed_bars_evaluated=closed_evaluated,
            open_bars_skipped=open_skipped,
            run_id=run_id,
            policy_trips=policy_trips,
            ts=[int(t) for t in candles.ts],
            orders_placed=total_orders,
            strategy_errors=strategy_errors,
        )


def _split_legacy_args(
    a: Any, b: Any, c: Any
) -> tuple[Any, Any, Any]:
    """Accept both (candles, strategy, config) and (config, strategy, candles)."""
    if isinstance(a, Candles):
        return a, b, c
    if isinstance(c, Candles) and not isinstance(a, Candles):
        return c, b, a
    return a, b, c


def run_simulated_live(
    candles: Any = None,
    strategy: Strategy | None = None,
    config: Any | None = None,
    *,
    now_ms: Optional[int] = None,
    bar_closed_policy: Optional[BarClosedPolicy] = None,
    run_policy: Optional[LiveRunPolicy] = None,
    policy: Optional[LiveRunPolicy] = None,
    logger: Any | None = None,
    **kw: Any,
) -> LiveOutcome:
    if isinstance(candles, BacktestConfig) or isinstance(candles, LiveRunConfig):
        candles, strategy, config = _split_legacy_args(candles, strategy, config)
    if "run_config" in kw and config is None:
        config = kw.pop("run_config")
    rp = run_policy if run_policy is not None else policy
    if rp is None and isinstance(config, LiveRunPolicy):
        rp, config = config, None
    if logger is None:
        logger = kw.pop("logger", None)
    cfg = _coerce_live_config(config)
    # symbol/timeframe follow the candles when the caller used a bare default
    if isinstance(candles, Candles):
        if cfg.symbol == "TEST/USDT" and candles.symbol and candles.symbol != cfg.symbol:
            cfg.symbol = candles.symbol
        if cfg.timeframe == "1d" and candles.timeframe and candles.timeframe != cfg.timeframe:
            cfg.timeframe = candles.timeframe
    runner = LiveRunner(cfg, strategy, bar_closed_policy=bar_closed_policy, run_policy=rp, logger=logger)
    assert isinstance(candles, Candles), "run_simulated_live needs candles"
    assert strategy is not None, "run_simulated_live needs a strategy"
    return runner.run(candles, now_ms=now_ms)
