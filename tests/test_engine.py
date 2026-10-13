"""Engine invariants: fill timing, no lookahead, and money that adds up."""

from __future__ import annotations

import unittest

import numpy as np

from quant.broker import Order
from quant.config import BacktestConfig, Costs, RiskConfig
from quant.engine import Engine
from quant.strategies import EmaAtrTrend
from tests.helpers import ScriptedStrategy, candles_from, flat_bars


def config(**kw) -> BacktestConfig:
    base = dict(symbol="TEST/USDT", timeframe="1d", initial_cash=10_000.0)
    base.update(kw)
    return BacktestConfig(**base)


class TestFillTiming(unittest.TestCase):
    def test_signal_on_a_close_fills_at_the_next_open(self) -> None:
        candles = candles_from(flat_bars(6))
        strategy = ScriptedStrategy({2: [Order("buy", qty=1.0, reason="test")]})
        result = Engine(config(costs=Costs(fee_bps=0, slippage_bps=10)), strategy).run(candles)
        self.assertEqual(len(result.fills), 1)
        fill = result.fills[0]
        self.assertEqual(fill["ts"], int(candles.ts[3]))  # next bar
        self.assertAlmostEqual(fill["price"], 100.0 * (1 + 10 / 10_000))  # 10 bps worse for the buyer
        self.assertNotEqual(fill["ts"], int(candles.ts[2]))

    def test_order_on_the_final_bar_never_fills(self) -> None:
        candles = candles_from(flat_bars(6))
        strategy = ScriptedStrategy({5: [Order("buy", qty=1.0)]})
        result = Engine(config(), strategy).run(candles)
        self.assertEqual(result.fills, [])
        self.assertTrue(any("never filled" in n for n in result.notes))

    def test_strategy_cannot_see_future_bars(self) -> None:
        candles = candles_from(flat_bars(10))
        strategy = ScriptedStrategy()
        Engine(config(), strategy).run(candles)
        self.assertEqual(strategy.seen, list(range(2, 10)))  # warmup is 2 bars
        for i, view in zip(strategy.seen, strategy.portfolio_views):
            self.assertLess(i, len(candles))

    def test_window_is_truncated_at_the_current_bar(self) -> None:
        candles = candles_from(flat_bars(8))
        captured: list[tuple[int, int]] = []

        class Peeker(ScriptedStrategy):
            def on_bar(self, w, portfolio):
                captured.append((w.i, len(w.close)))
                self.assert_close_visible(w)
                return []

            def assert_close_visible(self, w) -> None:  # pragma: no cover - assertion helper
                assert len(w.close) == w.i + 1

        Engine(config(), Peeker()).run(candles)
        self.assertTrue(all(length == i + 1 for i, length in captured))

    def test_indicator_views_are_also_truncated(self) -> None:
        candles = candles_from(flat_bars(8))
        lengths: list[tuple[int, int]] = []

        class IndPeeker(ScriptedStrategy):
            def prepare(self, candles):
                return {"dummy": np.arange(len(candles), dtype=float)}

            def on_bar(self, w, portfolio):
                lengths.append((w.i, len(w.ind("dummy"))))
                return []

        Engine(config(), IndPeeker()).run(candles)
        self.assertTrue(all(n == i + 1 for i, n in lengths))

    def test_unknown_indicator_name_raises(self) -> None:
        candles = candles_from(flat_bars(6))

        class BadInd(ScriptedStrategy):
            def on_bar(self, w, portfolio):
                return [] if w.ind("nope") is not None else []

        with self.assertRaises(KeyError):
            Engine(config(), BadInd()).run(candles)


