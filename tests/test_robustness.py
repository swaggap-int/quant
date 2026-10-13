"""Robustness-sweep invariants: the aggregate must not flatter the strategy.

What must never break here is the honesty of the arithmetic. A cell counts as
good only if it traded enough *and* beat buy & hold; the luck test must compare
against a genuinely comparable random schedule (same trade count, holding times,
sizing, stops, costs); and the selection adjustment must be the expected maximum
of the null distribution rather than a guess.
"""

from __future__ import annotations

import json
import math
import random
import unittest

from quant.config import BacktestConfig, Costs
from quant.datafeed import DataError
from quant.robustness import (
    BASELINE_PARAMS,
    Market,
    _p_value,
    NullResult,
    RandomEntryStrategy,
    SweepCell,
    SweepReport,
    default_param_grid,
    expected_max_of_draws,
    format_sweep_report,
    make_strategy,
    null_test,
    random_schedule,
    run_study,
    run_sweep,
    short_params,
    small_param_grid,
    summarize,
)
from quant.strategies.base import BarWindow
from tests.helpers import candles_from, flat_bars

TREND_DAYS = 600


def wave_bars(n: int = TREND_DAYS, *, start: float = 100.0, amp: float = 12.0, period: float = 90.0) -> list:
    """A trending sine wave: enough swings for a crossover system to trade."""
    rows = []
    for i in range(n):
        px = start + amp * math.sin(2 * math.pi * i / period) + i * 0.06
        rows.append((px, px * 1.012, px * 0.988, px, 1_000.0))
    return rows


def market(symbol: str = "TEST/USDT", timeframe: str = "1d") -> Market:
    return Market(symbol, timeframe)


def candles(m: Market, n: int = TREND_DAYS):
    return candles_from(wave_bars(n), symbol=m.symbol, timeframe=m.timeframe)


def engine_run(bars, schedule, *, stop_mult: float = 2.5):
    """Run a random-timing schedule through the real engine."""
    from quant.engine import Engine

    strategy = RandomEntryStrategy(schedule, stop_mult=stop_mult, risk_pct=0.02)
    return Engine(config(symbol=bars.symbol, timeframe=bars.timeframe), strategy).run(bars)


def config(**kw) -> BacktestConfig:
    base = dict(symbol="TEST/USDT", timeframe="1d", costs=Costs(fee_bps=10.0, slippage_bps=5.0))
    base.update(kw)
    return BacktestConfig(**base)


def metrics(excess: float = 5.0, trades: int = 30, total: float = 55.0, mdd: float = -0.10, pf: float = 1.5) -> dict:
    """A metrics dict shaped like the real one, for aggregation tests."""
    return {
        "total_return_pct": total,
        "buy_hold_return_pct": total - excess,
        "excess_return_pct": excess,
        "sharpe": 0.5,
        "max_drawdown": mdd,
        "max_drawdown_pct": mdd * 100.0,
        "profit_factor": pf,
        "trades": trades,
        "win_rate_pct": 50.0,
        "bars_in_market_pct": 20.0,
        "fees_pct_of_start": 1.0,
    }


class TestRandomSchedule(unittest.TestCase):
    def test_is_deterministic_for_a_seed(self) -> None:
        a = random_schedule(500, start_bar=50, n_trades=8, holding_bars=[5, 9, 20], rng=random.Random(3))
        b = random_schedule(500, start_bar=50, n_trades=8, holding_bars=[5, 9, 20], rng=random.Random(3))
        self.assertEqual(a, b)
        c = random_schedule(500, start_bar=50, n_trades=8, holding_bars=[5, 9, 20], rng=random.Random(4))
        self.assertNotEqual(a, c)

    def test_places_exactly_the_requested_trade_count(self) -> None:
        schedule = random_schedule(1000, start_bar=60, n_trades=12, holding_bars=[10], rng=random.Random(1))
        self.assertEqual(len(schedule), 12)

    def test_entries_never_overlap_and_stay_in_range(self) -> None:
        schedule = random_schedule(400, start_bar=20, n_trades=9, holding_bars=[30], rng=random.Random(11))
        previous_exit = -1
        for entry, exit_bar in schedule:
            self.assertGreaterEqual(entry, 20)
            self.assertGreater(exit_bar, entry)
            self.assertLessEqual(exit_bar, 398)  # leaves room for the exit fill
            self.assertGreater(entry, previous_exit)  # one flat bar between trades
            previous_exit = exit_bar

    def test_returns_nothing_when_there_is_no_room(self) -> None:
        self.assertEqual(random_schedule(10, start_bar=50, n_trades=5, holding_bars=[3], rng=random.Random(0)), [])
        self.assertEqual(random_schedule(500, start_bar=50, n_trades=0, holding_bars=[3], rng=random.Random(0)), [])


