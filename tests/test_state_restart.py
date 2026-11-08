#!/usr/bin/env python
"""Milestone 5: persistence, idempotency, restart recovery."""
from __future__ import annotations

import unittest

from quant.broker import Order
from quant.exchange import PaperExchangeAdapter
from quant.state_store import (
    export_adapter_state,
    import_adapter_state,
    load_state,
    save_state,
    startup_reconcile,
)
from tests.helpers import candles_from, flat_bars


def bars(n=8):
    return candles_from(flat_bars(n))


def step(ex, candles, i):
    return ex.process_bar(int(candles.ts[i]), float(candles.open[i]), float(candles.high[i]),
                          float(candles.low[i]), float(candles.close[i]))


class TestPersistence(unittest.TestCase):
    def test_save_load_round_trip(self):
        import tempfile
        from pathlib import Path
        ex = PaperExchangeAdapter()
        ex.connect()
        candles = bars()
        for i in range(3):
            step(ex, candles, i)
        oid = ex.place_order(Order("buy", qty=1.0, reason="hold"), client_id="bar-3")
        state = export_adapter_state(ex, last_bar_ts=int(candles.ts[2]), last_bar_index=2)
        with tempfile.TemporaryDirectory() as tmp:
            p = save_state(Path(tmp) / "run.json", state)
            back = load_state(p)
        self.assertEqual(back["last_bar_index"], 2)
        self.assertIn(oid, back["orders"])
        self.assertEqual(back["by_client"]["bar-3"], oid)
        self.assertEqual(back["version"], 1)

    def test_corrupt_state_fails_loudly(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.json"
            p.write_text('{"version": 999}', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_state(p)


class TestRestartRecovery(unittest.TestCase):
    def test_restart_mid_run_fills_once(self):
        candles = bars(8)
        ex = PaperExchangeAdapter()
        ex.connect()
        for i in range(3):
            step(ex, candles, i)
        oid = ex.place_order(Order("buy", qty=1.0, reason="entry"), client_id="entry-bar-3")
        state = export_adapter_state(ex, last_bar_ts=int(candles.ts[2]), last_bar_index=2)
        # Crash: brand-new adapter restored from disk.
        ex2 = PaperExchangeAdapter()
        ex2.connect()
        import_adapter_state(ex2, state)
        self.assertEqual(ex2.get_order_state(oid).status, "open")
        for i in range(3, 6):
            step(ex2, candles, i)
        self.assertEqual(len(ex2.paper.fills), 1)
        self.assertEqual(ex2.get_order_state(oid).status, "filled")

    def test_lost_order_response_does_not_double_submit(self):
        ex = PaperExchangeAdapter()
        ex.connect()
        oid = ex.place_order(Order("buy", qty=1.0, reason="e"), client_id="k-1")
        pending_before = len(ex.paper.broker.pending)
        # Response lost: caller retries with the same client key.
        again = ex.place_order(Order("buy", qty=1.0, reason="e"), client_id="k-1")
        self.assertEqual(oid, again)
        self.assertEqual(len(ex.paper.broker.pending), pending_before)

    def test_crash_mid_bar_replay_is_idempotent(self):
        candles = bars(6)
        ex = PaperExchangeAdapter()
        ex.connect()
        for i in range(3):
            step(ex, candles, i)
        ex.place_order(Order("buy", qty=1.0, reason="e"), client_id="e3")
        state = export_adapter_state(ex, last_bar_ts=int(candles.ts[2]), last_bar_index=2)
        ex2 = PaperExchangeAdapter()
        ex2.connect()
        import_adapter_state(ex2, state)
        # Re-feed bar 2 (already processed) then continue.
        step(ex2, candles, 2)
        self.assertEqual(len(ex2._candles), 3)  # no duplicate candle
        for i in range(3, 5):
            step(ex2, candles, i)
        self.assertEqual(len(ex2.paper.fills), 1)

    def test_local_remote_mismatch_is_reported(self):
        ex = PaperExchangeAdapter()
        ex.connect()
        oid = ex.place_order(Order("buy", qty=1.0, reason="m"))
        ex.get_order_state(oid).status = "filled"  # lie locally: broker still pending
        problems = ex.check_consistency()
        self.assertTrue(problems)
        rec = startup_reconcile(ex)
        self.assertTrue(rec)


if __name__ == "__main__":
    unittest.main()
