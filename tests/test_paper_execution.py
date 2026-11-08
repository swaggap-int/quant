#!/usr/bin/env python
"""Milestone 3: paper execution layer on backtester fill semantics."""
from __future__ import annotations

import unittest

from quant.broker import Order
from quant.config import BacktestConfig, Costs
from quant.paper import PaperExecutionAdapter
from quant.live_runner import LiveRunPolicy, run_simulated_live
from tests.helpers import candles_from, flat_bars


def config(**kw):
    base = dict(symbol="TEST/USDT", timeframe="1d", initial_cash=10_000.0)
    base.update(kw)
    return BacktestConfig(**base)


class TestPaperAdapterFillModel(unittest.TestCase):
    def test_next_open_fill_with_fee_and_slippage(self):
        candles = candles_from(flat_bars(6))
        adapter = PaperExecutionAdapter(
            initial_cash=10_000.0, costs=Costs(fee_bps=0, slippage_bps=10)
        )
        # Replay bars 0-2 with no orders, decide on bar 2 close, fill at bar 3 open.
        for i in range(3):
            ts = int(candles.ts[i])
            adapter.process_bar(ts, float(candles.open[i]), float(candles.high[i]),
                                float(candles.low[i]), float(candles.close[i]))
        adapter.submit(Order("buy", qty=1.0, reason="paper-entry"))
        ts = int(candles.ts[3])
        adapter.process_bar(ts, float(candles.open[3]), float(candles.high[3]),
                            float(candles.low[3]), float(candles.close[3]))
        self.assertEqual(len(adapter.fills), 1)
        fill = adapter.fills[0]
        self.assertEqual(fill["ts"], int(candles.ts[3]))
        self.assertAlmostEqual(fill["price"], 100.0 * (1 + 10 / 10_000))

    def test_sizing_from_stop_distance(self):
        adapter = PaperExecutionAdapter(initial_cash=10_000.0)
        adapter.submit(Order("buy", risk_pct=0.01, stop=95.0, reason="risk-sized"))
        adapter.process_bar(1, 100.0, 101.0, 99.0, 100.0)
        self.assertEqual(len(adapter.fills), 1)
        # (0.01 * ~10000) / (100 - 95) ≈ 20 units
        self.assertGreater(adapter.fills[0]["qty"], 10.0)

    def test_stop_triggers_intrabar(self):
        adapter = PaperExecutionAdapter(initial_cash=10_000.0)
        adapter.submit(Order("buy", qty=1.0, stop=90.0, reason="with-stop"))
        adapter.process_bar(1, 100.0, 101.0, 99.0, 100.0)
        self.assertEqual(len(adapter.fills), 1)
        # Next bar trades through the stop.
        adapter.process_bar(2, 100.0, 100.0, 80.0, 85.0)
        self.assertEqual(len(adapter.trades), 1)
        self.assertIn("stop", adapter.trades[0]["exit_reason"])

    def test_account_state_shape(self):
        adapter = PaperExecutionAdapter(initial_cash=10_000.0)
        state = adapter.account_state(100.0)
        for field in ("cash", "qty", "side", "open_orders", "pending_stop",
                      "exposure", "equity", "n_trades", "n_fills"):
            self.assertTrue(hasattr(state, field), field)
        d = adapter.to_dict(100.0)
        self.assertIn("open_orders", d)
        self.assertIn("pending_stop", d)


class TestPaperPolicyGuards(unittest.TestCase):
    def test_exposure_guard_blocks(self):
        candles = candles_from(flat_bars(8))
        script = {2: [Order("buy", qty=1.0, reason="first")],
                  3: [Order("buy", qty=1.0, reason="second")]}
        from tests.helpers import ScriptedStrategy
        policy = LiveRunPolicy(max_exposure_pct=0.0)
        outcome = run_simulated_live(candles, ScriptedStrategy(script, windows={}),
                                     config(), run_policy=policy)
        self.assertEqual(len(outcome.fills), 1)
        self.assertTrue(any("exposure guard" in t.get("reason", "") for t in outcome.policy_trips))

    def test_duplicate_blocked_same_bar(self):
        candles = candles_from(flat_bars(8))
        dup = [Order("buy", qty=1.0, reason="same"), Order("buy", qty=1.0, reason="same")]
        from tests.helpers import ScriptedStrategy
        outcome = run_simulated_live(candles, ScriptedStrategy({2: dup}, windows={}), config())
        self.assertEqual(len(outcome.fills), 1)
        self.assertTrue(any("duplicate" in t.get("reason", "") for t in outcome.policy_trips))

    def test_rate_limit_per_bar(self):
        candles = candles_from(flat_bars(8))
        two = [Order("buy", qty=1.0, reason="a"), Order("buy", qty=2.0, reason="b")]
        from tests.helpers import ScriptedStrategy
        policy = LiveRunPolicy(max_orders_per_bar=1)
        outcome = run_simulated_live(candles, ScriptedStrategy({2: two}, windows={}),
                                     config(), run_policy=policy)
        self.assertTrue(any("rate limit" in t.get("reason", "") for t in outcome.policy_trips))

    def test_rate_limit_per_run(self):
        candles = candles_from(flat_bars(10))
        script = {2: [Order("buy", qty=1.0, reason="one")],
                  4: [Order("sell", qty=1.0, reason="two")]}
        from tests.helpers import ScriptedStrategy
        policy = LiveRunPolicy(max_orders_per_run=1)
        outcome = run_simulated_live(candles, ScriptedStrategy(script, windows={}),
                                     config(), run_policy=policy)
        self.assertTrue(any("rate limit" in t.get("reason", "") for t in outcome.policy_trips))

    def test_kill_switch_stops_trading(self):
        candles = candles_from(flat_bars(8))
        from tests.helpers import ScriptedStrategy
        from quant.live_runner import LiveRunner
        runner = LiveRunner(config(), ScriptedStrategy({2: [Order("buy", qty=1.0)]}, windows={}))
        runner.kill()
        self.assertFalse(runner.is_alive())
        outcome = runner.run(candles)
        self.assertEqual(outcome.fills, [])


if __name__ == "__main__":
    unittest.main()
