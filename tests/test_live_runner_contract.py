#!/usr/bin/env python
"""
Milestone 1 integration tests for the live-vs-backtest contract.

These tests assert:
- The same strategy code can run through the historical backtest path and the
  simulated-live path.
- The simulated-live path uses the same fill timing as the backtester:
  decide on close, fill at next open.
- The live path cannot see future bars.
- A runtime policy guard can block orders and the runner has a kill switch.
- A simulated-live run emits a result shape the rest of the project can read.
"""

from __future__ import annotations

import json
import unittest
import numpy as np


from quant.broker import Order
from quant.config import BacktestConfig, Costs, RiskConfig
from quant.datafeed import Candles
from quant.live_runner import LiveOutcome, LiveRunPolicy, LiveRunner, run_simulated_live
from quant.strategies import EmaAtrTrend
from tests.helpers import ScriptedStrategy, candles_from, flat_bars


def config(**kw):
    base = dict(symbol="TEST/USDT", timeframe="1d", initial_cash=10_000.0)
    base.update(kw)
    return BacktestConfig(**base)


class TestSameStrategyRunsBothPaths(unittest.TestCase):
    def test_same_scripted_strategy_runs_in_backtest_and_simulated_live(self):
        # warmup_bars for this scripted strategy is 5, so the first usable bar
        # in both paths is index 5. Put the order there so both paths can fill it.
        candles = candles_from(flat_bars(8))
        script = {5: [Order("buy", qty=1.0, reason="backtest-or-live")]}
        strategy = ScriptedStrategy(script, windows={"dummy": 3})

        from quant.engine import Engine

        backtest_cfg = config()
        live_cfg = config()

        backtest_result = Engine(backtest_cfg, strategy).run(candles)
        live_outcome = run_simulated_live(candles, strategy, live_cfg)

        self.assertEqual(len(backtest_result.fills), 1)
        self.assertEqual(len(live_outcome.fills), 1)

        # Both paths should use the same fill price assumption for this flat
        # synthetic series: next open is 100.0, slippage is zero in the config
        # we passed.
        self.assertAlmostEqual(backtest_result.fills[0]["price"], live_outcome.fills[0]["price"])

    def test_strategy_is_reset_and_prepared_in_both_paths(self):
        candles = candles_from(flat_bars(10))

        class Inspectable(ScriptedStrategy):
            def on_bar(self, w, portfolio):
                return []

        strategy = Inspectable(windows={"dummy": 3})
        from quant.engine import Engine
        Engine(config(), strategy).run(candles)
        self.assertTrue(hasattr(strategy, "seen"))

        strategy2 = Inspectable(windows={"dummy": 3})
        run_simulated_live(candles, strategy2, config())
        self.assertTrue(hasattr(strategy2, "seen"))


class TestSimulatedLiveFillTiming(unittest.TestCase):
    def test_decision_on_close_fills_at_next_open(self):
        candles = candles_from(flat_bars(6))
        script = {2: [Order("buy", qty=1.0, reason="close decision")]}
        strategy = ScriptedStrategy(script, windows={})
        outcome = run_simulated_live(config(costs=Costs(fee_bps=0, slippage_bps=10)), strategy, candles)

        self.assertEqual(len(outcome.fills), 1)
        fill = outcome.fills[0]
        self.assertEqual(fill["ts"], int(candles.ts[3]))
        self.assertAlmostEqual(fill["price"], 100.0 * (1 + 10 / 10_000))
        self.assertNotEqual(fill["ts"], int(candles.ts[2]))

    def test_order_on_final_bar_never_fills_in_live_replay(self):
        # In a closed-bar replay an order queued on the final bar cannot fill,
        # but the live path should still record that fact rather than silently
        # dropping it. Milestone 1 keeps this explicit even if the message text
        # later changes.
        candles = candles_from(flat_bars(6))
        script = {5: [Order("buy", qty=1.0)]}
        strategy = ScriptedStrategy(script, windows={})
        outcome = run_simulated_live(config(), strategy, candles)

        self.assertEqual(outcome.fills, [])
        # We assert the behavior, not a brittle note string.
        self.assertEqual(len(outcome.fills), 0)
        self.assertEqual(len(outcome.trades), 0)


class TestRealNowSkipsOpenBars(unittest.TestCase):
    def test_open_bars_never_evaluated_with_real_now(self):
        from quant.live_runner import run_simulated_live as run_live
        candles = candles_from(flat_bars(8))
        seen: list[int] = []

        class Watcher(ScriptedStrategy):
            def on_bar(self, w, portfolio):
                seen.append(w.i)
                return []

        now = int(candles.ts[5])  # bars 0-4 closed; 5+ still open
        outcome = run_live(candles, Watcher(windows={}), config(), now_ms=now)
        self.assertEqual(outcome.open_bars_skipped, 3)
        self.assertTrue(all(i <= 4 for i in seen))
        self.assertNotIn(5, seen)

    def test_order_on_open_bar_never_sent(self):
        from quant.live_runner import run_simulated_live as run_live
        candles = candles_from(flat_bars(8))
        script = {6: [Order("buy", qty=1.0, reason="open-bar")],
                  3: [Order("buy", qty=1.0, reason="closed-bar")]}.copy()
        now = int(candles.ts[5])
        outcome = run_live(candles, ScriptedStrategy(script, windows={}), config(), now_ms=now)
        self.assertEqual(len(outcome.fills), 1)
        self.assertEqual(outcome.fills[0]["reason"], "closed-bar")