class TestAccounting(unittest.TestCase):
    def random_walk(self, n: int = 400, seed: int = 42) -> object:
        rng = np.random.default_rng(seed)
        close = 100 * np.exp(np.cumsum(rng.normal(0.0004, 0.02, n)))
        high = close * (1 + np.abs(rng.normal(0, 0.008, n)))
        low = close * (1 - np.abs(rng.normal(0, 0.008, n)))
        rows = [(close[i - 1] if i else close[0], high[i], low[i], close[i], 10.0) for i in range(n)]
        return candles_from(rows)

    def test_cash_flow_identity_holds(self) -> None:
        candles = self.random_walk()
        cfg = config(costs=Costs(fee_bps=10, slippage_bps=5), risk=RiskConfig(risk_per_trade=0.02))
        result = Engine(cfg, EmaAtrTrend(fast=10, slow=30)).run(candles)

        buys = sum(f["notional"] for f in result.fills if f["side"] == "buy")
        sells = sum(f["notional"] for f in result.fills if f["side"] == "sell")
        fees = sum(f["fee"] for f in result.fills)
        self.assertAlmostEqual(result.cash[-1], cfg.initial_cash + sells - buys - fees, places=6)
        self.assertAlmostEqual(result.metrics["fees_paid"], fees, places=9)

    def test_equity_is_cash_plus_position_at_every_bar(self) -> None:
        candles = self.random_walk(seed=7)
        result = Engine(config(), EmaAtrTrend(fast=5, slow=20)).run(candles)
        self.assertEqual(len(result.equity), len(candles))
        self.assertTrue(np.all(np.isfinite(result.equity)))
        self.assertTrue(np.all(result.equity > 0))
        # Exposure is a fraction and can never be negative for a long-only run.
        self.assertTrue(np.all(result.exposure >= 0))
        self.assertTrue(np.all(result.exposure <= 1.0 + 1e-9))

    def test_every_trade_is_a_complete_round_trip(self) -> None:
        candles = self.random_walk(seed=99)
        result = Engine(config(), EmaAtrTrend(fast=8, slow=21)).run(candles)
        self.assertGreater(len(result.trades), 3)
        for trade in result.trades:
            self.assertGreater(trade["exit_ts"], trade["entry_ts"])
            self.assertGreater(trade["qty"], 0)
            self.assertIn(trade["side"], ("long", "short"))
            self.assertGreater(trade["bars_held"], 0)
        open_at_end = len(result.fills) % 2
        self.assertEqual(len(result.fills), 2 * len(result.trades) + open_at_end)

    def test_slippage_only_ever_makes_things_worse(self) -> None:
        candles = self.random_walk(seed=123)
        clean = Engine(
            config(costs=Costs(fee_bps=0, slippage_bps=0)), EmaAtrTrend(fast=10, slow=30)
        ).run(candles)
        dirty = Engine(
            config(costs=Costs(fee_bps=10, slippage_bps=10)), EmaAtrTrend(fast=10, slow=30)
        ).run(candles)
        self.assertLess(dirty.metrics["final_equity"], clean.metrics["final_equity"])
        self.assertEqual(clean.metrics["fees_paid"], 0.0)
        self.assertGreater(dirty.metrics["fees_paid"], 0.0)

    def test_runs_are_deterministic(self) -> None:
        candles = self.random_walk(seed=555)
        a = Engine(config(), EmaAtrTrend(fast=10, slow=30)).run(candles)
        b = Engine(config(), EmaAtrTrend(fast=10, slow=30)).run(candles)
        np.testing.assert_array_equal(a.equity, b.equity)
        self.assertEqual(a.trades, b.trades)

    def test_too_few_bars_is_an_error(self) -> None:
        with self.assertRaises(ValueError):
            Engine(config(), EmaAtrTrend()).run(candles_from(flat_bars(2)))


class TestResultSerialisation(unittest.TestCase):
    def test_to_dict_round_trips_through_json(self) -> None:
        import json

        candles = candles_from(flat_bars(40))
        strategy = ScriptedStrategy({2: [Order("buy", qty=1.0)]}, windows={"dummy": 3})
        result = Engine(config(), strategy).run(candles)
        payload = json.loads(json.dumps(result.to_dict(max_points=20), allow_nan=False))
        self.assertEqual(len(payload["series"]["equity"]), 20)
        self.assertEqual(payload["data"]["bars"], 40)
        self.assertEqual(len(payload["series"]["ts"]), len(payload["series"]["equity"]))
        self.assertEqual(len(payload["series"]["drawdown_pct"]), len(payload["series"]["equity"]))

    def test_save_writes_a_file(self) -> None:
        import tempfile
        from pathlib import Path

        candles = candles_from(flat_bars(20))
        result = Engine(config(), ScriptedStrategy()).run(candles)
        with tempfile.TemporaryDirectory() as tmp:
            path = result.save(Path(tmp) / "nested" / "run.json")
            self.assertTrue(path.exists())
            self.assertIn(result.run_id, path.read_text(encoding="utf-8"))

    def test_report_and_verdict_render(self) -> None:
        from quant.report import format_report, verdict

        candles = candles_from(flat_bars(30))
        result = Engine(config(), ScriptedStrategy()).run(candles)
        text = format_report(result)
        self.assertIn("RETURNS", text)
        self.assertIn("RISK", text)
        judgement = verdict(result.metrics)
        self.assertTrue(judgement)
        self.assertIn("trades", judgement)  # a no-trade run is called out


if __name__ == "__main__":
    unittest.main()
