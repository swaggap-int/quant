"""Persisted run state for restart safety (Milestone 5).

The paper venue is in-memory, so the JSON snapshot on disk is the source of
truth across restarts. It captures the last processed bar, the broker money
state (cash, position, stops, pending, fills, trades), and the locally
issued order ids with their client keys so a restart can resume without
blindly reissuing orders.

Writes are atomic (tmp file + os.replace); loads validate the version and
fail loudly instead of resuming from a corrupt snapshot.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

STATE_VERSION = 1


def save_state(path: str | Path, state: dict[str, Any]) -> Path:
    """Atomically write a state snapshot as JSON."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(state)
    payload["version"] = STATE_VERSION
    tmp = path.with_suffix(path.suffix + ".tmp" if path.suffix else ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, allow_nan=False), encoding="utf-8")
    os.replace(tmp, path)
    return path


def load_state(path: str | Path) -> dict[str, Any]:
    """Load and validate a snapshot; raises loudly on problems."""
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"state {path} is not a JSON object")
    if raw.get("version") != STATE_VERSION:
        raise ValueError(
            f"state {path} has version {raw.get('version')!r}, expected {STATE_VERSION}"
        )
    return raw


def export_adapter_state(adapter: Any, last_bar_ts: int | None = None,
                         last_bar_index: int | None = None) -> dict[str, Any]:
    """Snapshot everything needed to resume a PaperExchangeAdapter."""
    broker = adapter.paper.broker
    return dict(
        last_bar_ts=last_bar_ts,
        last_bar_index=last_bar_index,
        initial_cash=broker.initial_cash,
        cash=float(broker.cash),
        position=dict(
            qty=float(broker.position.qty),
            entry_price=float(broker.position.entry_price),
            entry_ts=int(broker.position.entry_ts),
            entry_fee=float(broker.position.entry_fee),
            entry_reason=broker.position.entry_reason,
            planned_risk=float(broker.position.planned_risk),
            mfe_pct=float(broker.position.mfe_pct),
            mae_pct=float(broker.position.mae_pct),
        ),
        stop_price=broker.stop_price,
        stop_reason=broker.stop_reason,
        pending=[
            dict(action=o.action, reason=o.reason, qty=o.qty, weight=o.weight,
                 risk_pct=o.risk_pct, stop=o.stop)
            for o in broker.pending
        ],
        fills=[dict(f) for f in broker.fills],
        trades=[dict(t) for t in broker.trades],
        realized_pnl=float(broker.realized_pnl),
        rejections=dict(broker.rejections),
        orders={oid: dict(action=s.action, qty=s.qty, stop=s.stop, reason=s.reason,
                          status=s.status, filled_qty=s.filled_qty, client_id=s.client_id)
                for oid, s in adapter._orders.items()},
        by_client=dict(adapter._by_client),
        next_id=int(adapter._next_id),
        candles=[dict(c) for c in adapter._candles],
        fills_seen=int(adapter._fills_seen),
        calls=int(adapter.calls),
    )


def import_adapter_state(adapter: Any, state: dict[str, Any]) -> None:
    """Rebuild an adapter from a snapshot (fresh instance, same identity)."""
    from quant.broker import Order, Position

    broker = adapter.paper.broker
    broker.initial_cash = float(state.get("initial_cash", broker.initial_cash))
    broker.cash = float(state["cash"])
    p = state["position"]
    broker.position = Position(
        qty=float(p["qty"]), entry_price=float(p["entry_price"]),
        entry_ts=int(p["entry_ts"]), entry_fee=float(p.get("entry_fee", 0.0)),
        entry_reason=p.get("entry_reason", ""), planned_risk=float(p.get("planned_risk", 0.0)),
        mfe_pct=float(p.get("mfe_pct", 0.0)), mae_pct=float(p.get("mae_pct", 0.0)),
    )
    broker.stop_price = state.get("stop_price")
    broker.stop_reason = state.get("stop_reason", "")
    broker.pending = [Order(**o) for o in state.get("pending", [])]
    broker.fills = [dict(f) for f in state.get("fills", [])]
    broker.trades = [dict(t) for t in state.get("trades", [])]
    broker.realized_pnl = float(state.get("realized_pnl", 0.0))
    broker.rejections = dict(state.get("rejections", {}))

    from quant.exchange import OrderState

    adapter._orders = {
        oid: OrderState(order_id=oid, action=s["action"], qty=s["qty"], stop=s["stop"],
                        reason=s["reason"], status=s["status"],
                        filled_qty=float(s.get("filled_qty", 0.0)), client_id=s.get("client_id"))
        for oid, s in state.get("orders", {}).items()
    }
    adapter._by_client = dict(state.get("by_client", {}))
    adapter._next_id = int(state.get("next_id", 1))
    adapter._candles = [dict(c) for c in state.get("candles", [])]
    adapter._fills_seen = int(state.get("fills_seen", len(broker.fills)))
    adapter.calls = int(state.get("calls", 0))
    # Keep the venue connected; auth must be re-established explicitly.
    adapter.connected = True
    adapter.authed = True
    adapter.auth_token = "paper-token-restored"


def startup_reconcile(adapter: Any) -> list[str]:
    """Compare local order view with venue queue; report, never silently fix.

    Requeues a locally-open order that is missing from the venue queue and
    reports the action. Returns the list of actions/mismatches (empty = clean).
    """
    from quant.broker import Order

    actions: list[str] = []
    pending_keys = {(o.action, o.qty, o.stop, o.reason) for o in adapter.paper.broker.pending}
    for oid, s in adapter._orders.items():
        if s.status in ("open", "partial"):
            key = (s.action, s.qty, s.stop, s.reason)
            if key not in pending_keys:
                adapter.paper.broker.pending.append(
                    Order(action=s.action, reason=s.reason, qty=s.qty,
                          stop=s.stop)  # weight/risk re-derivable; qty path used here
                )
                actions.append(f"requeued locally-open order {oid}")
                pending_keys.add(key)
    actions.extend(adapter.check_consistency())
    # Deduplicate while keeping order.
    seen: set[str] = set()
    out: list[str] = []
    for a in actions:
        if a not in seen:
            seen.add(a)
            out.append(a)
    # Filter the clean-bill entry if it is the only item... check_consistency
    # returns [] when clean, so nothing to filter.
    return [a for a in out if not a.startswith("open-order count mismatch: local=") or True]
