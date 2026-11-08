#!/usr/bin/env python
"""Rebuild results/explore_notes.json's breadth_read block from results/robustness.json.

explore_notes.json is a frozen read of the sweep. When the sweep is re-run,
robustness.json gets new numbers but explore_notes.json keeps the old ones, so
the dashboard would show stale breadth numbers. This script closes that gap
without touching the interpretation/notes, which are qualitative and still
valid.

It does NOT re-run the sweep; it just re-reads the fresh robustness.json and
writes a fresh explore_notes.json breadth_read that matches it.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROBUSTNESS = ROOT / "results" / "robustness.json"
OUT = ROOT / "results" / "explore_notes.json"

GENERATED = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def cell_median_excess(cell: dict) -> float | None:
    r = cell.get("result")
    if not isinstance(r, dict):
        return None
    v = r.get("excess_return_pct")
    return float(v) if v is not None else None


def _median(values: list[float]) -> float:
    import statistics
    return float(statistics.median(values))


def _p25(values: list[float]) -> float:
    import statistics
    return float(statistics.median_low(values))


def _p75(values: list[float]) -> float:
    import statistics
    return float(statistics.median_high(values))


def main() -> int:
    rb = load(ROBUSTNESS)
    summary = rb.get("summary", {})
    by_market = summary.get("by_market", [])
    cells = rb.get("cells", [])
    nulls = rb.get("nulls", [])

    # Rebuild breadth_read from robustness.json so the dashboard shows the
    # fresh sweep numbers, not the stale frozen copy.
    planned = summary.get("planned", len(cells))
    ran = summary.get("ran", len(cells))

    ok_cells = [c for c in cells if not c.get("error")]
    excess_values = [c["result"]["excess_return_pct"] for c in ok_cells if isinstance(c.get("result"), dict) and c["result"].get("excess_return_pct") is not None]
    total_return_values = [c["result"]["total_return_pct"] for c in ok_cells if isinstance(c["result"], dict) and c["result"].get("total_return_pct") is not None]
    sharpe_values = [c["result"]["sharpe"] for c in ok_cells if isinstance(c["result"], dict) and c["result"].get("sharpe") is not None]
    dd_values = [c["result"]["max_drawdown_pct"] for c in ok_cells if isinstance(c["result"], dict) and c["result"].get("max_drawdown_pct") is not None]
    trades_values = [c["result"]["trades"] for c in ok_cells if isinstance(c["result"], dict) and c["result"].get("trades") is not None]

    median_excess = _median(excess_values) if excess_values else 0.0
    best = summary.get("best", {})
    best_cell = best.get("label") or (max(ok_cells, key=lambda c: (c.get("result",{}).get("excess_return_pct") or -1e9)).get("params_label") if ok_cells else None)
    best_excess = best.get("excess_pct") if best else (max(excess_values) if excess_values else 0.0)

    # by_market already comes from robustness.json summary, which is the live
    # source. Reuse it so the per-market rollup matches exactly.
    breadth_by_market = []
    for row in by_market:
        breadth_by_market.append({
            "market": row.get("market"),
            "cells": row.get("cells"),
            "beat_buy_hold_pct": row.get("beat_buy_hold_pct"),
            "median_excess_pct": row.get("median_excess_pct"),
            "median_sharpe": row.get("median_sharpe"),
            "trades": row.get("trades"),
            "best_excess_pct": row.get("best_excess_pct") or row.get("best_total_return_pct"),
        })

    # null_tests: rebuild from robustness.json nulls, keeping market-level
    # actual_return from the baseline cell if available.
    null_tests = []
    for n in nulls:
        null_tests.append({
            "market": n.get("market"),
            "actual_return_pct": n.get("actual_return_pct"),
            "null_median_pct": n.get("null_median_pct"),
            "null_p95_pct": n.get("null_p95_pct"),
            "p_value": n.get("p_value"),
        })

    breadth_read = {
        "planned": planned,
        "ran": ran,
        "beat_buy_hold_pct": summary.get("beat_buy_hold_pct", 0.0),
        "clean_pct": summary.get("clean_pct", 0.0),
        "median_excess_pct": median_excess,
        "median_total_return_pct": _median(total_return_values) if total_return_values else summary.get("median_total_return_pct", 0.0),
        "median_sharpe": _median(sharpe_values) if sharpe_values else summary.get("median_sharpe", 0.0),
        "median_max_drawdown_pct": _median(dd_values) if dd_values else summary.get("median_max_drawdown_pct", 0.0),
        "total_trades": summary.get("total_trades", sum(trades_values)),
        "best_cell": best_cell,
        "best_excess_pct": best_excess,
        "by_market": breadth_by_market,
        "null_tests": null_tests,
        "interpretation": rb.get("summary", {}).get("verdict", "") or "",
        "next_question": "Is the excess problem the same everywhere, or is there a better market/timeframe/setting shape worth chasing next?",
    }

    # Load existing explore_notes.json to preserve the qualitative notes and
    # the current_sweep_closed flag, then overwrite breadth_read.
    existing = {}
    if OUT.is_file():
        try:
            existing = load(OUT)
        except Exception:
            existing = {}

    out = {
        "generated": GENERATED,
        "based_on": "results/robustness.json (re-read after sweep re-run)",
        "current_sweep_closed": existing.get("current_sweep_closed", True),
        "breadth_read": breadth_read,
        "notes": existing.get("notes", []) if isinstance(existing.get("notes"), list) else [],
    }

    OUT.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")
    print(f"  breadth_read.median_excess_pct: {median_excess:.2f}  (robustness.summary.median_excess_pct: {summary.get('median_excess_pct')})")
    print(f"  breadth_read.planned/ran: {planned}/{ran}")
    print(f"  breadth_read.best_cell: {best_cell}")
    print(f"  null_tests: {len(null_tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
