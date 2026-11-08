#!/usr/bin/env python
"""Milestone 4: exchange adapter contract on the paper venue."""
from __future__ import annotations

import unittest

from quant.broker import Order
from quant.config import Costs
from quant.exchange import (
    AuthError,
    ExchangeAdapter,
    OrderStateMismatch,
    PaperExchangeAdapter,
    RateLimitError,
    UnknownOrderError,
)
from tests.helpers import candles_from, flat_bars


def bars():
    return candles_from(flat_bars(6))


class TestContract(unittest.TestCase):
    def test_paper_implements_contract(self):
        self.assertTrue(issubclass(PaperExchangeAdapter, ExchangeAdapter))

    def test_connect_place_fill_cancel_account_order_state(self):
        ex = PaperExchangeAdapter(initial_cash=10_000.0)
        ex.connect()
        candles = bars()
        for i in range(3):
            ex.process_bar(int(candles.ts[i]), float(candles.open[i]), float(candles.high[i]),
                           float(candles.low[i]), float(candles.close[i]))
        oid = ex.place_order(Order("buy", qty=1.0, reason="paper-entry"))
        st = ex.get_order_state(oid)
        self.assertEqual(st.status, "open")
        ex.process_bar(int(candles.ts[3]), float(candles.open[3]), float(candles.high[3]),
                       float(candles.low[3]), float(candles.close[3]))
        self.assertEqual(ex.get_order_state(oid).status, "filled")
        acct = ex.get_account_state()
        self.assertIn("cash", acct)
        self.assertIn("open_orders", acct)
        self.assertEqual(ex.latest_price(), float(candles.close[3]))
        self.assertEqual(len(ex.get_candles()), 4)
        self.assertEqual(ex.check_consistency(), [])

    def test_cancel_open_then_unknown_and_filled(self):
        ex = PaperExchangeAdapter()
        ex.connect()
        oid = ex.place_order(Order("buy", qty=1.0, reason="to-cancel"))
        self.assertTrue(ex.cancel_order(oid))
        self.assertEqual(ex.get_order_state(oid).status, "cancelled")
        with self.assertRaises(UnknownOrderError):
            ex.cancel_order("paper-999")
        with self.assertRaises(UnknownOrderError):
            ex.get_order_state("paper-999")

    def test_cancel_filled_is_mismatch(self):
        ex = PaperExchangeAdapter()
        ex.connect()
        candles = bars()
        oid = ex.place_order(Order("buy", qty=1.0, reason="fill-then-cancel"))
        for i in range(4):
            ex.process_bar(int(candles.ts[i]), float(candles.open[i]), float(candles.high[i]),
                           float(candles.low[i]), float(candles.close[i]))
        self.assertEqual(ex.get_order_state(oid).status, "filled")
        with self.assertRaises(OrderStateMismatch):
            ex.cancel_order(oid)

    def test_idempotent_reissue_same_client_id(self):
        ex = PaperExchangeAdapter()
        ex.connect()
        a = ex.place_order(Order("buy", qty=1.0, reason="x"), client_id="strat-bar-3")
        b = ex.place_order(Order("buy", qty=1.0, reason="x"), client_id="strat-bar-3")
        self.assertEqual(a, b)


class TestResilience(unittest.TestCase):
    def test_retry_backoff_recovers(self):
        ex = PaperExchangeAdapter()
        ex.connect()
        ex.fail_plan["place"] = 2
        sleeps: list[float] = []
        oid = ex.place_order_with_retry(Order("buy", qty=1.0, reason="r"),
                                        max_attempts=3, sleep_fn=sleeps.append)
        self.assertTrue(oid.startswith("paper-"))
        self.assertEqual(len(ex.attempt_log), 3)
        self.assertEqual(len(sleeps), 2)  # backoff between attempts, explicit

    def test_retry_exhausts_raises(self):
        ex = PaperExchangeAdapter()
        ex.connect()
        ex.fail_plan["place"] = 5
        with self.assertRaises(Exception):
            ex.place_order_with_retry(Order("buy", qty=1.0, reason="r"), max_attempts=2,
                                      sleep_fn=lambda s: None)

    def test_auth_expired_then_refresh(self):
        ex = PaperExchangeAdapter()
        ex.connect()
        ex.expire_auth()
        with self.assertRaises(AuthError):
            ex.place_order(Order("buy", qty=1.0, reason="x"))
        ex.refresh_auth()
        oid = ex.place_order(Order("buy", qty=1.0, reason="x"))
        self.assertTrue(oid)

    def test_rate_limit_is_explicit(self):
        ex = PaperExchangeAdapter(max_calls=2)
        ex.connect()
        ex.place_order(Order("buy", qty=1.0, reason="a"))
        ex.place_order(Order("buy", qty=2.0, reason="b"))
        with self.assertRaises(RateLimitError):
            ex.place_order(Order("buy", qty=3.0, reason="c"))

    def test_partial_fill_is_explicit(self):
        ex = PaperExchangeAdapter()
        ex.connect()
        oid = ex.place_order(Order("buy", qty=10.0, reason="big"))
        st = ex.record_partial_fill(oid, 4.0)
        self.assertEqual(st.status, "partial")
        self.assertEqual(st.filled_qty, 4.0)
        self.assertEqual(ex.get_order_state(oid).status, "partial")

    def test_mismatch_report_not_silent(self):
        ex = PaperExchangeAdapter()
        ex.connect()
        oid = ex.place_order(Order("buy", qty=1.0, reason="m"))
        # Tamper locally to simulate a divergent view.
        ex.get_order_state(oid).status = "bogus"
        problems = ex.check_consistency()
        self.assertTrue(any("bogus" in p or "mismatch" in p for p in problems))


if __name__ == "__main__":
    unittest.main()