class TestRandomEntryStrategy(unittest.TestCase):
    def test_runs_through_the_engine_and_pays_costs(self) -> None:
        m = market()
        bars = candles(m)
        schedule = random_schedule(len(bars), start_bar=45, n_trades=6, holding_bars=[20], rng=random.Random(2))
        result = engine_run(bars, schedule)
        self.assertGreaterEqual(len(result.trades), 4)
        self.assertGreater(result.metrics["fees_paid"], 0.0)

    def test_an_empty_schedule_never_trades(self) -> None:
        bars = candles(market())
        result = engine_run(bars, [])
        self.assertEqual(result.trades, [])

    def test_advances_past_a_slot_the_position_lost_early(self) -> None:
        """A trade closed before its scheduled exit must not block later entries."""
        bars = candles(market(), n=300)
        strategy = RandomEntryStrategy([(60, 200), (220, 260)], stop_mult=2.5)
        indicators = strategy.prepare(bars)

        def flat(i: int):
            return strategy.on_bar(BarWindow(bars, indicators, i), {"position": "flat"})

        self.assertEqual(flat(60)[0].action, "buy")
        # Stop took the position out; the strategy is handed "flat" again at 80.
        self.assertEqual(flat(80), [])
        # The later slot still fires rather than being stranded behind the old one.
        self.assertEqual(flat(220)[0].action, "buy")

    def test_two_scheduled_trades_both_complete(self) -> None:
        bars = candles(market(), n=300)
        result = engine_run(bars, [(60, 70), (220, 260)], stop_mult=2.5)
        self.assertEqual(len(result.trades), 2)
        self.assertTrue(all(t["bars_held"] > 0 for t in result.trades))


class TestSelectionAdjustment(unittest.TestCase):
    def test_single_draw_is_the_sample_mean(self) -> None:
        samples = [-10.0, -2.5, 0.0, 4.0, 33.0]
        self.assertAlmostEqual(expected_max_of_draws(samples, 1), sum(samples) / len(samples))

    def test_two_draws_from_a_two_point_distribution(self) -> None:
        # E[max of two uniform draws from {0, 1}] = 3/4, by hand.
        self.assertAlmostEqual(expected_max_of_draws([0.0, 1.0], 2), 0.75)

    def test_is_increasing_in_the_number_of_draws(self) -> None:
        samples = [-20.0, -5.0, 3.0, 11.0, 40.0]
        values = [expected_max_of_draws(samples, g) for g in (1, 2, 5, 20)]
        self.assertEqual(values, sorted(values))

    def test_a_flat_distribution_has_no_best_of(self) -> None:
        self.assertAlmostEqual(expected_max_of_draws([7.0] * 10, 25), 7.0)

    def test_empty_input_is_not_a_number(self) -> None:
        self.assertTrue(math.isnan(expected_max_of_draws([], 5)))


