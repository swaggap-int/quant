"""Metrics are the headline; they get hand-computed expectations."""

from __future__ import annotations

import math
import unittest

import numpy as np

from quant.metrics import (
    annualised_return,
    annualised_vol,
    compute_metrics,
    drawdown_duration_bars,
    drawdown_series,
    max_drawdown,
    monthly_returns,
    returns_from_equity,
    sharpe,
    sortino,
)

YEAR_MS = 365 * 86_400_000


class TestDrawdown(unittest.TestCase):
    def test_max_drawdown_is_peak_to_trough(self) -> None:
        equity = np.array([100.0, 110.0, 99.0, 120.0, 60.0, 130.0])
        self.assertAlmostEqual(max_drawdown(equity), 60.0 / 120.0 - 1.0)

    def test_monotonic_rise_has_no_drawdown(self) -> None:
        self.assertAlmostEqual(max_drawdown(np.array([1.0, 2.0, 3.0])), 0.0)

    def test_drawdown_series_resets_at_new_highs(self) -> None:
        dd = drawdown_series(np.array([100.0, 90.0, 105.0, 100.0]))
        np.testing.assert_allclose(dd, [0.0, -0.1, 0.0, 100.0 / 105.0 - 1.0])

    def test_drawdown_duration_counts_the_longest_underwater_run(self) -> None:
        equity = np.array([100.0, 95.0, 96.0, 97.0, 101.0, 100.0])
        self.assertEqual(drawdown_duration_bars(equity), 3)


class TestReturns(unittest.TestCase):
    def test_returns_are_simple_period_returns(self) -> None:
        np.testing.assert_allclose(returns_from_equity(np.array([100.0, 110.0, 99.0])), [0.1, -0.1])

    def test_returns_of_a_short_curve_are_empty(self) -> None:
        self.assertEqual(len(returns_from_equity(np.array([100.0]))), 0)

    def test_zero_equity_does_not_produce_nan(self) -> None:
        out = returns_from_equity(np.array([0.0, 10.0, 20.0]))
        self.assertTrue(np.all(np.isfinite(out)))

    def test_cagr_doubles_over_one_year(self) -> None:
        self.assertAlmostEqual(annualised_return(np.array([100.0, 200.0]), 1.0), 1.0)

    def test_cagr_over_two_years_is_the_square_root(self) -> None:
        equity = np.array([100.0, 121.0])
        self.assertAlmostEqual(annualised_return(equity, 2.0), 0.1)

    def test_annualised_vol_scales_by_root_time(self) -> None:
        rets = np.array([0.01, -0.01, 0.01, -0.01])
        expected = float(np.std(rets, ddof=1) * math.sqrt(365))
        self.assertAlmostEqual(annualised_vol(rets, 365), expected)


class TestRiskAdjusted(unittest.TestCase):
    def test_sharpe_of_constant_returns_is_zero_not_infinite(self) -> None:
        self.assertEqual(sharpe(np.full(20, 0.01), 365), 0.0)

    def test_sharpe_matches_the_textbook_formula(self) -> None:
        rets = np.array([0.02, -0.01, 0.03, -0.02, 0.01])
        expected = float(np.mean(rets) / np.std(rets, ddof=1) * math.sqrt(365))
        self.assertAlmostEqual(sharpe(rets, 365), expected)

    def test_sortino_ignores_upside_volatility(self) -> None:
        good = np.array([0.05, 0.05, 0.05, -0.01])
        chop = np.array([0.05, -0.05, 0.05, -0.01])
        self.assertGreater(sortino(good, 365), sortino(chop, 365))

    def test_sortino_without_losses_is_zero(self) -> None:
        self.assertEqual(sortino(np.full(10, 0.01), 365), 0.0)


class TestComputeMetrics(unittest.TestCase):
    def setUp(self) -> None:
        self.ts = np.arange(365, dtype=np.int64) * 86_400_000 + 1_600_000_000_000
        self.equity = np.linspace(10_000.0, 12_000.0, 365)

    def metrics(self, **kw):
        return compute_metrics(
            self.equity,
            self.ts,
            initial_cash=10_000.0,
            periods_per_year=365.0,
            bar_ms=86_400_000,
            **kw,
        )

    def test_every_expected_key_is_present(self) -> None:
        m = self.metrics()
        for key in (
            "total_return", "cagr", "sharpe", "sortino", "max_drawdown", "calmar",
            "trades", "win_rate", "profit_factor", "expectancy", "fees_paid", "years",
        ):
            self.assertIn(key, m)

    def test_totals_are_consistent(self) -> None:
        m = self.metrics()
        self.assertAlmostEqual(m["total_return"], 0.2, places=6)
        self.assertAlmostEqual(m["final_equity"], 12_000.0)
        self.assertAlmostEqual(m["net_pnl"], 0.0)  # no trades supplied

    def test_profit_factor_uses_gross_win_over_gross_loss(self) -> None:
        trades = [{"pnl": 300.0}, {"pnl": -100.0}, {"pnl": -50.0}, {"pnl": 25.0}]
        m = self.metrics(trades=trades)
        self.assertAlmostEqual(m["profit_factor"], 325.0 / 150.0, places=6)
        self.assertAlmostEqual(m["win_rate"], 0.5)
        self.assertAlmostEqual(m["expectancy"], (325.0 - 150.0) / 4)
        self.assertEqual(m["trades"], 4)

    def test_infinite_profit_factor_is_reported_for_all_winners(self) -> None:
        m = self.metrics(trades=[{"pnl": 10.0}, {"pnl": 5.0}])
        self.assertEqual(m["profit_factor"], float("inf"))

    def test_fees_and_turnover_come_from_fills(self) -> None:
        fills = [{"fee": 12.0, "notional": 5_000.0}, {"fee": 8.0, "notional": 5_000.0}]
        m = self.metrics(fills=fills)
        self.assertAlmostEqual(m["fees_paid"], 20.0)
        self.assertAlmostEqual(m["fees_pct_of_start"], 0.2)
        self.assertAlmostEqual(m["turnover_multiple"], 10_000.0 / float(np.mean(self.equity)), places=6)

    def test_benchmark_comparison(self) -> None:
        bench = np.linspace(10_000.0, 15_000.0, 365)
        m = self.metrics(benchmark=bench)
        self.assertAlmostEqual(m["buy_hold_return_pct"], 50.0, places=6)
        self.assertAlmostEqual(m["excess_return_pct"], 20.0 - 50.0, places=6)

    def test_years_derives_from_bar_count_and_bar_size(self) -> None:
        m = self.metrics()
        self.assertAlmostEqual(m["years"], 365 * 86_400_000 / YEAR_MS, places=9)

    def test_monthly_returns_compound_to_the_total(self) -> None:
        rows = monthly_returns(self.ts, self.equity)
        compounded = 1.0
        for row in rows:
            compounded *= 1.0 + row["return_pct"] / 100.0
        self.assertAlmostEqual(compounded - 1.0, 0.2, places=6)


if __name__ == "__main__":
    unittest.main()
