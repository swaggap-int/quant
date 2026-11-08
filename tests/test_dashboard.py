"""The results directory must only ever surface real backtests.

The robustness study writes its own JSON into ``results/``, so the run index
has to tell a study apart from a run rather than showing an empty row.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer


from datetime import datetime, timezone
from unittest.mock import patch

from quant.dashboard.server import (
    BadRequest,
    PaperIndex,
    RunIndex,
    WalkforwardIndex,
    _excess_sources_fresh,
    build_walkforward_command,
    launch_walkforward,
    make_handler,
    RESULTS_ROOT,
)

NOW_MS = int(datetime(2026, 10, 28, 14, 0, tzinfo=timezone.utc).timestamp() * 1000)


def write(directory: Path, name: str, payload: object) -> None:
    (directory / name).write_text(json.dumps(payload), encoding="utf-8")


def _serve_json(
    handler: type, path: str, method: str = "GET", payload: dict | None = None
) -> dict:
    """Start a handler on an ephemeral port, request one path, and stop it."""
    import socket
    import urllib.request

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 5.0
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise AssertionError("server did not start accepting connections in time")
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        with urllib.request.urlopen(request, timeout=5) as res:
            return json.loads(res.read())
    finally:
        httpd.shutdown()
        httpd.server_close()


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


class TestPaperIndex(unittest.TestCase):
    """The dashboard must mirror the scheduler probe, not invent its own answer."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.paper = Path(self._tmp.name) / "paper"
        self.paper.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _last_run(self, **overrides: object) -> dict:
        payload = {
            "run_id": "BTCUSDT_1d_ema-atr-trend_20261028T140000Z",
            "created_at": "2026-10-28T14:00:00+00:00",
            "success": True,
            "exit_code": 0,
            "timeframe": "1d",
            "run_timestamp_ms": NOW_MS,
            "freshness_ok": True,
            "data_end_iso": "2026-10-28 00:00:00",
            "trade_count": 3,
            "equity_final": 9967.28,
            "total_return_pct": -0.33,
            "verdict": "Read with care",
        }
        payload.update(overrides)
        write(self.paper, "last_run.json", payload)
        return payload

    def _run(self, run_id: str, *, equity: float = 10000.0, trades: int = 0) -> None:
        write(
            self.paper,
            f"{run_id}.json",
            {
                "run_id": run_id,
                "created_at": "2026-10-28T00:00:00+00:00",
                "data_end_iso": "2026-10-28 00:00:00",
                "bar_count": 365,
                "trade_count": trades,
                "equity_final": equity,
                "config": {"initial_cash": 10000.0},
                "freshness_ok": True,
                "verdict": "ok",
            },
        )

    def test_snapshot_lists_newest_runs_first_with_a_computed_return(self) -> None:
        self._last_run()
        self._run("BTCUSDT_1d_ema-atr-trend_20261026T070000Z")
        self._run("BTCUSDT_1d_ema-atr-trend_20261027T070000Z", equity=9967.28, trades=3)

        snap = PaperIndex(self.paper).snapshot(now_ms=NOW_MS + 60_000)

        self.assertTrue(snap["health"]["healthy"], snap["health"])
        self.assertEqual(
            [r["run_id"] for r in snap["runs"]],
            [
                "BTCUSDT_1d_ema-atr-trend_20261027T070000Z",
                "BTCUSDT_1d_ema-atr-trend_20261026T070000Z",
            ],
        )
        self.assertAlmostEqual(snap["runs"][0]["return_pct"], -0.33, places=2)

    def test_daily_bar_floor_keeps_a_daily_schedule_healthy_between_runs(self) -> None:
        self._last_run()
        # Twenty hours after the run: one hour would call it dead, the 1.5-bar
        # floor for a 1d bot must not.
        snap = PaperIndex(self.paper).snapshot(now_ms=NOW_MS + 20 * 3600 * 1000)

        self.assertTrue(snap["health"]["healthy"], snap["health"])
        self.assertEqual(snap["health"]["effective_max_missing_seconds"], 129_600.0)
        self.assertEqual(snap["health"]["requested_max_missing_seconds"], 3600.0)

    def test_no_state_yet_is_unhealthy_and_says_so(self) -> None:
        snap = PaperIndex(self.paper).snapshot(now_ms=NOW_MS)

        self.assertFalse(snap["health"]["healthy"])
        self.assertEqual(snap["health"]["reason"], "no successful run yet")
        self.assertEqual(snap["runs"], [])

    def test_state_files_are_never_listed_as_runs(self) -> None:
        self._last_run()
        write(self.paper, "last_failure.json", {"kind": "stale_data", "reason": "x"})
        self._run("BTCUSDT_1d_ema-atr-trend_20261027T070000Z")

        snap = PaperIndex(self.paper).snapshot(now_ms=NOW_MS + 60_000)

        self.assertEqual(
            [r["run_id"] for r in snap["runs"]],
            ["BTCUSDT_1d_ema-atr-trend_20261027T070000Z"],
        )

    def test_api_paper_serves_the_same_snapshot(self) -> None:
        self._last_run()
        self._run("BTCUSDT_1d_ema-atr-trend_20261027T070000Z", equity=10400.0, trades=2)
        results = Path(self._tmp.name)

        body = _serve_json(make_handler(RunIndex(results)), "/api/paper")

        self.assertIn("health", body)
        self.assertIn("runs", body)
        self.assertEqual(body["runs"][0]["run_id"], "BTCUSDT_1d_ema-atr-trend_20261027T070000Z")
        self.assertAlmostEqual(body["runs"][0]["return_pct"], 4.0, places=2)


