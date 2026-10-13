"""Order bookkeeping: fills, costs, stops, sizing, round-trip trade records.

Model and its limits, stated plainly:

* Orders are decided on a bar's close and filled at the *next* bar's open.
* Every fill pays a taker fee plus slippage, both in basis points.
* Protective stops are checked intrabar against that bar's low/high, and a gap
  through the stop fills at the open (worse than the stop) rather than the stop.
* Positions are all-in or flat. No scaling in or out; a second entry while
  already positioned is rejected and recorded as a note. This keeps PnL
  attribution exact, and it is what most single-signal trend systems do anyway.
* Shorting, if enabled, assumes 100% margin, no borrow fee and no funding
  payments. That flatters short backtests on perpetuals; leave it off on spot.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Literal

from quant.config import Costs, RiskConfig

# Ignore orders too small to be meaningful (and below most exchange minimums).
MIN_NOTIONAL = 10.0

Action = Literal["buy", "sell", "set_stop"]


@dataclass
class Order:
    """A request the strategy makes on a bar close, filled on the next open."""

    action: Action
    reason: str = ""
    qty: float | None = None
    weight: float | None = None
    risk_pct: float | None = None
    stop: float | None = None

    def __post_init__(self) -> None:
        if self.action not in ("buy", "sell", "set_stop"):
            raise ValueError(f"unknown order action {self.action!r}")
        if self.action == "set_stop" and self.stop is None:
            raise ValueError("a set_stop order needs a stop price")


@dataclass
class Position:
    """The open position, if any. ``qty`` is signed (negative = short)."""

    qty: float = 0.0
    entry_price: float = 0.0
    entry_ts: int = 0
    entry_fee: float = 0.0
    entry_reason: str = ""
    planned_risk: float = 0.0
    mfe_pct: float = 0.0
    mae_pct: float = 0.0

    @property
    def is_flat(self) -> bool:
        return abs(self.qty) < 1e-12

    @property
    def is_long(self) -> bool:
        return self.qty > 0

    @property
    def side(self) -> str:
        return "flat" if self.is_flat else ("long" if self.is_long else "short")


@dataclass
class Broker:
    """Holds cash, the position, resting stops and the resulting records."""

    initial_cash: float = 10_000.0
    costs: Costs = field(default_factory=Costs)
    risk: RiskConfig = field(default_factory=RiskConfig)
    bar_ms: int = 86_400_000

    cash: float = 0.0
    position: Position = field(default_factory=Position)
    stop_price: float | None = None
    stop_reason: str = ""

    pending: list[Order] = field(default_factory=list)
    fills: list[dict[str, Any]] = field(default_factory=list)
    trades: list[dict[str, Any]] = field(default_factory=list)
    rejections: dict[str, int] = field(default_factory=dict)
    realized_pnl: float = 0.0

    def __post_init__(self) -> None:
        if self.cash == 0.0:
            self.cash = self.initial_cash

    # -- state ------------------------------------------------------------- #

    def equity(self, price: float) -> float:
        return self.cash + self.position.qty * price

    def position_value(self, price: float) -> float:
        return abs(self.position.qty) * price

    def submit(self, order: Order) -> None:
        """Queue an order for the next bar's open."""
        self.pending.append(order)

    def _reject(self, reason: str) -> None:
        self.rejections[reason] = self.rejections.get(reason, 0) + 1

    # -- fill mechanics ---------------------------------------------------- #

    def _fill_price(self, side: str, reference: float) -> float:
        """Reference price adjusted against us by the slippage assumption."""
        rate = self.costs.slippage_rate
        return reference * (1.0 + rate) if side == "buy" else reference * (1.0 - rate)

    def _apply_fill(self, ts: int, signed_qty: float, price: float, reason: str, fees_hint: float = 0.0) -> dict[str, Any]:
        notional = abs(signed_qty) * price
        fee = notional * self.costs.fee_rate
        self.cash -= signed_qty * price
        self.cash -= fee
        old = self.position
        opening = old.is_flat
        new_qty = old.qty + signed_qty

        if opening:
            self.position = Position(
                qty=new_qty,
                entry_price=price,
                entry_ts=ts,
                entry_fee=fee,
                entry_reason=reason,
                planned_risk=fees_hint,
            )
        else:
            qty_abs = abs(old.qty)
            if old.is_long:
                pnl = (price - old.entry_price) * qty_abs
            else:
                pnl = (old.entry_price - price) * qty_abs
            fees = old.entry_fee + fee
            pnl -= fees
            planned = old.planned_risk or abs(old.entry_price - (self.stop_price or old.entry_price)) * qty_abs
            self.realized_pnl += pnl
            self.trades.append(
                {
                    "side": "long" if old.is_long else "short",
                    "qty": round(qty_abs, 10),
                    "entry_ts": old.entry_ts,
                    "exit_ts": ts,
                    "entry_price": old.entry_price,
                    "exit_price": price,
                    "pnl": pnl,
                    "return_pct": (pnl / (old.entry_price * qty_abs) * 100.0) if old.entry_price else 0.0,
                    "fees": fees,
                    "bars_held": max(int(round((ts - old.entry_ts) / self.bar_ms)), 1),
                    "entry_reason": old.entry_reason,
                    "exit_reason": reason,
                    "r_multiple": (pnl / planned) if planned > 0 else 0.0,
                    "mfe_pct": old.mfe_pct * 100.0,
                    "mae_pct": old.mae_pct * 100.0,
                }
            )
            self.position = Position()
            self.stop_price = None
            self.stop_reason = ""

        fill = {
            "ts": ts,
            "side": "buy" if signed_qty > 0 else "sell",
            "qty": round(abs(signed_qty), 10),
            "price": price,
            "notional": notional,
            "fee": fee,
            "reason": reason,
            "cash_after": self.cash,
        }
        self.fills.append(fill)
        return fill

    def _size(self, order: Order, price: float, side: str) -> tuple[float, float]:
        """Return (quantity, planned_risk_amount) for an entry order."""
        equity = self.equity(price)
        planned_risk = 0.0
        if order.qty is not None:
            qty = order.qty
        elif order.risk_pct is not None and order.stop is not None:
            per_unit = (price - order.stop) if side == "buy" else (order.stop - price)
            if per_unit <= 0:
                self._reject(f"invalid stop for {side} entry ({order.reason or 'no reason'})")
                return 0.0, 0.0
            qty = (order.risk_pct * equity) / per_unit
            planned_risk = qty * per_unit
        elif order.weight is not None:
            qty = (min(order.weight, self.risk.max_position_pct) * equity) / price
        else:
            self._reject("order had no qty, weight or risk_pct")
            return 0.0, 0.0

        # Never exceed the configured exposure cap.
        max_qty = (self.risk.max_position_pct * equity) / price
        qty = min(qty, max_qty)

        # Margin/cash constraint. Longs need cash; shorts are capped at 1x equity.
        if side == "buy":
            qty = min(qty, self.cash / (price * (1.0 + self.costs.fee_rate)))
            # Deliberately the same expression _apply_fill uses (notional, then
            # fee) so the cash guard agrees with the debit to the last bit.
            notional = qty * price
            fee = notional * self.costs.fee_rate
            if notional + fee >= self.cash:
                # A full-size buy must not overdraw the account through float
                # rounding, so leave a relative sliver of headroom.
                qty *= (self.cash / (notional + fee)) * (1.0 - 1e-12)
        else:
            qty = min(qty, equity / price)

        if qty * price < MIN_NOTIONAL:
            self._reject("order below minimum notional")
            return 0.0, 0.0
        if planned_risk == 0.0 and order.stop is not None:
            planned_risk = qty * abs(price - order.stop)
        return qty, planned_risk

    # -- bar lifecycle ------------------------------------------------------ #

    def process_open(self, ts: int, open_price: float, high: float, low: float) -> None:
        """Fill everything queued at the previous close, at this bar's open."""
        orders, self.pending = self.pending, []
        for order in orders:
            if order.action == "set_stop":
                if self.position.is_flat:
                    self._reject("set_stop while flat")
                    continue
                self.stop_price = order.stop
                self.stop_reason = order.reason
                continue

            if order.action == "buy":
                if not self.position.is_flat:
                    self._reject("already in a position (no pyramiding)")
                    continue
                price = self._fill_price("buy", open_price)
                qty, planned = self._size(order, price, "buy")
                if qty <= 0:
                    continue
                self._apply_fill(ts, qty, price, order.reason or "entry", fees_hint=planned)
                if order.stop is not None:
                    self.stop_price = order.stop
                    self.stop_reason = "initial stop"
            elif order.action == "sell":
                if self.position.is_flat:
                    if self.risk.allow_short:
                        price = self._fill_price("sell", open_price)
                        qty, planned = self._size(order, price, "sell")
                        if qty > 0:
                            self._apply_fill(ts, -qty, price, order.reason or "short entry", fees_hint=planned)
                            if order.stop is not None:
                                self.stop_price = order.stop
                                self.stop_reason = "initial stop"
                    else:
                        self._reject("sell signal while flat (shorting disabled)")
                    continue
                if not self.position.is_long and not self.risk.allow_short:
                    self._reject("sell signal while short")
                    continue
                price = self._fill_price("sell", open_price)
                self._apply_fill(ts, -self.position.qty, price, order.reason or "exit")

        # The engine calls update_excursion() and check_stop() next, so a
        # position entered on this bar's open can still be stopped out on it.

    def check_stop(self, ts: int, open_price: float, high: float, low: float) -> None:
        """Trigger a resting stop against this bar's range."""
        if self.position.is_flat or self.stop_price is None:
            return
        if self.position.is_long:
            if low > self.stop_price:
                return
            # Gap through the stop: you get the open, not your stop price.
            reference = min(open_price, self.stop_price)
            price = self._fill_price("sell", reference)
            self._apply_fill(ts, -self.position.qty, price, f"stop ({self.stop_reason or 'stop'} loss)")
        else:
            if high < self.stop_price:
                return
            reference = max(open_price, self.stop_price)
            price = self._fill_price("buy", reference)
            self._apply_fill(ts, -self.position.qty, price, f"stop ({self.stop_reason or 'stop'} loss)")

    def update_excursion(self, high: float, low: float) -> None:
        """Track how far the open position ran for and against us."""
        pos = self.position
        if pos.is_flat or not pos.entry_price:
            return
        if pos.is_long:
            fav, adv = high / pos.entry_price - 1.0, low / pos.entry_price - 1.0
        else:
            fav, adv = pos.entry_price / low - 1.0 if low > 0 else 0.0, pos.entry_price / high - 1.0 if high > 0 else 0.0
        pos.mfe_pct = max(pos.mfe_pct, fav)
        pos.mae_pct = min(pos.mae_pct, adv)

    def snapshot(self, price: float) -> dict[str, Any]:
        """Portfolio view handed to the strategy (past-and-present only)."""
        pos = self.position
        return {
            "cash": self.cash,
            "equity": self.equity(price),
            "position": pos.side,
            "qty": pos.qty,
            "entry_price": pos.entry_price,
            "stop": self.stop_price,
            "exposure": self.position_value(price) / self.equity(price) if self.equity(price) else 0.0,
            "realized_pnl": self.realized_pnl,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "initial_cash": self.initial_cash,
            "final_cash": self.cash,
            "realized_pnl": self.realized_pnl,
            "rejections": self.rejections,
            "costs": asdict(self.costs),
        }