class TestSummarize(unittest.TestCase):
    def build(self) -> list[SweepCell]:
        m = market("BTC/USDT", "1d")
        return [
            SweepCell(m, dict(BASELINE_PARAMS), metrics=metrics(excess=20.0)),
            SweepCell(m, {"fast": 10, "slow": 40}, metrics=metrics(excess=9.0)),
            SweepCell(m, {"fast": 30, "slow": 60}, metrics=metrics(excess=-14.0, total=-4.0)),
            SweepCell(m, {"fast": 5, "slow": 90}, metrics=metrics(excess=60.0, trades=3)),
            SweepCell(m, {"fast": 8, "slow": 21}, error="no data for BTC/USDT 1d"),
        ]

    def test_counts_good_cells_only_when_they_traded_enough(self) -> None:
        s = summarize(self.build(), grid_size=5, min_trades=15)
        self.assertEqual(s["planned"], 5)
        self.assertEqual(s["ran"], 4)
        self.assertEqual(s["errored"], 1)
        # The +60% cell is excluded: three trades proves nothing.
        self.assertEqual(s["beat_buy_hold"], 2)
        self.assertAlmostEqual(s["beat_buy_hold_pct"], 50.0)

    def test_losing_cells_are_counted_against_the_strategy(self) -> None:
        s = summarize(self.build(), grid_size=5, min_trades=15)
        self.assertEqual(s["positive"], 3)
        self.assertAlmostEqual(s["positive_pct"], 75.0)

    def test_best_cell_concentration_is_measured(self) -> None:
        s = summarize(self.build(), grid_size=5, min_trades=15)
        # Positive excess is 20 + 9 + 60 = 89; the best (60, under-traded) cell is 67% of it.
        self.assertAlmostEqual(s["best_share_pct"], 60.0 / 89.0 * 100.0, places=6)
        self.assertEqual(s["best"]["excess_pct"], 60.0)

    def test_clean_uses_the_same_verdict_rulebook_as_a_single_run(self) -> None:
        s = summarize(
            [
                SweepCell(market(), dict(BASELINE_PARAMS), metrics=metrics()),
                SweepCell(market(), {"fast": 9}, metrics=metrics(mdd=-0.72)),  # 72% drawdown
            ],
            grid_size=2,
        )
        self.assertEqual(s["clean"], 1)

    def test_verdict_says_the_result_mostly_lost_when_it_did(self) -> None:
        lousy = [SweepCell(market(), dict(BASELINE_PARAMS), metrics=metrics(excess=-9.0)) for _ in range(6)]
        s = summarize(lousy, grid_size=6)
        self.assertIn("only 0%", s["verdict"])

    def test_verdict_says_breadth_when_the_neighbourhood_works(self) -> None:
        solid = [SweepCell(market(), dict(BASELINE_PARAMS), metrics=metrics(excess=4.0)) for _ in range(6)]
        s = summarize(solid, grid_size=6)
        self.assertIn("breadth rather than a single lucky cell", s["verdict"])

    def test_verdict_reports_the_luck_test_verdict(self) -> None:
        cells = [SweepCell(market(), dict(BASELINE_PARAMS), metrics=metrics())]
        nulls = [NullResult(market(), dict(BASELINE_PARAMS), 55.0, 30, [1.0] * 199 + [99.0], 0.005)]
        s = summarize(cells, nulls, grid_size=1)
        self.assertIn("timing carries information", s["verdict"])

        nulls = [NullResult(market(), dict(BASELINE_PARAMS), 55.0, 30, [1.0] * 200, 1.0)]
        s = summarize(cells, nulls, grid_size=1)
        self.assertIn("not adding anything", s["verdict"])

    def test_a_lucky_looking_winner_is_called_out_even_when_breadth_is_good(self) -> None:
        cells = [SweepCell(market(), dict(BASELINE_PARAMS), metrics=metrics(excess=4.0)) for _ in range(6)]
        nulls = [NullResult(market(), dict(BASELINE_PARAMS), 55.0, 30, [1.0] * 200, 0.9)]
        verdict_text = summarize(cells, nulls, grid_size=6)["verdict"]
        self.assertIn("looks like exposure rather than signal", verdict_text)

    def test_opposite_readings_are_reconciled_not_papered_over(self) -> None:
        """Timing that beats random entries can still lose to buy & hold."""
        lousy = [SweepCell(market(), dict(BASELINE_PARAMS), metrics=metrics(excess=-30.0)) for _ in range(6)]
        nulls = [NullResult(market(), dict(BASELINE_PARAMS), 20.0, 30, [1.0] * 200, 0.02)]
        verdict_text = summarize(lousy, nulls, grid_size=6)["verdict"]
        self.assertIn("timing carries information", verdict_text)
        self.assertIn("Both readings hold at once", verdict_text)
        self.assertIn("trails buy & hold", verdict_text)

    def test_missing_luck_test_is_stated_as_a_gap(self) -> None:
        cells = [SweepCell(market(), dict(BASELINE_PARAMS), metrics=metrics())]
        self.assertIn("nothing here rules out luck", summarize(cells, [], grid_size=1)["verdict"])

    def test_by_market_rollup_carries_the_null_columns(self) -> None:
        m = market()
        cells = [SweepCell(m, dict(BASELINE_PARAMS), metrics=metrics())]
        nulls = [NullResult(m, dict(BASELINE_PARAMS), 12.0, 7, [5.0, 9.0, 30.0], 0.25)]
        row = summarize(cells, nulls, grid_size=1)["by_market"][0]
        self.assertEqual(row["market"], "TEST/USDT 1d")
        self.assertEqual(row["actual_return_pct"], 12.0)
        self.assertAlmostEqual(row["null_median_pct"], 9.0)
        self.assertAlmostEqual(row["p_value"], 0.25)


