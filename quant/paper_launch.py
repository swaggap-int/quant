"""Paper launch discipline harness (Milestone 7).

Runs the same strategy twice over the same candles — once through the
simulated-live `LiveRunner`, once through a bar-by-bar `PaperExchangeAdapter`
venue loop with the same warmup and policy guards — then compares fills and
behavior and documents divergence reasons. Paper-only; no live venue.
"""

from __future__ import annotations

from typing import Any

from quant.broker import Broker
from quant.config import Costs, RiskConfig
from quant.datafeed import Candles
from quant.exchange import PaperExchangeAdapter
from quant.live_runner import LiveOutcome, LiveRunPolicy, run_simulated_live
from quant.observability import LiveLogger
from quant.strategies.base import BarWindow


def run_paper_venue(candles: Candles, strategy: Any,
                    policy: LiveRunPolicy | None = None,
                    costs: Costs | None = None, risk: RiskConfig | None = None,
                    initial_cash: float = 10_000.0,
                    logger: LiveLogger | None = None) -> dict[str, Any]:
    """Drive a paper venue bar-by-bar; mirrors LiveRunner guards."""
    ex = PaperExchangeAdapter(initial_cash=initial_cash,
                              costs=costs, risk=risk, bar_ms=candles.bar_ms,
                              logger=logger)
    # Honour explicit cash when the caller passes a config-like strategy attr.
    ex.connect()
    strategy.reset()
    indicators = strategy.prepare(candles)
    warmup = min(strategy.warmup_bars(), max(len(candles) - 2, 0))
    notes: list[str] = []
    trips: list[dict[str, Any]] = []
    equity: list[float] = []
    total_orders = 0

    def trip(reason: str, ts_val: int) -> None:
        notes.append(reason)
        trips.append(dict(reason=reason, kind="warn", ts=int(ts_val)))

    for i in range(len(candles)):
        ts_i = int(candles.ts[i])
        o, h, low, c = (float(candles.open[i]), float(candles.high[i]),
                        float(candles.low[i]), float(candles.close[i]))
        eq = ex.process_bar(ts_i, o, h, low, c)
        equity.append(float(eq))
        if i < warmup:
            continue
        if policy and policy.killed:
            trip("killed: venue stopped, order not sent", ts_i)
            continue
        snap = ex.paper.broker.snapshot(c)
        try:
            orders = strategy.on_bar(BarWindow(candles, indicators, i), snap) or []
        except Exception as exc:
            notes.append(f"strategy error at bar {i}: {exc}")
            continue
        seen: set[tuple[Any, ...]] = set()
        sent_this_bar = 0
        for k, order in enumerate(orders):
            qty = float(getattr(order, "qty", 0.0) or 0.0)
            fp = (order.action, qty, getattr(order, "stop", None), order.reason)
            if fp in seen:
                trip("duplicate order blocked", ts_i)
                continue
            seen.add(fp)
            if policy and policy.blocks_order(qty):
                trip("policy blocked order", ts_i)
                continue
            if policy and not policy.exposure_allowed(float(snap.get("exposure", 0.0))) and order.action == "buy":
                trip("exposure guard blocked order", ts_i)
                continue
            if policy and policy.max_orders_per_bar is not None and sent_this_bar >= policy.max_orders_per_bar:
                trip("rate limit blocked order (per-bar)", ts_i)
                continue
            if policy and policy.max_orders_per_run is not None and total_orders >= policy.max_orders_per_run:
                trip("rate limit blocked order (per-run)", ts_i)
                continue
            ex.place_order(order, client_id=f"bar-{i}-{k}-{order.action}-{order.reason}")
            sent_this_bar += 1
            total_orders += 1
        if policy and not policy.check_daily_loss(equity, float(eq)):
            trip("daily loss limit trip", ts_i)
            policy.killed = True

    return dict(fills=list(ex.paper.fills), trades=list(ex.paper.trades),
                equity=equity, notes=notes, trips=trips, orders_placed=total_orders,
                venue=ex)


def compare_runs(live: LiveOutcome, paper: dict[str, Any]) -> list[str]:
    """Compare fills/behavior; return human divergence reasons (empty = parity)."""
    out: list[str] = []
    lf, pf = live.fills, paper["fills"]
    if len(lf) != len(pf):
        out.append(f"fill count differs: live={len(lf)} paper={len(pf)} "
                   f"(usual cause: final-bar pending or a policy trip evaluated one bar apart)")
        return out
    for n, (a, b) in enumerate(zip(lf, pf)):
        if a.get("ts") != b.get("ts") or abs(float(a.get("price", 0)) - float(b.get("price", 0))) > 1e-9:
            out.append(f"fill {n} differs: live={a.get('ts')}/{a.get('price')} "
                       f"paper={b.get('ts')}/{b.get('price')}")
    if abs(float(live.equity[-1]) - float(paper["equity"][-1])) > 1e-6 if live.equity else False:
        out.append("final equity differs despite identical fills (check fee/slippage config parity)")
    if len(live.trades) != len(paper["trades"]):
        out.append(f"trade count differs: live={len(live.trades)} paper={len(paper['trades'])}")
    return out


def launch_report(live: LiveOutcome, paper: dict[str, Any], divergences: list[str],
                  period_bars: int, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return dict(
        mode="paper_launch_check",
        run_id=live.run_id,
        period_bars=period_bars,
        live_fills=len(live.fills),
        paper_fills=len(paper["fills"]),
        live_trades=len(live.trades),
        paper_trades=len(paper["trades"]),
        parity_ok=not divergences,
        divergences=list(divergences),
        divergence_notes=[
            "Same fill model (next-open, fees, slippage, stops) in both paths by construction.",
            "Same warmup (strategy.warmup_bars) and closed-bar evaluation in both paths.",
            "Known benign sources: final-bar pending orders never fill in either path; "
            "policy-trip bookkeeping may timestamp one bar apart without changing fills.",
        ],
        live_notes=list(live.notes),
        paper_notes=list(paper["notes"]),
        extra=dict(extra or {}),
    )
