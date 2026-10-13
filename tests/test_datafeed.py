"""Data layer tests. All offline: parsers are exercised with fixture payloads."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from quant.datafeed import (
    BinanceProvider,
    Candles,
    CoinbaseProvider,
    DataError,
    KrakenProvider,
    OkxProvider,
    _build,
    load_candles,
    merge_candles,
    parse_date,
    read_cache,
    write_cache,
)
from tests.helpers import DAY_MS, START_MS, candles_from, flat_bars


class TestCandlesValidation(unittest.TestCase):
    def test_rejects_nan(self) -> None:
        with self.assertRaises(ValueError):
            candles_from([(1.0, 2.0, 0.5, float("nan"), 1.0)])

    def test_rejects_unsorted_timestamps(self) -> None:
        good = candles_from(flat_bars(3))
        with self.assertRaises(ValueError):
            Candles(**{**good.__dict__, "ts": good.ts[::-1].copy()})

    def test_rejects_high_below_low(self) -> None:
        with self.assertRaises(ValueError):
            candles_from([(1.0, 0.5, 1.5, 1.0, 1.0)])

    def test_slice_is_half_open_on_the_right(self) -> None:
        candles = candles_from(flat_bars(5))
        window = candles.slice(int(candles.ts[1]), int(candles.ts[3]))
        self.assertEqual(len(window), 2)
        self.assertEqual(int(window.ts[0]), int(candles.ts[1]))
        self.assertEqual(int(window.ts[-1]), int(candles.ts[2]))

    def test_len_and_bool(self) -> None:
        self.assertEqual(len(candles_from(flat_bars(4))), 4)
        empty = candles_from([])
        self.assertFalse(empty)
        self.assertEqual(empty.start_iso, "-")


class TestDateParsing(unittest.TestCase):
    def test_iso_date(self) -> None:
        self.assertEqual(parse_date("2024-01-01"), 1_704_067_200_000)

    def test_iso_datetime_with_z(self) -> None:
        self.assertEqual(parse_date("2024-01-01T00:30:00Z"), 1_704_069_000_000)

    def test_epoch_seconds_and_millis_both_work(self) -> None:
        self.assertEqual(parse_date("1704067200"), 1_704_067_200_000)
        self.assertEqual(parse_date("1704067200000"), 1_704_067_200_000)

    def test_default_is_returned_for_none(self) -> None:
        self.assertEqual(parse_date(None, default=7), 7)
        self.assertEqual(parse_date("", default=7), 7)

    def test_bad_input_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_date("last tuesday")


class TestSymbolMapping(unittest.TestCase):
    def test_binance_strips_the_separator(self) -> None:
        self.assertEqual(BinanceProvider().to_symbol("BTC/USDT"), "BTCUSDT")
        self.assertEqual(BinanceProvider().to_symbol("eth-usd"), "ETHUSD")

    def test_okx_uses_a_dash(self) -> None:
        self.assertEqual(OkxProvider().to_symbol("BTC/USDT"), "BTC-USDT")

    def test_kraken_aliases_btc_to_xbt(self) -> None:
        self.assertEqual(KrakenProvider().to_symbol("BTC/USD"), "XBTUSD")
        self.assertEqual(KrakenProvider().to_symbol("ETH/USDT"), "ETHUSDT")

    def test_coinbase_uses_a_dash(self) -> None:
        self.assertEqual(CoinbaseProvider().to_symbol("BTC/USD"), "BTC-USD")

    def test_unsupported_timeframe_fails_before_any_network_call(self) -> None:
        with self.assertRaises(DataError):
            CoinbaseProvider().fetch("BTC/USD", "1w", START_MS, START_MS + DAY_MS)
        with self.assertRaises(DataError):
            KrakenProvider().fetch("BTC/USD", "nonsense", START_MS, START_MS + DAY_MS)


class TestResponseParsing(unittest.TestCase):
    def test_build_sorts_and_dedupes(self) -> None:
        rows = [
            [START_MS, 1, 2, 0.5, 1.5, 10],
            [START_MS + DAY_MS, 1, 2, 0.5, 1.5, 10],
            [START_MS, 9, 9, 9, 9, 9],  # duplicate timestamp, later wins
        ]
        candles = _build(rows, "BTC/USDT", "1d", "test")
        self.assertEqual(len(candles), 2)
        self.assertTrue(np.all(np.diff(candles.ts) > 0))
        self.assertEqual(candles.provider, "test")

    def test_empty_rows_build_an_empty_series(self) -> None:
        candles = _build([], "BTC/USDT", "1d", "test")
        self.assertEqual(len(candles), 0)
        self.assertFalse(candles)

    def test_row_order_is_normalised(self) -> None:
        rows = [[START_MS + 2 * DAY_MS, 3, 4, 2, 3, 1], [START_MS, 1, 2, 0.5, 1, 1]]
        candles = _build(rows, "X", "1d", "test")
        np.testing.assert_array_equal(candles.close, [1.0, 3.0])


class TestCache(unittest.TestCase):
    def test_round_trip_through_csv(self) -> None:
        candles = candles_from(flat_bars(6), symbol="BTC/USDT")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.csv"
            write_cache(path, candles)
            loaded = read_cache(path)
            self.assertEqual(len(loaded), 6)
            np.testing.assert_array_equal(loaded.ts, candles.ts)
            np.testing.assert_allclose(loaded.close, candles.close)
            self.assertEqual(loaded.symbol, "BTC/USDT")
            self.assertEqual(loaded.timeframe, "1d")

    def test_read_missing_file_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(read_cache(Path(tmp) / "nope.csv"))

    def test_merge_prefers_the_newest_values(self) -> None:
        old = candles_from(flat_bars(3))
        new = candles_from([(5.0, 6.0, 4.0, 5.5, 1.0)], start_ms=START_MS + 2 * DAY_MS)
        merged = merge_candles(old, new)
        self.assertEqual(len(merged), 3)
        self.assertAlmostEqual(float(merged.close[2]), 5.5)

    def test_merge_with_empty_sides(self) -> None:
        old = candles_from(flat_bars(2))
        empty = candles_from([])
        self.assertEqual(len(merge_candles(empty, old)), 2)
        self.assertEqual(len(merge_candles(old, empty)), 2)

    def test_load_from_csv_never_touches_the_network(self) -> None:
        candles = candles_from(flat_bars(10))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prices.csv"
            write_cache(path, candles)
            loaded = load_candles("BTC/USDT", "1d", provider="binance", csv_path=path, verbose=False)
            self.assertEqual(len(loaded), 10)
            self.assertEqual(loaded.timeframe, "1d")

    def test_load_from_csv_respects_the_window(self) -> None:
        candles = candles_from(flat_bars(10))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prices.csv"
            write_cache(path, candles)
            loaded = load_candles(
                "BTC/USDT", "1d",
                start="2020-09-14", end="2020-09-16",
                csv_path=path, verbose=False,
            )
            self.assertEqual(len(loaded), 3)

    def test_missing_csv_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(DataError):
                load_candles("BTC/USDT", "1d", csv_path=Path(tmp) / "gone.csv", verbose=False)


if __name__ == "__main__":
    unittest.main()
