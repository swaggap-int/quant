"""The results directory must only ever surface real backtests.

The robustness study writes its own JSON into ``results/``, so the run index
has to tell a study apart from a run rather than showing an empty row.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from quant.dashboard.server import RunIndex


def write(directory: Path, name: str, payload: object) -> None:
    (directory / name).write_text(json.dumps(payload), encoding="utf-8")


class TestRunIndex(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_backtest_result_is_listed(self) -> None:
        write(
            self.dir,
            "BTCUSDT_1d_ema_20260101T000000Z.json",
            {
                "run_id": "BTCUSDT_1d_ema_20260101T000000Z",
                "created_at": "2026-01-01T00:00:00+00:00",
                "strategy": {"name": "ema-atr-trend"},
                "data": {"symbol": "BTC/USDT", "timeframe": "1d", "bars": 100},
                "metrics": {"trades": 30, "total_return_pct": 12.0, "excess_return_pct": 3.0},
            },
        )
        summaries = RunIndex(self.dir).summaries()
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["symbol"], "BTC/USDT")
        self.assertEqual(summaries[0]["trades"], 30)

    def test_a_study_file_is_ignored_rather_than_shown_as_an_empty_run(self) -> None:
        write(self.dir, "robustness.json", {"window": {}, "summary": {"ran": 60}, "cells": []})
        index = RunIndex(self.dir)
        self.assertEqual(index.summaries(), [])
        self.assertIsNone(index.get("robustness"))

    def test_unreadable_files_do_not_break_the_index(self) -> None:
        (self.dir / "truncated.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(RunIndex(self.dir).summaries(), [])


if __name__ == "__main__":
    unittest.main()