class TestSweep(unittest.TestCase):
    def two_market_loader(self):
        calls: list[str] = []
        bars = {m.label: candles(m, n=300) for m in (market("AAA/USDT"), market("BBB/USDT", "4h"))}

        def loader(m: Market):
            calls.append(m.label)
            return bars[m.label]

        return loader, calls

    def test_loader_runs_once_per_market_not_once_per_cell(self) -> None:
        loader, calls = self.two_market_loader()
        markets = [market("AAA/USDT"), market("BBB/USDT", "4h")]
        grid = small_param_grid()
        report = run_sweep(markets, grid, base_config=config(), loader=loader)
        self.assertEqual(calls, ["AAA/USDT 1d", "BBB/USDT 4h"])
        self.assertEqual(len(report.cells), len(markets) * len(grid))
        self.assertTrue(all(c.ok for c in report.cells))
        self.assertIn("AAA/USDT 1d", report.data)

    def test_repeated_failures_are_reported_once_per_market(self) -> None:
        repeated = [
            SweepCell(market(), {"fast": f}, error="no provider could serve TEST/USDT 1d")
            for f in (5, 9, 13)
        ]
        summary = summarize(repeated, grid_size=3)
        self.assertEqual(len(summary["errors"]), 1)
        self.assertEqual(summary["errors"][0]["cells"], 3)
        text = format_sweep_report(
            SweepReport(
                cells=repeated, nulls=[], grid_size=3,
                window={"start": None, "end": None, "provider": "auto", "fee_bps": 10.0, "slippage_bps": 5.0},
                summary=summary,
            )
        )
        self.assertIn("(3 settings)", text)
        # Nothing ran, so the empty stat blocks would be noise: just say why.
        self.assertNotIn("HOW OFTEN WAS IT GOOD?", text)
        self.assertIn("DATA ERRORS", text)
        self.assertIn("Nothing ran", text)

    def test_a_market_that_cannot_load_does_not_sink_the_sweep(self) -> None:
        def loader(m: Market):
            if m.symbol == "BBB/USDT":
                raise DataError("no candles returned for BBB/USDT")
            return candles(m, n=300)

        report = run_sweep(
            [market("AAA/USDT"), market("BBB/USDT")], small_param_grid(),
            base_config=config(), loader=loader,
        )
        broken = [c for c in report.cells if not c.ok]
        self.assertEqual(len(broken), len(small_param_grid()))
        self.assertIn("no candles returned", broken[0].error)
        self.assertEqual(report.summary["ran"], len(small_param_grid()))
        self.assertEqual(report.summary["errored"], len(small_param_grid()))

    def test_the_study_is_json_serialisable_even_with_infinities(self) -> None:
        loader, _ = self.two_market_loader()
        report = run_sweep([market("AAA/USDT")], small_param_grid(), base_config=config(), loader=loader)
        # No losing trades means profit_factor is inf; JSON must still be writable.
        report.cells[0].metrics["profit_factor"] = float("inf")
        report.cells[0].metrics["sharpe"] = float("nan")
        payload = json.loads(json.dumps(report.to_dict(), allow_nan=False))
        self.assertEqual(payload["summary"]["ran"], len(small_param_grid()))
        self.assertIsNone(payload["cells"][0]["result"]["profit_factor"])  # inf -> null
        self.assertIsNone(payload["cells"][0]["result"]["sharpe"])  # nan -> null

    def test_report_renders_the_headline_and_the_luck_block(self) -> None:
        loader, _ = self.two_market_loader()
        report = run_sweep([market("AAA/USDT")], small_param_grid(), base_config=config(), loader=loader)
        text = format_sweep_report(report)
        self.assertIn("HOW OFTEN WAS IT GOOD?", text)
        self.assertIn("beat buy & hold", text)
        self.assertIn("VERDICT", text)

    def test_run_study_adds_the_luck_test(self) -> None:
        loader, _ = self.two_market_loader()
        report = run_study(
            [market("AAA/USDT")], small_param_grid(),
            base_config=config(), loader=loader, null_runs=8, seed=1,
        )
        self.assertEqual(len(report.nulls), 1)
        self.assertEqual(len(report.nulls[0].null_returns), 8)
        # The selection adjustment must know how many settings were (and were not) tried.
        self.assertEqual(report.nulls[0].selection_n, len(small_param_grid()))
        self.assertIsNotNone(report.summary["median_p_value"])
        text = format_sweep_report(report)
        self.assertIn("LUCK TEST", text)
        self.assertIn("best of 3 rnd", text)

    def test_run_study_with_no_null_runs_skips_the_test(self) -> None:
        loader, _ = self.two_market_loader()
        report = run_study([market("AAA/USDT")], small_param_grid(), base_config=config(), loader=loader, null_runs=0)
        self.assertEqual(report.nulls, [])
        self.assertIsNone(report.summary["median_p_value"])


