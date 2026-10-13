"""Indicators must match their textbook definitions, warmup included."""

from __future__ import annotations

import unittest

import numpy as np

from quant.indicators import atr, donchian, ema, highest, lowest, rma, rolling_std, rsi, sma, true_range


class TestMovingAverages(unittest.TestCase):
    def setUp(self) -> None:
        self.x = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0])

    def test_sma_matches_hand_calculation(self) -> None:
        out = sma(self.x, 3)
        self.assertTrue(np.all(np.isnan(out[:2])))
        np.testing.assert_allclose(out[2:], [2.0, 3.0, 4.0, 5.0, 6.0, 7.0])

    def test_sma_shorter_than_period_is_all_nan(self) -> None:
        self.assertTrue(np.all(np.isnan(sma(np.array([1.0, 2.0]), 5))))

    def test_ema_seeds_with_sma_then_recurses(self) -> None:
        out = ema(self.x, 3)
        self.assertAlmostEqual(out[2], 2.0)  # seed = mean(1,2,3)
        alpha = 2.0 / 4.0
        expected = 2.0
        for v in [4.0, 5.0, 6.0, 7.0, 8.0]:
            expected = alpha * v + (1 - alpha) * expected
        self.assertAlmostEqual(out[-1], expected)

    def test_ema_converges_to_pandas_ewm(self) -> None:
        """Same recursion as pandas, different seed, so the gap must decay.

        pandas seeds with the first observation; we seed with the SMA of the
        first ``period`` values. Both are standard; ours is the one that matches
        the textbook definition. The difference decays by (1 - alpha) per bar.
        """
        try:
            import pandas as pd
        except ImportError:  # pragma: no cover
            self.skipTest("pandas not installed")
        rng = np.random.default_rng(7)
        x = 100 + np.cumsum(rng.normal(0, 1, 400))
        ours = ema(x, 20)
        theirs = pd.Series(x).ewm(span=20, adjust=False).mean().to_numpy()
        gap = np.abs(ours[20:] - theirs[20:])
        self.assertLess(gap[-1], gap[0], "seed difference should decay, not grow")
        self.assertTrue(np.all(np.diff(gap[-50:]) < 1e-12), "gap should be shrinking at the end")
        np.testing.assert_allclose(ours[-50:], theirs[-50:], rtol=1e-6, atol=2e-3)

    def test_rma_uses_wilder_alpha(self) -> None:
        out = rma(self.x, 3)
        self.assertAlmostEqual(out[2], 2.0)
        self.assertAlmostEqual(out[3], (2.0 * 2 + 4.0) / 3)

    def test_rolling_std_matches_numpy(self) -> None:
        out = rolling_std(self.x, 4)
        self.assertAlmostEqual(out[3], float(np.std(self.x[:4], ddof=1)))


class TestVolatilityIndicators(unittest.TestCase):
    def test_true_range_uses_prior_close(self) -> None:
        high = np.array([10.0, 12.0, 11.0])
        low = np.array([8.0, 11.0, 9.0])
        close = np.array([9.0, 11.5, 10.0])
        tr = true_range(high, low, close)
        self.assertAlmostEqual(tr[0], 2.0)  # no prior close on bar 0
        self.assertAlmostEqual(tr[1], max(1.0, abs(12.0 - 9.0), abs(11.0 - 9.0)))
        self.assertAlmostEqual(tr[2], max(2.0, abs(11.0 - 11.5), abs(9.0 - 11.5)))

    def test_atr_is_wilder_smoothed_true_range(self) -> None:
        rng = np.random.default_rng(3)
        close = 100 + np.cumsum(rng.normal(0, 1, 120))
        high, low = close + 1.5, close - 1.5
        np.testing.assert_allclose(atr(high, low, close, 14), rma(true_range(high, low, close), 14))

    def test_atr_needs_no_future_data(self) -> None:
        """ATR at bar i must be identical when later bars are removed."""
        rng = np.random.default_rng(11)
        close = 100 + np.cumsum(rng.normal(0, 2, 200))
        high, low = close + 1.0, close - 1.0
        full = atr(high, low, close, 14)[:120]
        truncated = atr(high[:120], low[:120], close[:120], 14)
        np.testing.assert_allclose(full, truncated)


class TestRsiAndChannels(unittest.TestCase):
    def test_rsi_saturates_on_a_monotonic_rise(self) -> None:
        x = np.arange(1.0, 60.0)
        out = rsi(x, 14)
        self.assertTrue(np.all(np.isnan(out[:14])))
        self.assertAlmostEqual(out[-1], 100.0)

    def test_rsi_is_flat_at_fifty_when_unchanged(self) -> None:
        out = rsi(np.full(60, 42.0), 14)
        self.assertAlmostEqual(out[-1], 50.0)

    def test_rsi_stays_in_range(self) -> None:
        rng = np.random.default_rng(5)
        x = 100 + np.cumsum(rng.normal(0, 3, 500))
        out = rsi(x, 14)[~np.isnan(rsi(x, 14))]
        self.assertTrue(out.min() >= 0.0 and out.max() <= 100.0)

    def test_donchian_channel_matches_window(self) -> None:
        high = np.array([1.0, 5.0, 3.0, 9.0, 2.0])
        low = np.array([0.5, 2.0, 1.0, 4.0, 0.1])
        upper, lower = donchian(high, low, 3)
        self.assertAlmostEqual(upper[2], 5.0)
        self.assertAlmostEqual(upper[3], 9.0)  # rolling, not cumulative
        self.assertAlmostEqual(lower[3], 1.0)
        np.testing.assert_allclose(highest(high, 2), donchian(high, high, 2)[0])
        np.testing.assert_allclose(lowest(low, 2), donchian(low, low, 2)[1])


if __name__ == "__main__":
    unittest.main()
