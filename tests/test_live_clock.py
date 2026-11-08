#!/usr/bin/env python
"""
Milestone 2 tests for the live clock and bar-closed stream adapter.

These tests assert:
- BarClock can compute bar boundaries from a timeframe and bar length.
- BarClosedPolicy can forbid open-bar trading by default.
- CandleStreamAdapter exposes bars one at a time and advances cleanly.
- make_bar_closed_stream wires the pieces together without breaking the
  historical stream semantics.
"""
from __future__ import annotations

import unittest

from quant.datafeed import Candles
from quant.live_clock import BarClock, BarClosedPolicy, CandleStreamAdapter, make_bar_closed_stream
from tests.helpers import candles_from, flat_bars


class TestBarClock(unittest.TestCase):
    def test_bar_boundaries_match_timeframe(self):
        candles = candles_from(flat_bars(3, price=100.0))
        clock = BarClock(timeframe=candles.timeframe, bar_ms=candles.bar_ms)
        self.assertEqual(clock.bar_end_ts(0) - clock.bar_start_ts(0), candles.bar_ms)

    def test_bar_closed_before_end_is_false(self):
        candles = candles_from(flat_bars(3, price=100.0))
        clock = BarClock(timeframe=candles.timeframe, bar_ms=candles.bar_ms)
        self.assertFalse(clock.is_bar_closed_at(0, clock.bar_start_ts(0) + 1))

    def test_bar_closed_at_or_after_end_is_true(self):
        candles = candles_from(flat_bars(3, price=100.0))
        clock = BarClock(timeframe=candles.timeframe, bar_ms=candles.bar_ms)
        self.assertTrue(clock.is_bar_closed_at(0, clock.bar_end_ts(0)))

    def test_current_bar_index_for_mid_bar(self):
        candles = candles_from(flat_bars(3, price=100.0))
        clock = BarClock(timeframe=candles.timeframe, bar_ms=candles.bar_ms)
        mid = clock.bar_start_ts(2) + clock.bar_ms // 2
        self.assertEqual(clock.current_bar_index_for(mid), 2)

    def test_current_bar_index_before_first_bar(self):
        candles = candles_from(flat_bars(3, price=100.0))
        clock = BarClock(timeframe=candles.timeframe, bar_ms=candles.bar_ms)
        self.assertEqual(clock.current_bar_index_for(clock.bar_start_ts(0) - 1), -1)


class TestBarClosedPolicy(unittest.TestCase):
    def test_default_forbids_open_bar_trading(self):
        policy = BarClosedPolicy()
        self.assertFalse(policy.is_safe_to_evaluate(False))
        self.assertTrue(policy.is_safe_to_evaluate(True))

    def test_allow_open_bar_can_be_turned_on(self):
        policy = BarClosedPolicy(allow_open_bar_trading=True)
        self.assertTrue(policy.is_safe_to_evaluate(False))
        self.assertTrue(policy.is_safe_to_evaluate(True))


class TestCandleStreamAdapter(unittest.TestCase):
    def test_stream_exposes_bars_one_at_a_time(self):
        candles = candles_from(flat_bars(4, price=100.0))
        stream = CandleStreamAdapter(candles=candles)
        bars = []
        while stream:
            bars.append(stream.advance())

        self.assertEqual(len(bars), 4)
        self.assertFalse(stream)
        self.assertIsNone(stream.peek())
        self.assertIsNone(stream.advance())

    def test_peek_does_not_advance(self):
        candles = candles_from(flat_bars(3, price=100.0))
        stream = CandleStreamAdapter(candles=candles)
        first = stream.peek()
        second = stream.peek()
        self.assertEqual(first, second)
        self.assertTrue(stream)

    def test_stream_reports_length(self):
        candles = candles_from(flat_bars(6, price=100.0))
        stream = CandleStreamAdapter(candles=candles)
        self.assertEqual(len(stream), 6)


class TestMakeBarClosedStream(unittest.TestCase):
    def test_wires_clock_and_stream(self):
        candles = candles_from(flat_bars(3, price=100.0))
        stream = make_bar_closed_stream(candles)
        self.assertIsInstance(stream, CandleStreamAdapter)
        first = stream.advance()
        self.assertIsNotNone(first)
        self.assertEqual(first.index, 0)

    def test_anchored_clock_uses_real_timestamps(self):
        candles = candles_from(flat_bars(6, price=100.0))
        t0 = int(candles.ts[0])
        clock = BarClock(timeframe=candles.timeframe, bar_ms=candles.bar_ms, t0=t0)
        # Mid-history "now": bars ending at or before now are closed.
        now = t0 + 3 * candles.bar_ms  # exactly bar 2's close
        self.assertTrue(clock.is_bar_closed_at(2, now))
        self.assertFalse(clock.is_bar_closed_at(3, now))
        self.assertEqual(clock.current_bar_index_for(now), 3)

    def test_stream_with_real_now_marks_open_bars(self):
        candles = candles_from(flat_bars(6, price=100.0))
        now = int(candles.ts[3])  # bars 0-2 closed, 3+ open
        stream = make_bar_closed_stream(candles, now_ms=now)
        closed = [stream.advance().bar_closed for _ in range(6)]
        self.assertEqual(closed, [True, True, True, False, False, False])

    def test_make_bar_closed_stream_is_deterministic_over_history(self):
        candles = candles_from(flat_bars(5, price=100.0))
        stream = make_bar_closed_stream(candles)
        bars = []
        while stream:
            bars.append(stream.advance())
        ts_values = [b.ts for b in bars]
        self.assertEqual(ts_values, list(candles.ts))


if __name__ == "__main__":
    unittest.main()