class TestNullTest(unittest.TestCase):
    def test_p_value_lands_in_range_and_is_seeded(self) -> None:
        m = market()
        bars = candles(m, n=500)
        first = null_test(m, bars, base_config=config(), replicates=6, seed=3)
        again = null_test(m, bars, base_config=config(), replicates=6, seed=3)
        self.assertEqual(first.null_returns, again.null_returns)
        self.assertIsNotNone(first.p_value)
        self.assertGreaterEqual(first.p_value, 0.0)
        self.assertLessEqual(first.p_value, 1.0)
        self.assertGreater(first.actual_trades, 0)

    def test_smoothed_p_value_never_claims_certainty(self) -> None:
        # Every random schedule beat the real one: the honest answer is 1.0.
        self.assertAlmostEqual(_p_value(0.0, [1.0, 2.0, 3.0]), 1.0)
        # Nothing random came close: 1/(3+1), never a claimed probability of zero.
        self.assertAlmostEqual(_p_value(99.0, [1.0, 2.0, 3.0]), 0.25)
        self.assertTrue(math.isnan(_p_value(5.0, [])))

    def test_a_strategy_that_never_trades_has_nothing_to_control_for(self) -> None:
        result = null_test(market(), candles_from(flat_bars(120)), base_config=config(), replicates=3)
        self.assertIsNone(result.p_value)
        self.assertEqual(result.null_returns, [])
        self.assertIn("never traded", result.note)

    def test_expected_max_is_the_best_of_as_many_settings_as_were_tried(self) -> None:
        m = market()
        bars = candles(m, n=400)
        one = null_test(m, bars, base_config=config(), replicates=10, seed=2, selection_n=1)
        many = null_test(m, bars, base_config=config(), replicates=10, seed=2, selection_n=8)
        # Trying one setting means no selection effect: just the average schedule.
        self.assertAlmostEqual(one.expected_max_pct, sum(one.null_returns) / len(one.null_returns))
        # Trying eight settings raises the bar the best cell has to clear.
        self.assertGreater(many.expected_max_pct, one.expected_max_pct)


class TestGridsAndLabels(unittest.TestCase):
    def test_default_grid_spans_speed_and_stop_width(self) -> None:
        grid = default_param_grid()
        self.assertEqual(len(grid), 15)
        for params in grid:
            self.assertLess(params["fast"], params["slow"])
            make_strategy(params)  # every cell must be constructible

    def test_small_grid_includes_the_baseline(self) -> None:
        grid = small_param_grid()
        self.assertEqual(len(grid), 3)
        self.assertIn(BASELINE_PARAMS["slow"], [p["slow"] for p in grid])

    def test_short_label_reads_as_speed_and_stop(self) -> None:
        self.assertEqual(short_params({"fast": 20, "slow": 50, "atr_stop_mult": 2.5, "trail_mult": 3.0}), "20/50 s2.5 t3.0")

    def test_partial_params_fall_back_to_the_baseline(self) -> None:
        strategy = make_strategy({"fast": 9, "slow": 21})
        self.assertEqual(strategy.fast, 9)
        self.assertEqual(strategy.atr_period, BASELINE_PARAMS["atr_period"])
        self.assertEqual(strategy.trail_mult, BASELINE_PARAMS["trail_mult"])

    def test_a_cell_with_few_trades_is_not_good_however_it_returns(self) -> None:
        generous = SweepCell(market(), dict(BASELINE_PARAMS), metrics=metrics(excess=500.0, trades=2))
        self.assertFalse(generous.good())


if __name__ == "__main__":
    unittest.main()