class TestWalkforwardIndex(unittest.TestCase):
    """The card must report the study's own reading, not a second opinion."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _study(self, **overrides: object) -> dict:
        payload = {
            "study": "rolling_walkforward",
            "timeframe": "4h",
            "folds": 5,
            "symbols": [
                {
                    "symbol": "BTC/USDT",
                    "first_train": "2021-01-01 00:00:00",
                    "last_test": "2026-10-28 20:00:00",
                    "families": {
                        "ema": {"picks": ["30/60", "30/60", "30/60", "10/40"]},
                        "donchian": {"picks": ["30/15", "30/15", "20/10", "30/15"]},
                    },
                },
                {
                    "symbol": "ETH/USDT",
                    "first_train": "2021-01-01 00:00:00",
                    "last_test": "2026-10-28 20:00:00",
                    "families": {
                        "ema": {"picks": ["30/60", "30/60", "30/60", "10/40"]},
                        "donchian": {"picks": ["55/20", "30/15", "55/20", "30/15"]},
                    },
                },
            ],
            "headline": {
                "ema": {
                    "windows": 8,
                    "beat_buy_hold": 4,
                    "risk_adjusted_beat": 5,
                    "risk_adjusted_windows": 8,
                    "median_test_excess": -3.9,
                    "verdict": "Mixed: 4 of 8 -- no reliable edge in this sample.",
                },
                "donchian": {
                    "windows": 8,
                    "beat_buy_hold": 4,
                    "risk_adjusted_beat": 4,
                    "risk_adjusted_windows": 8,
                    "median_test_excess": -4.72,
                    "verdict": "Mixed: 4 of 8 -- no reliable edge in this sample.",
                },
            },
        }
        payload.update(overrides)
        write(self.dir, "walkforward_rolling_4h.json", payload)
        return payload

    def test_counts_and_markets_come_through(self) -> None:
        self._study()

        snap = WalkforwardIndex(self.dir).snapshot()

        self.assertTrue(snap["available"])
        self.assertEqual(snap["timeframe"], "4h")
        self.assertEqual(snap["folds"], 5)
        self.assertEqual(snap["windows"], 8)
        self.assertEqual(snap["markets"], ["BTC/USDT", "ETH/USDT"])
        ema = snap["families"][0]
        self.assertEqual(ema["label"], "EMA-ATR")
        self.assertEqual((ema["beat"], ema["windows"]), (4, 8))
        self.assertEqual((ema["risk_beat"], ema["risk_windows"]), (5, 8))
        self.assertEqual(ema["median_excess"], -3.9)
        # Picks are pooled across markets, not read from one market's summary.
        self.assertEqual((ema["pick"], ema["pick_pct"]), ("30/60", 75.0))
        donchian = snap["families"][1]
        self.assertEqual(donchian["median_excess"], -4.72)
        self.assertEqual((donchian["pick"], donchian["pick_pct"]), ("30/15", 62.5))

    def test_three_family_study_lists_all_three_labels(self) -> None:
        payload = self._study()
        payload["headline"]["bollinger"] = {
            "windows": 8,
            "beat_buy_hold": 4,
            "risk_adjusted_beat": 2,
            "risk_adjusted_windows": 7,
            "median_test_excess": -20.73,
            "verdict": "Mixed: 4 of 8 -- no reliable edge in this sample.",
        }
        write(self.dir, "walkforward_rolling_4h.json", payload)

        snap = WalkforwardIndex(self.dir).snapshot()

        labels = [family["label"] for family in snap["families"]]
        self.assertEqual(labels, ["EMA-ATR", "Donchian", "Bollinger reversion"])
        self.assertEqual(
            snap["studies"][0]["family_labels"],
            ["EMA-ATR", "Donchian", "Bollinger reversion"],
        )

    def test_a_shared_verdict_is_one_plain_sentence(self) -> None:
        self._study()

        snap = WalkforwardIndex(self.dir).snapshot()

        self.assertEqual(snap["verdict"], "Mixed: 4 of 8 \u2014 no reliable edge in this sample.")

    def test_disagreeing_families_keep_their_own_reads(self) -> None:
        payload = self._study()
        payload["headline"]["donchian"]["verdict"] = "Did not hold up: 1 of 8."
        write(self.dir, "walkforward_rolling_4h.json", payload)

        snap = WalkforwardIndex(self.dir).snapshot()

        self.assertIsNone(snap["verdict"])
        self.assertEqual(snap["families"][1]["verdict"], "Did not hold up: 1 of 8.")

    def test_a_study_without_pick_lists_still_reports_the_counts(self) -> None:
        payload = self._study()
        for row in payload["symbols"]:
            row.pop("families")
        write(self.dir, "walkforward_rolling_4h.json", payload)

        snap = WalkforwardIndex(self.dir).snapshot()

        ema = snap["families"][0]
        self.assertIsNone(ema["pick"])
        self.assertIsNone(ema["pick_pct"])
        self.assertEqual(ema["median_excess"], -3.9)

    def _dated_study(self, payload: dict, name: str, when: float) -> Path:
        write(self.dir, name, payload)
        path = self.dir / name
        os.utime(path, (when, when))
        return path

    def test_saved_studies_are_listed_newest_first_with_their_settings(self) -> None:
        self._study()
        base = self.dir / "walkforward_rolling_4h.json"
        os.utime(base, (1_700_000_000, 1_700_000_000))
        payload = json.loads(base.read_text(encoding="utf-8"))
        newer = self._dated_study(
            payload, "walkforward_rolling_4h_20261028T120000Z.json", 1_700_000_100
        )

        snap = WalkforwardIndex(self.dir).snapshot()

        self.assertEqual([s["file"] for s in snap["studies"]], [newer.name, base.name])
        self.assertEqual(snap["file"], newer.name)
        self.assertEqual(snap["studies"][0]["timeframe"], "4h")
        self.assertEqual(snap["studies"][0]["folds"], 5)
        self.assertEqual(snap["studies"][0]["family_labels"], ["EMA-ATR", "Donchian"])
        self.assertEqual(snap["studies"][0]["markets"], ["BTC/USDT", "ETH/USDT"])

    def test_a_saved_study_can_be_chosen_by_name(self) -> None:
        payload = self._study()
        base = self.dir / "walkforward_rolling_4h.json"
        os.utime(base, (1_700_000_000, 1_700_000_000))
        payload["headline"]["ema"]["beat_buy_hold"] = 7
        self._dated_study(
            payload, "walkforward_rolling_4h_20261028T120000Z.json", 1_700_000_100
        )

        snap = WalkforwardIndex(self.dir).snapshot(base.name)

        self.assertTrue(snap["available"])
        self.assertEqual(snap["file"], base.name)
        # The chosen file's own numbers, not the newest file's.
        self.assertEqual(snap["families"][0]["beat"], 4)

    def test_an_unknown_file_is_reported_as_missing_with_the_list_intact(self) -> None:
        self._study()

        snap = WalkforwardIndex(self.dir).snapshot("nope.json")

        self.assertFalse(snap["available"])
        self.assertEqual(snap["missing"], "nope.json")
        self.assertEqual(len(snap["studies"]), 1)

    def test_api_serves_a_chosen_study(self) -> None:
        self._study()

        body = _serve_json(
            make_handler(RunIndex(self.dir)),
            "/api/walkforward?file=walkforward_rolling_4h.json",
        )

        self.assertTrue(body["available"])
        self.assertEqual(body["file"], "walkforward_rolling_4h.json")
        self.assertEqual(body["studies"][0]["file"], "walkforward_rolling_4h.json")

    def test_a_foreign_file_with_the_same_name_is_ignored(self) -> None:
        write(self.dir, "walkforward_rolling_4h.json", {"study": "something_else"})

        snap = WalkforwardIndex(self.dir).snapshot()

        self.assertFalse(snap["available"])
        self.assertEqual(snap["families"], [])

    def test_a_truncated_study_falls_back_to_the_next_oldest(self) -> None:
        self._study()
        older = self.dir / "walkforward_rolling_4h.json"
        os.utime(older, (1_700_000_000, 1_700_000_000))
        newer = self.dir / "walkforward_rolling_1d.json"
        newer.write_text("{not json", encoding="utf-8")
        os.utime(newer, (1_700_000_100, 1_700_000_100))

        snap = WalkforwardIndex(self.dir).snapshot()

        self.assertTrue(snap["available"])
        self.assertEqual(snap["file"], "walkforward_rolling_4h.json")

    def test_api_walkforward_serves_the_reading(self) -> None:
        self._study()

        body = _serve_json(make_handler(RunIndex(self.dir)), "/api/walkforward")

        self.assertTrue(body["available"])
        self.assertEqual(body["families"][0]["label"], "EMA-ATR")
        self.assertEqual(body["verdict"], "Mixed: 4 of 8 \u2014 no reliable edge in this sample.")


class TestWalkforwardRun(unittest.TestCase):
    """Rerunning the study must be bounded input and one process at a time."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _study(self) -> None:
        write(
            self.dir,
            "walkforward_rolling_4h.json",
            {
                "study": "rolling_walkforward",
                "timeframe": "4h",
                "folds": 5,
                "symbols": [
                    {"symbol": "BTC/USDT", "first_train": "", "last_test": ""}
                ],
                "headline": {
                    "ema": {
                        "windows": 4,
                        "beat_buy_hold": 2,
                        "risk_adjusted_beat": 2,
                        "risk_adjusted_windows": 4,
                        "verdict": "Mixed.",
                    }
                },
            },
        )

    def _completed(self, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess([], returncode, stdout="", stderr=stderr)

    def test_command_replays_the_study_settings(self) -> None:
        cmd = build_walkforward_command(
            {"timeframe": "4h", "folds": 4, "family": "ema", "symbols": "BTC/USDT,ETH/USDT"}
        )
        joined = " ".join(cmd)
        self.assertEqual(cmd[0], sys.executable)
        self.assertIn("run_walkforward.py", cmd[1])
        self.assertIn("--symbols BTC/USDT,ETH/USDT", joined)
        self.assertIn("--timeframe 4h", joined)
        self.assertIn("--folds 4", joined)
        self.assertIn("--family ema", joined)

    def test_command_accepts_the_three_family_comparison(self) -> None:
        joined = " ".join(build_walkforward_command({"family": "all"}))
        self.assertIn("--family all", joined)

    def test_empty_request_uses_the_script_defaults(self) -> None:
        joined = " ".join(build_walkforward_command({}))
        self.assertIn("--symbols BTC/USDT,ETH/USDT", joined)
        self.assertIn("--timeframe 4h", joined)
        self.assertIn("--folds 5", joined)
        self.assertIn("--family bollinger", joined)

    def test_a_start_date_is_forwarded(self) -> None:
        joined = " ".join(build_walkforward_command({"start": "2022-01-01"}))
        self.assertIn("--start 2022-01-01", joined)

    def test_bad_inputs_are_rejected_before_anything_runs(self) -> None:
        bad = [
            {"timeframe": "2h"},
            {"folds": 2},
            {"folds": 11},
            {"family": "macd"},
            {"symbols": ""},
            {"symbols": "BTCUSDT"},
            {"symbols": "BTC/USDT,ETH/USDT,SOL/USDT,XRP/USDT,ADA/USDT"},
            {"start": "2022/01/01"},
        ]
        for payload in bad:
            with self.subTest(payload=payload):
                with self.assertRaises(BadRequest):
                    build_walkforward_command(payload)

    def test_launch_returns_the_fresh_reading(self) -> None:
        self._study()
        with patch("quant.dashboard.server.subprocess.run") as run:
            run.return_value = self._completed()
            snap = launch_walkforward(
                {"timeframe": "4h", "folds": 5, "family": "both"},
                results_dir=self.dir,
            )
        self.assertEqual(run.call_count, 1)
        self.assertTrue(snap["available"])
        self.assertEqual(snap["families"][0]["label"], "EMA-ATR")

    def test_a_failed_run_is_a_bad_request_naming_the_reason(self) -> None:
        with patch("quant.dashboard.server.subprocess.run") as run:
            run.return_value = self._completed(2, "error: not enough bars per chunk")
            with self.assertRaises(BadRequest) as ctx:
                launch_walkforward({}, results_dir=self.dir)
        self.assertIn("not enough bars", str(ctx.exception))

    def test_post_route_reruns_and_returns_the_card_payload(self) -> None:
        self._study()
        with patch("quant.dashboard.server.subprocess.run") as run:
            run.return_value = self._completed()
            body = _serve_json(
                make_handler(RunIndex(self.dir)),
                "/api/walkforward/run",
                method="POST",
                payload={"timeframe": "4h", "folds": 5, "family": "both"},
            )
        self.assertTrue(body["available"])
        self.assertEqual(body["file"], "walkforward_rolling_4h.json")
        # The run is pinned to a dated file inside the dashboard's own results
        # directory, so a custom --results tree still reads back what it wrote.
        cmd = run.call_args[0][0]
        out = Path(cmd[cmd.index("--out") + 1])
        self.assertEqual(out.parent, self.dir)
        self.assertTrue(out.name.startswith("walkforward_rolling_4h_"))
        self.assertTrue(out.name.endswith(".json"))


class TestExcessBuildGuards(unittest.TestCase):
    """The excess build must fail loudly when its frozen sources are absent or
    stale, and the dashboard must not serve a stale snapshot."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        # The builder reads its sources from a repo-root-relative path, but it
        # honors ``$QUANT_EXCESS_ROOT`` so a test harness can point it at an
        # isolated results tree. Copy the builder into the temp dir and run it
        # directly there, with the env override, so the real workspace is never
        # touched.
        src = Path(__file__).resolve().parent.parent / "quant" / "build_dashboard_excess.py"
        self._builder = self.dir / "build_dashboard_excess.py"
        self._builder.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        self._py_env = {**subprocess.os.environ, "QUANT_EXCESS_ROOT": str(self.dir)}
        # Every guard fixture references "latest_active": "x.json" with an
        # excess of -1.0; the builder requires that artifact to exist and
        # agree before it reaches the per-source gates under test.
        (self.dir / "x.json").write_text(
            json.dumps({"metrics": {"excess_return_pct": -1.0}}), encoding="utf-8"
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- build script guards -------------------------------------------------

    def test_missing_status_json_fails_loudly(self) -> None:
        results = self.dir / "results"
        results.mkdir(exist_ok=True)
        (results / "explore_notes.json").write_text(
            json.dumps({"breadth_read": {"planned": 1, "ran": 1, "median_excess_pct": 0.0}})
        )
        (results / "robustness.json").write_text(
            json.dumps({"summary": {"median_excess_pct": 0.0, "best": {"label": "x"}, "planned": 1}, "nulls": []})
        )
        rc = subprocess.run(
            [sys.executable, str(self._builder)],
            cwd=self.dir,
            env=self._py_env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(rc.returncode, 0, "build should fail when status.json is missing")
        combined = rc.stdout + rc.stderr
        self.assertIn("status.json", combined, "failure message should name the missing file")

    def test_missing_explore_notes_json_fails_loudly(self) -> None:
        results = self.dir / "results"
        results.mkdir(exist_ok=True)
        (results / "status.json").write_text(
            json.dumps({
                "paper": {
                    "latest_active": "x.json",
                    "latest_metrics": {"excess_return_pct": -1.0, "symbol": "BTCUSDT", "trades": 1},
                }
            })
        )
        (results / "robustness.json").write_text(
            json.dumps({"summary": {"median_excess_pct": 0.0, "best": {"label": "x"}, "planned": 1}, "nulls": []})
        )
        rc = subprocess.run(
            [sys.executable, str(self._builder)],
            cwd=self.dir,
            env=self._py_env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(rc.returncode, 0)
        combined = rc.stdout + rc.stderr
        self.assertIn("explore_notes.json", combined)

    def test_missing_robustness_json_fails_loudly(self) -> None:
        results = self.dir / "results"
        results.mkdir(exist_ok=True)
        (results / "status.json").write_text(
            json.dumps({
                "paper": {
                    "latest_active": "x.json",
                    "latest_metrics": {"excess_return_pct": -1.0, "symbol": "BTCUSDT", "trades": 1},
                }
            })
        )
        (results / "explore_notes.json").write_text(
            json.dumps({"breadth_read": {"planned": 1, "ran": 1, "median_excess_pct": 0.0}})
        )
        rc = subprocess.run(
            [sys.executable, str(self._builder)],
            cwd=self.dir,
            env=self._py_env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(rc.returncode, 0)
        combined = rc.stdout + rc.stderr
        self.assertIn("robustness.json", combined)

    def test_stale_snapshot_rebuilds(self) -> None:
        results = self.dir / "results"
        results.mkdir(exist_ok=True)
        (results / "status.json").write_text(
            json.dumps({
                "paper": {
                    "latest_active": "x.json",
                    "latest_metrics": {"excess_return_pct": -1.0, "symbol": "BTCUSDT", "trades": 1},
                }
            })
        )
        (results / "explore_notes.json").write_text(
            json.dumps({"breadth_read": {"planned": 1, "ran": 1, "median_excess_pct": 0.0}})
        )
        (results / "robustness.json").write_text(
            json.dumps({"summary": {"median_excess_pct": 0.0, "best": {"label": "x"}, "planned": 1}, "nulls": []})
        )
        # Build once so a snapshot exists.
        rc1 = subprocess.run(
            [sys.executable, str(self._builder)],
            cwd=self.dir,
            env=self._py_env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(rc1.returncode, 0, rc1.stdout + rc1.stderr)
        snapshot = results / "dashboard_excess.json"
        self.assertTrue(snapshot.is_file())

        # Make one source newer than the snapshot. Keep the excess reading
        # consistent with the paper artifact (only bump an unchecked field),
        # so the build reaches the staleness gate instead of the artifact
        # agreement gate.
        time.sleep(0.05)
        (results / "status.json").write_text(
            json.dumps({
                "paper": {
                    "latest_active": "x.json",
                    "latest_metrics": {"excess_return_pct": -1.0, "symbol": "BTCUSDT", "trades": 2},
                }
            })
        )
        rc2 = subprocess.run(
            [sys.executable, str(self._builder)],
            cwd=self.dir,
            env=self._py_env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(rc2.returncode, 0, rc2.stdout + rc2.stderr)
        combined = rc2.stdout + rc2.stderr
        self.assertIn("stale", combined.lower())
        self.assertGreater(
            snapshot.stat().st_mtime,
            (results / "status.json").stat().st_mtime,
            "rebuild should leave the snapshot newer than its sources",
        )
        rebuilt = json.loads(snapshot.read_text(encoding="utf-8"))
        self.assertEqual(rebuilt["paper"]["trades"], 2)

    # -- server-side freshness guard ----------------------------------------

    def _handler_for(self) -> type:
        """Make a handler whose excess-source set points at this test's temp dir.

        The route ``/results/dashboard_excess.json`` is resolved by stripping the
        ``/results/`` route prefix and joining the remainder onto ``RESULTS_ROOT``, so
        ``RESULTS_ROOT`` must be the directory that contains the test's ``results/``
        subtree (i.e. ``self.dir / "results"``), not the tree root.

        The freshness check reads ``_EXCESS_SOURCES`` at request time, so a
        permanent monkeypatch (not a context-manager scope) is required for the
        guard to actually fire when the server thread handles the request.
        """
        import quant.dashboard.server as srv
        results = self.dir / "results"
        srv.RESULTS_ROOT = results
        srv._EXCESS_SOURCES = (results / "status.json", results / "explore_notes.json", results / "robustness.json")
        return make_handler(RunIndex(results))

    def test_excess_route_returns_404_when_sources_are_missing(self) -> None:
        results = self.dir / "results"
        results.mkdir(exist_ok=True)
        write(results, "dashboard_excess.json", {"paper": {"symbol": "x"}, "breadth": {}, "generated": ""})
        handler = self._handler_for()
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        port = httpd.server_address[1]
        httpd.timeout = 20
        serve_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        serve_thread.start()
        try:
            import socket
            import urllib.request
            import urllib.error
            # Wait until the server socket is actually accepting connections, otherwise the
            # first request can arrive before serve_forever has called handle_request().
            deadline = time.time() + 5.0
            while time.time() < deadline:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                        pass
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                self.fail("server did not start accepting connections in time")
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/results/dashboard_excess.json", timeout=5)
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 404)
                self.assertIn("stale", exc.read().decode().lower())
            else:
                self.fail("expected 404 when sources are missing")
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_excess_route_serves_when_sources_are_fresh(self) -> None:
        results = self.dir / "results"
        results.mkdir(exist_ok=True)
        (results / "status.json").write_text(
            json.dumps({
                "paper": {
                    "latest_active": "x.json",
                    "latest_metrics": {"excess_return_pct": -1.0, "symbol": "BTCUSDT", "trades": 1},
                }
            })
        )
        write(results, "explore_notes.json", {"breadth_read": {"planned": 1, "ran": 1, "median_excess_pct": 0.0}})
        write(results, "robustness.json", {"summary": {"median_excess_pct": 0.0, "best": {"label": "x"}, "planned": 1}, "nulls": []})
        write(results, "dashboard_excess.json", {
            "paper": {"symbol": "BTCUSDT"},
            "breadth": {"planned": 1, "ran": 1, "median_excess_pct": 0.0},
            "generated": "",
            "based_on": "",
            "notes": [],
            "verification": {},
        })
        handler = self._handler_for()
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        port = httpd.server_address[1]
        httpd.timeout = 20
        serve_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        serve_thread.start()
        try:
            import socket
            import urllib.request
            # Wait until the server socket is actually accepting connections, otherwise the
            # first request can arrive before serve_forever has called handle_request().
            deadline = time.time() + 5.0
            while time.time() < deadline:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                        pass
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                self.fail("server did not start accepting connections in time")
            body = urllib.request.urlopen(f"http://127.0.0.1:{port}/results/dashboard_excess.json", timeout=5).read()
            data = json.loads(body)
            self.assertEqual(data["paper"]["symbol"], "BTCUSDT")
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_excess_sources_fresh_returns_false_when_out_is_missing(self) -> None:
        self.assertFalse(_excess_sources_fresh(self.dir / "nonexistent.json"))


class TestRobustnessDashboardRefresh(unittest.TestCase):
    """A saved sweep refreshes the frozen breadth and excess snapshot."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.default = self.dir / "results" / "robustness.json"
        self.default.parent.mkdir()
        self.default.write_text("{}", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_bollinger_default_output_is_separate(self) -> None:
        import run_robustness

        self.assertEqual(run_robustness.default_out("ema"), run_robustness.DEFAULT_OUT)
        self.assertEqual(run_robustness.default_out("bollinger").name, "robustness_bollinger.json")

    def test_default_output_refreshes_chain_in_order(self) -> None:
        import run_robustness

        calls: list[str] = []
        with patch.object(run_robustness, "DEFAULT_OUT", self.default), \
             patch("quant.regenerate_frozen_reads.main", side_effect=lambda: calls.append("regen") or 0), \
             patch("quant.build_dashboard_excess.main", side_effect=lambda: calls.append("build") or 0):
            rc = run_robustness._refresh_dashboard_excess(self.default, json_mode=True)

        self.assertEqual(rc, 0)
        self.assertEqual(calls, ["regen", "build"])

    def test_custom_output_does_not_touch_dashboard(self) -> None:
        import run_robustness

        custom = self.dir / "elsewhere.json"
        custom.write_text("{}", encoding="utf-8")
        with patch.object(run_robustness, "DEFAULT_OUT", self.default), \
             patch("quant.regenerate_frozen_reads.main") as regen, \
             patch("quant.build_dashboard_excess.main") as build:
            rc = run_robustness._refresh_dashboard_excess(custom, json_mode=True)

        self.assertEqual(rc, 0)
        regen.assert_not_called()
        build.assert_not_called()

    def test_frozen_read_failure_skips_builder(self) -> None:
        import run_robustness

        calls: list[str] = []
        with patch.object(run_robustness, "DEFAULT_OUT", self.default), \
             patch("quant.regenerate_frozen_reads.main", side_effect=lambda: calls.append("regen") or 3), \
             patch("quant.build_dashboard_excess.main", side_effect=lambda: calls.append("build") or 0):
            rc = run_robustness._refresh_dashboard_excess(self.default, json_mode=True)

        self.assertEqual(rc, 3)
        self.assertEqual(calls, ["regen"])

    def test_main_refreshes_after_default_save(self) -> None:
        import contextlib
        import io
        import run_robustness

        class FakeReport:
            summary = {"ran": 1, "planned": 1}

            def to_dict(self) -> dict:
                return {"summary": self.summary}

            def __str__(self) -> str:
                return "fake report"

        output = io.StringIO()
        with patch.object(run_robustness, "DEFAULT_OUT", self.default), \
             patch.object(run_robustness, "run_study", return_value=FakeReport()), \
             patch.object(run_robustness, "_refresh_dashboard_excess", return_value=0) as refresh:
            with contextlib.redirect_stdout(output):
                rc = run_robustness.main(["--json", "--quiet"])

        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(output.getvalue()), {"summary": FakeReport.summary})
        refresh.assert_called_once_with(self.default, json_mode=True)

    def test_compare_reads_both_default_artifacts(self) -> None:
        import contextlib
        import io
        import run_robustness

        def payload(family, excess, p):
            return {
                "family": family,
                "grid_size": 15,
                "window": {"start": "2021-01-01", "end": "2026-11-08", "fee_bps": 10.0, "slippage_bps": 5.0},
                "summary": {
                    "planned": 15,
                    "ran": 15,
                    "beat_buy_hold": 0,
                    "beat_buy_hold_pct": 0.0,
                    "median_excess_pct": excess,
                    "median_sharpe": 0.1,
                    "total_trades": 100,
                    "median_p_value": p,
                    "by_market": [{"market": "BTC/USDT 1d", "ran": 15, "beat_buy_hold": 0, "median_excess_pct": excess, "p_value": p}],
                },
            }

        self.default.write_text(json.dumps(payload(None, -146.6, 0.08)), encoding="utf-8")
        (self.default.parent / "robustness_bollinger.json").write_text(
            json.dumps(payload("bollinger", -175.0, 0.28)), encoding="utf-8"
        )
        output = io.StringIO()
        with patch.object(run_robustness, "DEFAULT_OUT", self.default):
            with contextlib.redirect_stdout(output):
                rc = run_robustness.main(["--compare"])

        self.assertEqual(rc, 0)
        text = output.getvalue()
        self.assertIn("EMA-ATR", text)
        self.assertIn("Bollinger reversion", text)
        self.assertIn("BTC/USDT 1d", text)

    def test_bollinger_main_skips_ema_dashboard_refresh(self) -> None:
        import contextlib
        import io
        import run_robustness

        class FakeReport:
            summary = {"ran": 1, "planned": 1}
            family = "bollinger"

            def to_dict(self) -> dict:
                return {"family": self.family, "summary": self.summary}

            def __str__(self) -> str:
                return "fake report"

        output = io.StringIO()
        errors = io.StringIO()
        with patch.object(run_robustness, "DEFAULT_OUT", self.default), \
             patch.object(run_robustness, "run_study", return_value=FakeReport()) as study, \
             patch.object(run_robustness, "_refresh_dashboard_excess", return_value=0) as refresh:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                rc = run_robustness.main(["--family", "bollinger", "--json", "--quiet"])

        self.assertEqual(rc, 0)
        self.assertEqual(study.call_args.kwargs["family"], "bollinger")
        refresh.assert_not_called()
        self.assertIn("only the EMA sweep", errors.getvalue())
        self.assertTrue((self.default.parent / "robustness_bollinger.json").exists())

    def test_no_dashboard_refresh_skips_refresh(self) -> None:
        import run_robustness

        class FakeReport:
            summary = {"ran": 1, "planned": 1}

            def to_dict(self) -> dict:
                return {"summary": self.summary}

            def __str__(self) -> str:
                return "fake report"

        with patch.object(run_robustness, "DEFAULT_OUT", self.default), \
             patch.object(run_robustness, "run_study", return_value=FakeReport()), \
             patch.object(run_robustness, "_refresh_dashboard_excess", return_value=0) as refresh:
            rc = run_robustness.main(["--json", "--quiet", "--no-dashboard-refresh"])

        self.assertEqual(rc, 0)
        refresh.assert_not_called()


class TestFrozenReadPaperExcess(unittest.TestCase):
    """The frozen read uses the active paper value without inventing a cross-check."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.results = self.root / "results"
        self.results.mkdir()
        self.robustness = self.results / "robustness.json"
        self.out = self.results / "explore_notes.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_sweep(self, paper_excess: float | None = None) -> None:
        summary: dict[str, object] = {
            "planned": 1,
            "ran": 1,
            "median_excess_pct": -140.0,
            "by_market": [],
            "best": {},
            "verdict": "",
        }
        if paper_excess is not None:
            summary["paper_excess_pct"] = paper_excess
        write(self.results, "robustness.json", {"summary": summary, "cells": [], "nulls": []})

    def _run(self) -> int:
        import quant.regenerate_frozen_reads as regen

        with patch.object(regen, "ROOT", self.root), \
             patch.object(regen, "ROBUSTNESS", self.robustness), \
             patch.object(regen, "OUT", self.out):
            return regen.main()

    def test_latest_metrics_value_is_recorded_without_a_fake_cross_check(self) -> None:
        self._write_sweep()
        write(self.results, "status.json", {
            "paper": {"latest_metrics": {"excess_return_pct": -161.59403890969705}}
        })

        self.assertEqual(self._run(), 0)
        breadth = json.loads(self.out.read_text(encoding="utf-8"))["breadth_read"]
        self.assertEqual(breadth["paper_excess_pct"], -161.59403890969705)
        self.assertIsNone(breadth["paper_excess_from_sweep"])
        self.assertFalse(breadth["paper_excess_cross_checked_against_sweep"])

    def test_legacy_top_level_paper_excess_still_reads(self) -> None:
        self._write_sweep()
        write(self.results, "status.json", {"paper": {"excess_return_pct": -12.5}})

        self.assertEqual(self._run(), 0)
        breadth = json.loads(self.out.read_text(encoding="utf-8"))["breadth_read"]
        self.assertEqual(breadth["paper_excess_pct"], -12.5)
        self.assertFalse(breadth["paper_excess_cross_checked_against_sweep"])

    def test_explicit_sweep_value_is_cross_checked(self) -> None:
        self._write_sweep(paper_excess=-161.59403890969705)
        write(self.results, "status.json", {
            "paper": {"latest_metrics": {"excess_return_pct": -161.59403890969705}}
        })

        self.assertEqual(self._run(), 0)
        breadth = json.loads(self.out.read_text(encoding="utf-8"))["breadth_read"]
        self.assertEqual(breadth["paper_excess_from_sweep"], -161.59403890969705)
        self.assertTrue(breadth["paper_excess_cross_checked_against_sweep"])

    def test_mismatched_sweep_value_fails(self) -> None:
        self._write_sweep(paper_excess=-1.0)
        write(self.results, "status.json", {
            "paper": {"latest_metrics": {"excess_return_pct": -161.59403890969705}}
        })

        with self.assertRaises(AssertionError):
            self._run()
