#!/usr/bin/env python
"""Milestone 7: paper launch discipline — parity, kill, restart, divergences."""
from __future__ import annotations

import unittest

from quant.broker import Order
from quant.live_runner import LiveRunPolicy, run_simulated_live
from quant.observability import LiveLogger
from quant.paper_launch import compare_runs, launch_report, run_paper_venue
from tests.helpers import ScriptedStrategy, candles_from, flat_bars


def script():
    return {
        2: [Order("buy", qty=1.0, reason="entry-1")],
        4: [Order("sell", qty=1.0, reason="exit-1")],
        5: [Order("buy", qty=1.0, reason="entry-2")],
        7: [Order("sell", qty=1.0, reason="exit-2")],
    }


def config(**kw):
    from quant.config import BacktestConfig
    base = dict(symbol="TEST/USDT", timeframe="1d", initial_cash=10_000.0)
    base.update(kw)
    return BacktestConfig(**base)


class TestLaunchParity(unittest.TestCase):
    def test_paper_venue_matches_simulated_live(self):
        candles = candles_from(flat_bars(12))
        logger = LiveLogger()
        live = run_simulated_live(candles, ScriptedStrategy(script(), windows={}),
                                  config(), logger=logger)
        paper = run_paper_venue(candles, ScriptedStrategy(script(), windows={}))
        self.assertEqual(len(live.fills), 4)
        self.assertEqual(len(paper["fills"]), 4)
        for a, b in zip(live.fills, paper["fills"]):
            self.assertEqual(a["ts"], b["ts"])
            self.assertAlmostEqual(a["price"], b["price"])
        self.assertEqual(compare_runs(live, paper), [])
        rep = launch_report(live, paper, [], period_bars=len(candles))
        self.assertTrue(rep["parity_ok"])
        self.assertTrue(rep["divergence_notes"])

    def test_launch_is_deterministic(self):
        candles = candles_from(flat_bars(12))
        a = run_paper_venue(candles, ScriptedStrategy(script(), windows={}))
        b = run_paper_venue(candles, ScriptedStrategy(script(), windows={}))
        self.assertEqual(a["fills"], b["fills"])

    def test_kill_switch_holds_for_defined_period(self):
        candles = candles_from(flat_bars(12))
        paper = run_paper_venue(candles, ScriptedStrategy(script(), windows={}),
                                policy=LiveRunPolicy(killed=True))
        self.assertEqual(paper["fills"], [])
        self.assertTrue(any("killed" in n for n in paper["notes"]))

    def test_restart_continues_without_duplicate_fills(self):
        from quant.exchange import PaperExchangeAdapter
        from quant.state_store import export_adapter_state, import_adapter_state
        candles = candles_from(flat_bars(12))
        ex = PaperExchangeAdapter()
        ex.connect()
        for i in range(3):
            ex.process_bar(int(candles.ts[i]), float(candles.open[i]), float(candles.high[i]),
                           float(candles.low[i]), float(candles.close[i]))
        oid = ex.place_order(Order("buy", qty=1.0, reason="entry-1"), client_id="entry-1")
        saved = export_adapter_state(ex, last_bar_ts=int(candles.ts[2]), last_bar_index=2)
        ex2 = PaperExchangeAdapter()
        ex2.connect()
        import_adapter_state(ex2, saved)
        for i in range(3, 6):
            ex2.process_bar(int(candles.ts[i]), float(candles.open[i]), float(candles.high[i]),
                            float(candles.low[i]), float(candles.close[i]))
        self.assertEqual(len(ex2.paper.fills), 1)
        self.assertEqual(ex2.get_order_state(oid).status, "filled")


if __name__ == "__main__":
    unittest.main()
