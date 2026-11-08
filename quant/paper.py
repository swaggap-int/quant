"""Paper execution adapter (Milestone 3).

Thin wrapper over :class:`quant.broker.Broker` exposing an explicit paper
account surface: cash, positions, open orders, pending stops, and trade log.
Fill mechanics stay identical to the backtester (next-open fills, fees,
slippage, stops, sizing from stop distance) because it *is* the backtester
broker — this module only names the paper runtime role.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from quant.broker import Broker, Order
from quant.config import Costs, RiskConfig


@dataclass
class PaperAccountState:
    cash: float
    qty: float
    side: str
    entry_price: float
    stop_price: float | None
    stop_reason: str
    open_orders: int
    pending_stop: bool
    exposure: float
    equity: float
    realized_pnl: float
    n_trades: int
    n_fills: int


class PaperExecutionAdapter:
    """Paper runtime execution using backtester fill semantics."""

    def __init__(
        self,
        initial_cash: float = 10_000.0,
        costs: Costs | None = None,
        risk: RiskConfig | None = None,
        bar_ms: int = 86_400_000,
    ) -> None:
        self.broker = Broker(
            initial_cash=initial_cash,
            costs=costs or Costs(),
            risk=risk or RiskConfig(),
            bar_ms=bar_ms,
        )

    # -- order entry ---------------------------------------------------- #
    def submit(self, order: Order) -> None:
        self.broker.submit(order)

    @property
    def open_orders(self) -> list[Order]:
        return list(self.broker.pending)

    @property
    def pending_stop(self) -> float | None:
        return self.broker.stop_price

    @property
    def trades(self) -> list[dict[str, Any]]:
        return self.broker.trades

    @property
    def fills(self) -> list[dict[str, Any]]:
        return self.broker.fills

    # -- bar lifecycle (same order as Engine) --------------------------- #
    def process_bar(self, ts: int, o: float, h: float, low: float, c: float) -> float:
        self.broker.process_open(ts, o, h, low)
        self.broker.update_excursion(h, low)
        self.broker.check_stop(ts, o, h, low)
        return self.broker.equity(c)

    def account_state(self, price: float) -> PaperAccountState:
        snap = self.broker.snapshot(price)
        pos = self.broker.position
        return PaperAccountState(
            cash=float(self.broker.cash),
            qty=float(pos.qty),
            side=pos.side,
            entry_price=float(pos.entry_price),
            stop_price=self.broker.stop_price,
            stop_reason=self.broker.stop_reason,
            open_orders=len(self.broker.pending),
            pending_stop=self.broker.stop_price is not None,
            exposure=float(snap["exposure"]),
            equity=float(snap["equity"]),
            realized_pnl=float(self.broker.realized_pnl),
            n_trades=len(self.broker.trades),
            n_fills=len(self.broker.fills),
        )

    def to_dict(self, price: float) -> dict[str, Any]:
        s = self.account_state(price)
        return dict(
            cash=s.cash,
            qty=s.qty,
            side=s.side,
            entry_price=s.entry_price,
            stop_price=s.stop_price,
            stop_reason=s.stop_reason,
            open_orders=s.open_orders,
            pending_stop=s.pending_stop,
            exposure=s.exposure,
            equity=s.equity,
            realized_pnl=s.realized_pnl,
            n_trades=s.n_trades,
            n_fills=s.n_fills,
        )
