#!/usr/bin/env python
"""Milestone 6: structured logs, traces, alerts, live paper ops view."""
from __future__ import annotations

import unittest

from quant.broker import Order
from quant.exchange import PaperExchangeAdapter
from quant.live_runner import LiveRunPolicy, run_simulated_live
from quant.observability import (
    LiveLogger,
    build_ops_view,
    build_trace,
    evaluate_alerts,
)
from tests.helpers import ScriptedStrategy, candles_from, flat_bars


def config(**kw):
    from quant.config import BacktestConfig
    base = dict(symbol="TEST/USDT", timeframe="1d", initial_cash=10_000.0)
    base.update(kw)
    return BacktestConfig(**base)


class TestStructuredLogs(unittest.TestCase):
    def test_decision_order_fill_logged(self):
        candles = candles_from(flat_bars(8))
        logger = LiveLogger()
        outcome = run_simulated_live(candles, ScriptedStrategy({2: [Order("buy", qty=1.0, reason="sig")]}, windows={}),
                                     config(), logger=logger)
        self.assertEqual(len(outcome.fills), 1)
        kinds = {e.kind for e in logger.query()}
        self.assertIn("decision", kinds)
        self.assertIn("order", kinds)
        self.assertIn("fill", kinds)
        recs = logger.to_list()
        self.assertTrue(all({"seq", "ts_ms", "level", "kind", "message"} <= set(r) for r in recs))

    def test_guard_trip_logged(self):
        candles = candles_from(flat_bars(8))
        logger = LiveLogger()
        outcome = run_simulated_live(candles, ScriptedStrategy({2: [Order("buy", qty=9_000.0)]}, windows={}),
                                     config(), run_policy=LiveRunPolicy(max_position_qty=1.0), logger=logger)
        self.assertTrue(logger.count("guard_trip") >= 1)
        self.assertGreaterEqual(len(outcome.policy_trips), 1)

    def test_strategy_error_does_not_kill_run(self):
        candles = candles_from(flat_bars(8))

        class Exploder(ScriptedStrategy):
            def on_bar(self, w, portfolio):
                if w.i == 3:
                    raise RuntimeError("boom")
                return []

        logger = LiveLogger()
        outcome = run_simulated_live(candles, Exploder(windows={}), config(), logger=logger)
        self.assertEqual(outcome.bars_seen, len(candles))
        self.assertEqual(outcome.strategy_errors, 1)
        self.assertEqual(logger.count("error"), 1)


class TestTracesAndAlerts(unittest.TestCase):
    def test_trace_is_measurable(self):
        candles = candles_from(flat_bars(8))
        logger = LiveLogger()
        outcome = run_simulated_live(candles, ScriptedStrategy({2: [Order("buy", qty=1.0, reason="x")]}, windows={}),
                                     config(), logger=logger)
        trace = build_trace(outcome, logger)
        self.assertEqual(trace.bars_processed, 8)
        self.assertEqual(trace.orders_placed, 1)
        self.assertEqual(trace.orders_filled, 1)
        self.assertEqual(trace.errors, 0)

    def test_alerts_fire_on_thresholds(self):
        candles = candles_from(flat_bars(8))
        logger = LiveLogger()
        outcome = run_simulated_live(candles, ScriptedStrategy({2: [Order("buy", qty=9_000.0)]}, windows={}),
                                     config(), run_policy=LiveRunPolicy(max_position_qty=1.0), logger=logger)
        trace = build_trace(outcome, logger)
        alerts = evaluate_alerts(trace, [], max_guard_trips=1)
        self.assertTrue(any(a["name"] == "guard-trips" for a in alerts))

    def test_state_mismatch_and_downtime_alert(self):
        from quant.observability import RunTrace
        trace = RunTrace(bars_processed=10, downtime_bars=9)
        alerts = evaluate_alerts(trace, ["open-order count mismatch: local=1 venue-pending=0"])
        names = {a["name"] for a in alerts}
        self.assertIn("state-mismatch", names)
        self.assertIn("process-health", names)

    def test_venue_retry_logged(self):
        logger = LiveLogger()
        ex = PaperExchangeAdapter(logger=logger)
        ex.connect()
        ex.fail_plan["place"] = 1
        oid = ex.place_order_with_retry(Order("buy", qty=1.0, reason="r"), max_attempts=2,
                                        sleep_fn=lambda s: None)
        self.assertTrue(oid)
        self.assertGreaterEqual(logger.count("retry"), 1)


class TestOpsView(unittest.TestCase):
    def test_ops_view_is_separate_from_dashboard(self):
        from quant.engine import Engine
        candles = candles_from(flat_bars(12))
        logger = LiveLogger()
        outcome = run_simulated_live(candles, ScriptedStrategy({2: [Order("buy", qty=1.0, reason="x")]}, windows={}),
                                     config(), logger=logger)
        trace = build_trace(outcome, logger)
        alerts = evaluate_alerts(trace, [])
        view = build_ops_view(run_id=outcome.run_id, trace=trace, alerts=alerts,
                              account={"cash": 9000.0}, equity_tail=outcome.equity_series(5),
                              policy_trips=outcome.policy_trips,
                              recent_events=logger.to_list(), consistency=[], killed=False)
        self.assertEqual(view["mode"], "live_paper_ops")
        self.assertIn(view["health"], ("ok", "degraded", "halted"))
        for key in ("trace", "alerts", "account", "equity_tail", "policy_trips", "recent_events", "consistency"):
            self.assertIn(key, view)
        # Historical dashboard shape is different.
        hist = Engine(config(), ScriptedStrategy(windows={})).run(candles).to_dict(max_points=5)
        self.assertNotEqual(hist.get("mode"), "live_paper_ops")
        self.assertIn("series", hist)


if __name__ == "__main__":
    unittest.main()
