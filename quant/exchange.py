"""Exchange adapter contract + paper implementation (Milestone 4).

The contract names the six operations a live venue must support; the paper
implementation runs on the Milestone 3 :class:`PaperExecutionAdapter` (which
itself is the backtester :class:`Broker`, so fills stay comparable) while
adding the live-loop concerns: connect/auth, order ids, cancel, order state,
retry/backoff, and explicit mismatch reporting.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable

from quant.broker import Order
from quant.config import Costs, RiskConfig
from quant.paper import PaperExecutionAdapter


# -- errors (explicit, never silent) ------------------------------------ #
class ExchangeError(RuntimeError):
    """Base for all adapter errors."""


class TransientNetworkError(ExchangeError):
    """Retryable transport blip."""


class AuthError(ExchangeError):
    """Missing or expired credentials; caller must refresh explicitly."""


class RateLimitError(ExchangeError):
    """Call budget exhausted; backs off and surfaces, never queues silently."""


class UnknownOrderError(ExchangeError):
    """No such locally issued order id."""


class OrderStateMismatch(ExchangeError):
    """Local state disagrees with venue state (e.g. cancel a filled order)."""


# -- contract ------------------------------------------------------------ #
@dataclass
class OrderState:
    order_id: str
    action: str
    qty: float | None
    stop: float | None
    reason: str
    status: str = "open"  # open | filled | cancelled | rejected | partial
    filled_qty: float = 0.0
    client_id: str | None = None


class ExchangeAdapter(ABC):
    """Six-operation venue contract (Milestone 4)."""

    @abstractmethod
    def connect(self, auth_token: str | None = None) -> None: ...

    @abstractmethod
    def latest_price(self) -> float | None: ...

    @abstractmethod
    def get_candles(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    def place_order(self, order: Order, client_id: str | None = None) -> str: ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool: ...

    @abstractmethod
    def get_account_state(self, price: float | None = None) -> dict[str, Any]: ...

    @abstractmethod
    def get_order_state(self, order_id: str) -> OrderState: ...


def with_retry(
    fn: Callable[[], Any],
    *,
    max_attempts: int = 3,
    base_sleep_s: float = 0.05,
    sleep_fn: Callable[[float], None] = time.sleep,
    retryable: tuple[type[BaseException], ...] = (TransientNetworkError, RateLimitError),
) -> Any:
    """Run fn with exponential backoff on retryable errors. Attempt count explicit."""
    last: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except retryable as exc:
            last = exc
            if attempt == max_attempts:
                break
            sleep_fn(base_sleep_s * (2 ** (attempt - 1)))
    assert last is not None
    raise last


# -- paper implementation ------------------------------------------------- #
class PaperExchangeAdapter(ExchangeAdapter):
    """Paper venue on top of the backtester fill model.

    Failure injection is explicit and test-only: ``fail_plan`` maps op names
    (``place``, ``cancel``, ``account``) to remaining transient failures.
    Auth expiry is via :meth:`expire_auth`; recovery requires an explicit
    :meth:`refresh_auth` call — never an implicit silent retry.
    """

    def __init__(
        self,
        initial_cash: float = 10_000.0,
        costs: Costs | None = None,
        risk: RiskConfig | None = None,
        bar_ms: int = 86_400_000,
        max_calls: int | None = None,
        logger: Any | None = None,
    ) -> None:
        self.paper = PaperExecutionAdapter(initial_cash, costs, risk, bar_ms)
        self.logger = logger
        self.connected = False
        self.auth_token: str | None = None
        self.authed = False
        self.max_calls = max_calls
        self.calls = 0
        self.fail_plan: dict[str, int] = {}
        self._orders: dict[str, OrderState] = {}
        self._by_client: dict[str, str] = {}
        self._next_id = 1
        self._candles: list[dict[str, Any]] = []
        self._fills_seen = 0
        self.attempt_log: list[str] = []

    # -- connection/auth ------------------------------------------------ #
    def connect(self, auth_token: str | None = None) -> None:
        self.connected = True
        self.auth_token = auth_token or "paper-token"
        self.authed = True

    def expire_auth(self) -> None:
        self.authed = False

    def refresh_auth(self, auth_token: str | None = None) -> None:
        self.auth_token = auth_token or "paper-token-refreshed"
        self.authed = True

    def _require_ready(self, op: str) -> None:
        if not self.connected:
            raise ExchangeError(f"{op}: not connected; call connect() first")
        if not self.authed:
            raise AuthError(f"{op}: auth expired; call refresh_auth() explicitly")
        if self.max_calls is not None:
            self.calls += 1
            if self.calls > self.max_calls:
                raise RateLimitError(f"{op}: call budget {self.max_calls} exceeded")
        remaining = self.fail_plan.get(op, 0)
        if remaining > 0:
            self.fail_plan[op] = remaining - 1
            raise TransientNetworkError(f"{op}: injected transient failure")

    # -- market data ----------------------------------------------------- #
    def latest_price(self) -> float | None:
        return self._candles[-1]["close"] if self._candles else None

    def get_candles(self) -> list[dict[str, Any]]:
        return [dict(c) for c in self._candles]

    def process_bar(self, ts: int, o: float, h: float, low: float, c: float) -> float:
        """Advance venue time; fills queued orders at this bar's open."""
        if self._candles and ts <= self._candles[-1]["ts"]:
            return self.paper.broker.equity(c)  # replay after crash: no double fill
        eq = self.paper.process_bar(ts, o, h, low, c)
        self._candles.append({"ts": ts, "open": o, "high": h, "low": low, "close": c})
        self._sync_order_states()
        return eq

    # -- orders ----------------------------------------------------------- #
    def place_order(self, order: Order, client_id: str | None = None) -> str:
        self._require_ready("place")
        if client_id is not None and client_id in self._by_client:
            return self._by_client[client_id]  # idempotent reissue
        order_id = f"paper-{self._next_id}"
        self._next_id += 1
        self.paper.submit(order)
        state = OrderState(
            order_id=order_id,
            action=order.action,
            qty=order.qty,
            stop=order.stop,
            reason=order.reason,
            status="open",
            client_id=client_id,
        )
        self._orders[order_id] = state
        if client_id is not None:
            self._by_client[client_id] = order_id
        return order_id

    def _emit(self, kind: str, message: str, level: str = "info", **fields: Any) -> None:
        if self.logger is None:
            return
        try:
            self.logger.log(kind, message, level=level, **fields)
        except Exception:
            pass

    def place_order_with_retry(self, order: Order, client_id: str | None = None,
                               max_attempts: int = 3,
                               sleep_fn: Callable[[float], None] | None = None) -> str:
        def _do() -> str:
            self.attempt_log.append("place-attempt")
            try:
                oid = self.place_order(order, client_id=client_id)
            except (TransientNetworkError, RateLimitError) as exc:
                self._emit("retry", f"place retry: {exc}", level="warn")
                raise
            self._emit("order", f"placed {oid}", order_id=oid)
            return oid
        try:
            return with_retry(_do, max_attempts=max_attempts,
                              sleep_fn=sleep_fn or (lambda s: None))
        except Exception as exc:
            self._emit("error", f"place failed after retries: {exc}", level="error")
            raise

    def cancel_order(self, order_id: str) -> bool:
        self._require_ready("cancel")
        state = self._orders.get(order_id)
        if state is None:
            raise UnknownOrderError(f"cancel: unknown order {order_id}")
        if state.status == "filled":
            raise OrderStateMismatch(f"cancel: order {order_id} already filled")
        if state.status == "cancelled":
            return False
        # Remove from broker queue if still pending.
        pending = self.paper.broker.pending
        for i, o in enumerate(pending):
            if (o.action, o.qty, o.stop, o.reason) == (state.action, state.qty, state.stop, state.reason):
                pending.pop(i)
                break
        state.status = "cancelled"
        return True

    def get_order_state(self, order_id: str) -> OrderState:
        state = self._orders.get(order_id)
        if state is None:
            raise UnknownOrderError(f"state: unknown order {order_id}")
        return state

    def record_partial_fill(self, order_id: str, filled_qty: float) -> OrderState:
        """Record an explicit partial fill (never inferred silently)."""
        state = self.get_order_state(order_id)
        if state.status == "filled":
            raise OrderStateMismatch(f"partial: order {order_id} already filled")
        state.filled_qty = float(filled_qty)
        state.status = "partial"
        return state

    # -- account ----------------------------------------------------------- #
    def get_account_state(self, price: float | None = None) -> dict[str, Any]:
        self._require_ready("account")
        px = price if price is not None else (self.latest_price() or 0.0)
        d = self.paper.to_dict(px)
        d.update(connected=self.connected, authed=self.authed, calls=self.calls)
        return d

    # -- consistency (explicit mismatches) -------------------------------- #
    def _sync_order_states(self) -> None:
        new_fills = self.paper.fills[self._fills_seen:]
        self._fills_seen = len(self.paper.fills)
        open_ids = [oid for oid, s in self._orders.items() if s.status in ("open", "partial")]
        for fill, oid in zip(new_fills, open_ids):
            st = self._orders[oid]
            st.status = "filled"
            st.filled_qty = float(fill.get("qty", st.filled_qty or 0.0))

    def check_consistency(self) -> list[str]:
        """Return human-readable mismatches; empty means consistent."""
        out: list[str] = []
        broker_pending = len(self.paper.broker.pending)
        local_open = sum(1 for s in self._orders.values() if s.status in ("open", "partial"))
        if broker_pending != local_open:
            out.append(f"open-order count mismatch: local={local_open} venue-pending={broker_pending}")
        for oid, s in self._orders.items():
            if s.status not in ("open", "filled", "cancelled", "rejected", "partial"):
                out.append(f"order {oid} has unknown status {s.status!r}")
        return out
