#!/usr/bin/env python
"""Fuzz the new integrity runner against stale, missing, and hand-edited artifacts.

The point is not to rerun the whole sweep.  The point is to make sure the
integrity runner surfaces the same failure modes the dashboard builder is now
supposed to block upstream:
* a stale or missing paper.latest_active
* a missing paper artifact
* a hand-edited explore_notes.json that still parses but no longer matches robustness
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

# Import the runner under test.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import quant.check_excess_integrity as integrity  # noqa: E402


HERE = Path(__file__).resolve().parent.parent
REAL_RESULTS = HERE / "results"

SYMBOL = "BTCUSDT"
TIMEFRAME = "1d"
FILE = "BTCUSDT_1d_ema-atr-trend_20261016T100000Z.json"
ARTIFACT = {
    "symbol": SYMBOL,
    "timeframe": TIMEFRAME,
    "start": "2021-01-01 00:00:00",
    "end": "2026-10-16 00:00:00",
    "file": FILE,
    "trades": 17,
    "total_return_pct": 18.532484322115316,
    "buy_hold_return_pct": 180.12652323181237,
    "excess_return_pct": -161.59403890969705,
    "sharpe": 0.595960233305202,
    "max_drawdown_pct": -7.163900534690459,
    "fees_pct_of_start": 0.9597012574499365,
    "bars_in_market_pct": 17.06855791962175,
    "verdict": "Read with care: only 17 trades -- too few to be calm about; it did not beat buy & hold in this window.",
}

BREADTH_OK = {
    "planned": 60,
    "ran": 60,
    "beat_buy_hold_pct": 0.0,
    "clean_pct": 0.0,
    "median_excess_pct": -170.00504323926623,
    "median_total_return_pct": 18.77555039076454,
    "median_sharpe": 0.5071529121684613,
    "median_max_drawdown_pct": -11.642615999525024,
    "total_trades": 4039,
    "best_cell": "BTC/USDT 4h  30/60 s4.0 t5.0",
    "best_excess_pct": -56.36086295773248,
    "by_market": [
        {"market": "BTC/USDT 1d", "cells": 15, "trades": 244},
        {"market": "BTC/USDT 4h", "cells": 15, "trades": 1758},
    ],
    "null_tests": [{}, {}, {}, {}],
    "interpretation": "ok",
    "next_question": "next",
}

ROBUSTNESS_OK = {
    "window": {"start": "2021-01-01", "end": "2026-10-16", "label": "BTCUSDT 1d  opt-led sweep"},
    "cells": [
        {"label": "BTC/USDT 4h  30/60 s4.0 t5.0", "excess_return_pct": -56.36086295773248},
        {"label": "BTC/USDT 1d  30/60 s4.0 t5.0", "excess_return_pct": -143.6863878552155},
    ],
    "grid_size": {"planned_runs": 60, "ran_runs": 60, "markets": 2, "cells_per_market": 15},
    "summary": {
        "median_excess_pct": -170.00504323926623,
        "median_total_return_pct": 18.77555039076454,
        "median_sharpe": 0.5071529121684613,
        "median_max_drawdown_pct": -11.642615999525024,
        "total_trades": 4039,
        "beat_buy_hold_pct": 0.0,
        "clean_pct": 0.0,
    },
    "null_reference": {"kind": "label-shuffle", "n_epochs_per_cell": 200},
    "nulls": {
        "combined_by_market": [{}, {}, {}, {}],
        "by_market": [
            {
                "market": "BTC/USDT 1d",
                "actual_return_pct": 18.532484322115316,
                "null_median_pct": 4.32270407077694,
            }
        ],
    },
}


@contextmanager
def tmp_workspace():
    with tempfile.TemporaryDirectory(prefix="excess-integrity-") as d:
        root = Path(d) / "ws"
        root.mkdir()
        r = root / "results"
        r.mkdir()
        yield root, r


def write_artifact(r: Path, name: str, body: dict) -> Path:
    p = r / "paper" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(body, indent=2), encoding="utf-8")
    return p


def write_json(path: Path, body: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, indent=2), encoding="utf-8")


def status_for(paper_path: Path, active_name: str | None = None) -> dict:
    target = active_name or paper_path.name
    return {"paper": {"latest_active": str(paper_path.parent / target)}}


def run_in(r: Path) -> tuple[dict, int]:
    saved_results = integrity.RESULTS
    try:
        integrity.RESULTS = r
        state = integrity._load
        # Patch the module's loader so it reads from the tmp tree we chose.
        integrity._load = lambda p: json.loads(p.read_text(encoding="utf-8"))
        result, rc = integrity.run()
    finally:
        integrity.RESULTS = saved_results
        integrity._load = state
    return result, rc


def snapshot_case(name: str, r: Path, write_snap: bool) -> None:
    paper = write_artifact(r, FILE, ARTIFACT)
    status_for_path = paper.parent / "status.json"
    write_json(status_for_path, status_for(paper))
    explore_path = r / "explore_notes.json"
    write_json(explore_path, {"response": "ok", "breadth_read": BREADTH_OK})
    robustness_path = r / "robustness.json"
    write_json(robustness_path, ROBUSTNESS_OK)
    if write_snap:
        snap = r / "dashboard_excess.json"
        write_json(
            snap,
            {
                "generated": "2026-01-01T00:00:00Z",
                "based_on": "dummy",
                "paper": ARTIFACT,
                "breadth": BREADTH_OK,
                "verification": {
                    "breadth_median_matches_robustness": True,
                    "breadth_best_cell_matches_robustness": True,
                    "breadth_planned_matches_robustness": True,
                    "breadth_nulls_count_matches_robustness": True,
                    "paper_excess_from_status_reading": True,
                    "paper_excess_source": str(paper),
                    "paper_excess_source_exists": True,
                    "paper_excess_source_matches_active_artifact": True,
                    "paper_excess_value": ARTIFACT["excess_return_pct"],
                    "robustness_median_excess_pct": ROBUSTNESS_OK["summary"]["median_excess_pct"],
                },
            },
        )

    result, rc = run_in(r)
    failed = [s["name"] for s in result["stages"].values() if not s["ok"]]
    print(f"[{name}] rc={rc} failed={len(failed)}")
    for f in failed:
        print(f"  - {f}")


def main() -> int:
    print("=== helpers ===")
    print("integrity.RESULTS:", integrity.RESULTS)
    print("integrity.SNAPSHOT_PATH:", integrity.SNAPSHOT_PATH)
    print("integrity.ROBUSTNESS_PATH:", integrity.ROBUSTNESS_PATH)

    print("\n=== case A: clean workspace, no snapshot ===")
    with tmp_workspace() as (root, r):
        snapshot_case("clean-no-snapshot", r, write_snap=False)

    print("\n=== case B: clean workspace, snapshot present ===")
    with tmp_workspace() as (root, r):
        snapshot_case("clean-with-snapshot", r, write_snap=True)

    print("\n=== case C: status points at a missing paper artifact ===")
    with tmp_workspace() as (root, r):
        paper = write_artifact(r, FILE, ARTIFACT)
        missing = paper.parent / "MISSING_artifact.json"
        write_json(r / "status.json", status_for(paper, active_name=missing.name))
        write_json(r / "explore_notes.json", {"response": "ok", "breadth_read": BREADTH_OK})
        write_json(r / "robustness.json", ROBUSTNESS_OK)
        result, rc = run_in(r)
        failed = [s["name"] for s in result["stages"].values() if not s["ok"]]
        print(f"[status points at missing artifact] rc={rc} failed={len(failed)}")
        for f in failed:
            print(f"  - {f}")

    print("\n=== case D: hand-edited explore_notes that no longer matches robustness ===")
    with tmp_workspace() as (root, r):
        paper = write_artifact(r, FILE, ARTIFACT)
        write_json(r / "status.json", status_for(paper))
        fake_breadth = {
            **BREADTH_OK,
            "planned": 999,
            "best_cell": "HAND-LOCKED EVIL MARKET",
            "median_excess_pct": 222.0,
        }
        write_json(r / "explore_notes.json", {"response": "ok", "breadth_read": fake_breadth})
        write_json(r / "robustness.json", ROBUSTNESS_OK)
        result, rc = run_in(r)
        failed = [s["name"] for s in result["stages"].values() if not s["ok"]]
        print(f"[hand-edited breadth] rc={rc} failed={len(failed)}")
        for f in failed:
            print(f"  - {f}")

    print("\n=== case E: stale paper artifact with wrong excess_return_pct ===")
    with tmp_workspace() as (root, r):
        paper = write_artifact(r, FILE, {**ARTIFACT, "excess_return_pct": 999.0})
        write_json(r / "status.json", status_for(paper))
        write_json(r / "explore_notes.json", {"response": "ok", "breadth_read": BREADTH_OK})
        write_json(r / "robustness.json", ROBUSTNESS_OK)
        result, rc = run_in(r)
        failed = [s["name"] for s in result["stages"].values() if not s["ok"]]
        print(f"[stale paper excess] rc={rc} failed={len(failed)}")
        for f in failed:
            print(f"  - {f}")

    print("\n=== case F: artifact missing excess_return_pct key ===")
    with tmp_workspace() as (root, r):
        paper = write_artifact(r, FILE, {k: v for k, v in ARTIFACT.items() if k != "excess_return_pct"})
        write_json(r / "status.json", status_for(paper))
        write_json(r / "explore_notes.json", {"response": "ok", "breadth_read": BREADTH_OK})
        write_json(r / "robustness.json", ROBUSTNESS_OK)
        result, rc = run_in(r)
        failed = [s["name"] for s in result["stages"].values() if not s["ok"]]
        print(f"[artifact missing excess key] rc={rc} failed={len(failed)}")
        for f in failed:
            print(f"  - {f}")

    print("\n=== case G: robustness missing cells/summary/nulls ===")
    with tmp_workspace() as (root, r):
        paper = write_artifact(r, FILE, ARTIFACT)
        write_json(r / "status.json", status_for(paper))
        write_json(r / "explore_notes.json", {"response": "ok", "breadth_read": BREADTH_OK})
        write_json(r / "robustness.json", {"cells": [], "grid_size": {}, "summary": {}, "nulls": {}})
        result, rc = run_in(r)
        failed = [s["name"] for s in result["stages"].values() if not s["ok"]]
        print(f"[weak robustness] rc={rc} failed={len(failed)}")
        for f in failed:
            print(f"  - {f}")

    print("\n=== case H: explore_notes.json is not JSON ===")
    with tmp_workspace() as (root, r):
        paper = write_artifact(r, FILE, ARTIFACT)
        write_json(r / "status.json", status_for(paper))
        (r / "explore_notes.json").write_text("not json {{{", encoding="utf-8")
        write_json(r / "robustness.json", ROBUSTNESS_OK)
        result, rc = run_in(r)
        failed = [s["name"] for s in result["stages"].values() if not s["ok"]]
        print(f"[corrupt explore_notes] rc={rc} failed={len(failed)}")
        for f in failed:
            print(f"  - {f}")

    print("\n=== case I: robustness.json is not JSON ===")
    with tmp_workspace() as (root, r):
        paper = write_artifact(r, FILE, ARTIFACT)
        write_json(r / "status.json", status_for(paper))
        write_json(r / "explore_notes.json", {"response": "ok", "breadth_read": BREADTH_OK})
        (r / "robustness.json").write_text("not json {{{", encoding="utf-8")
        result, rc = run_in(r)
        failed = [s["name"] for s in result["stages"].values() if not s["ok"]]
        print(f"[corrupt robustness] rc={rc} failed={len(failed)}")
        for f in failed:
            print(f"  - {f}")

    print("\n=== case J: snapshot verification block says False after a bad build ===")
    with tmp_workspace() as (root, r):
        paper = write_artifact(r, FILE, ARTIFACT)
        write_json(r / "status.json", status_for(paper))
        write_json(r / "explore_notes.json", {"response": "ok", "breadth_read": BREADTH_OK})
        write_json(r / "robustness.json", ROBUSTNESS_OK)
        snap = r / "dashboard_excess.json"
        write_json(
            snap,
            {
                "generated": "2026-01-01T00:00:00Z",
                "based_on": "dummy",
                "paper": ARTIFACT,
                "breadth": BREADTH_OK,
                "notes": [],
                "verification": {
                    "breadth_median_matches_robustness": False,
                    "breadth_best_cell_matches_robustness": False,
                    "breadth_planned_matches_robustness": False,
                    "breadth_nulls_count_matches_robustness": False,
                    "paper_excess_from_status_reading": True,
                    "paper_excess_source": str(paper),
                    "paper_excess_source_exists": True,
                    "paper_excess_source_matches_active_artifact": True,
                },
            },
        )
        result, rc = run_in(r)
        failed = [s["name"] for s in result["stages"].values() if not s["ok"]]
        print(f"[snapshot verifier says False] rc={rc} failed={len(failed)}")
        for f in failed:
            print(f"  - {f}")

    print("\n=== case K: paper.latest_active is empty string ===")
    with tmp_workspace() as (root, r):
        paper = write_artifact(r, FILE, ARTIFACT)
        write_json(r / "status.json", {"paper": {"latest_active": ""}})
        write_json(r / "explore_notes.json", {"response": "ok", "breadth_read": BREADTH_OK})
        write_json(r / "robustness.json", ROBUSTNESS_OK)
        result, rc = run_in(r)
        failed = [s["name"] for s in result["stages"].values() if not s["ok"]]
        print(f"[empty latest_active] rc={rc} failed={len(failed)}")
        for f in failed:
            print(f"  - {f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