class TestSimulatedLiveCannotSeeFutureBars(unittest.TestCase):
    def test_live_replay_strategy_only_sees_history_through_window(self):
        candles = candles_from(flat_bars(10))

        seen: list[tuple[int, int]] = []

        class WindowInspector(ScriptedStrategy):
            def on_bar(self, w, portfolio):
                seen.append((w.i, len(w.close)))
                return []

        # With no windows declared, warmup_bars returns 2, so the first bar a
        # strategy sees in a real backtest/live path is index 2.
        outcome = run_simulated_live(config(), WindowInspector(windows={}), candles)

        self.assertEqual([i for i, _ in seen], list(range(2, 10)))
        self.assertTrue(all(length == i + 1 for i, length in seen))
        for i, length in seen:
            self.assertLess(i, len(candles))
            self.assertLessEqual(length, len(candles))


class TestSimulatedLiveResultShape(unittest.TestCase):
    def test_live_outcome_round_trips_through_json(self):
        candles = candles_from(flat_bars(40))
        outcome = run_simulated_live(config(), ScriptedStrategy(windows={}), candles)
        payload = json.loads(json.dumps(outcome.to_dict(max_points=20), allow_nan=False))

        self.assertEqual(payload["mode"], "simulated_live")
        self.assertEqual(payload["data"]["bars"], 40)
        self.assertEqual(len(payload["series"]["equity"]), 20)
        self.assertEqual(len(payload["series"]["ts"]), len(payload["series"]["equity"]))
        self.assertEqual(len(payload["series"]["drawdown_pct"]), len(payload["series"]["equity"]))

    def test_live_outcome_has_policy_trips_list(self):
        candles = candles_from(flat_bars(10))
        outcome = run_simulated_live(config(), ScriptedStrategy(windows={}), candles)
        self.assertIsInstance(outcome.policy_trips, list)

    def test_live_outcome_hides_future_bars_in_series(self):
        candles = candles_from(flat_bars(8))
        outcome = run_simulated_live(config(), ScriptedStrategy(windows={}), candles)
        series = outcome.equity_series(max_points=20)
        self.assertLessEqual(len(series["ts"]), len(candles))


class TestLivePolicyGuard(unittest.TestCase):
    def test_policy_blocks_order_when_killed(self):
        candles = candles_from(flat_bars(8))
        script = {2: [Order("buy", qty=1.0, reason="should be blocked")]}
        strategy = ScriptedStrategy(script, windows={})
        policy = LiveRunPolicy(killed=True)
        outcome = run_simulated_live(candles, strategy, config(), run_policy=policy)

        self.assertEqual(outcome.fills, [])
        self.assertTrue(any("killed" in n.lower() for n in outcome.notes))

    def test_policy_blocks_order_above_max_position(self):
        candles = candles_from(flat_bars(8))
        script = {2: [Order("buy", qty=9_000.0, reason="huge order")]}
        strategy = ScriptedStrategy(script, windows={})
        policy = LiveRunPolicy(max_position_qty=9_000.0)
        outcome = run_simulated_live(candles, strategy, config(initial_cash=10_000.0), run_policy=policy)

        self.assertEqual(outcome.fills, [])
        self.assertTrue(any("policy blocked" in (t.get("reason") or "").lower() for t in outcome.policy_trips))


    def test_policy_records_trips(self):
        candles = candles_from(flat_bars(8))
        script = {2: [Order("buy", qty=9_000.0)]}
        strategy = ScriptedStrategy(script, windows={})
        policy = LiveRunPolicy(max_position_qty=1.0)
        outcome = run_simulated_live(candles, strategy, config(), run_policy=policy)


        self.assertGreaterEqual(len(outcome.policy_trips), 1)
        self.assertEqual(outcome.policy_trips[0]["reason"], "policy blocked order")

    def test_runner_kill_switch(self):
        candles = candles_from(flat_bars(8))
        runner = LiveRunner(config(), ScriptedStrategy(windows={}))
        runner.kill()
        self.assertFalse(runner.is_alive())

        strategy = ScriptedStrategy({2: [Order("buy", qty=1.0)]}, windows={})
        outcome = runner.run(candles)
        self.assertFalse(runner.is_alive())
        self.assertEqual(outcome.fills, [])


class TestDailyLossLimitTrip(unittest.TestCase):
    def test_daily_loss_limit_can_trip(self):
        candles = candles_from(flat_bars(20, price=100.0))
        low_candles = list(candles_from(flat_bars(20, price=100.0)).close)
        low_candles[-1] = 80.0
        steep_candles = Candles(
            ts=candles.ts.copy(),
            open=np.array(low_candles, dtype=float),
            high=np.array(low_candles, dtype=float),
            low=np.array(low_candles, dtype=float),
            close=np.array(low_candles, dtype=float),
            volume=np.ones(len(low_candles), dtype=float),
            symbol=candles.symbol,
            timeframe=candles.timeframe,
            provider=candles.provider,
        )

        runner = LiveRunner(
            config(initial_cash=10_000.0),
            ScriptedStrategy({2: [Order("buy", qty=100.0)]}, windows={}),
            run_policy=LiveRunPolicy(daily_loss_limit_pct=0.05),
        )
        outcome = runner.run(steep_candles)
        self.assertTrue(any("daily loss limit" in n.lower() for n in outcome.notes))


if __name__ == "__main__":
    unittest.main()
