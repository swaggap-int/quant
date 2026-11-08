#!/usr/bin/env python
"""Build the dashboard excess JSON from the frozen result files.

Reads:
- results/status.json   -> single-paper excess reading
- results/explore_notes.json   -> swept breadth + null-test frozen read
- results/robustness.json   -> live truth for the verification assertions

Writes:
- results/dashboard_excess.json

The served snapshot is a frozen read of status.json + explore_notes.json.
It is NOT a direct rewrite of results/robustness.json. In particular:

- paper.excess_return_pct is the active paper artifact's SINGLE-WINDOW excess.
- results/robustness.json is a MULTI-CELL sweep summary.
- results/robustness.json has no equivalent single-paper excess field, so this
  builder does not assert a live cross-check for paper.excess_return_pct here.

The active paper source is controlled: when this builder writes
verification.paper_excess_source, the referenced file must exist and its
metrics must match the paper block this builder consumed. That prevents a
stale or wrong paper source from being published silently.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import time

_ROOT_OVERRIDE = os.environ.get("QUANT_EXCESS_ROOT")
if _ROOT_OVERRIDE:
    ROOT = Path(_ROOT_OVERRIDE).resolve()
else:
    ROOT = Path(__file__).resolve().parent.parent

RESULTS = ROOT / "results"
STATUS = RESULTS / "status.json"
EXPLORE = RESULTS / "explore_notes.json"
ROBUSTNESS = RESULTS / "robustness.json"
OUT = RESULTS / "dashboard_excess.json"

GENERATED = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _read_or_missing(path: Path) -> tuple[dict | None, str | None]:
    """Read ``path`` as JSON, or report why it is unusable."""
    if not path.is_file():
        return None, f"missing {path.name}"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"cannot read {path.name}: {exc}"
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"{path.name} is not valid JSON: {exc}"
    if not isinstance(payload, dict):
        return None, f"{path.name} is not a JSON object"
    return payload, None


def _freshness_guard(files: list[Path], out: Path) -> str | None:
    """Return a message when the snapshot would be stale.

    The displayed metric snapshot is a frozen read of status.json
    and explore_notes.json. It should be rebuilt whenever either of
    those inputs changes, so it does not silently drift away from
    the source files.
    """
    try:
        out_mtime = out.stat().st_mtime
    except OSError:
        return None
    for src in files:
        try:
            src_mtime = src.stat().st_mtime
        except OSError:
            continue
        if src_mtime > out_mtime:
            return (
                f"stale {out.name}: {src.name} was written at "
                f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(src_mtime))} "
                f"which is newer than {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(out_mtime))}"
            )
    return None


def fresh(results: Path) -> bool:
    """Is results/dashboard_excess.json present and fresh relative to its sources?"""
    out = results / "dashboard_excess.json"
    if not out.is_file():
        return False
    try:
        out_mtime = out.stat().st_mtime
    except OSError:
        return False
    for name in ("status.json", "explore_notes.json", "robustness.json"):
        src = results / name
        if not src.is_file():
            return False
        try:
            if src.stat().st_mtime > out_mtime:
                return False
        except OSError:
            return False
    return True


def load(path: Path) -> dict:
    payload, reason = _read_or_missing(path)
    if payload is None:
        raise RuntimeError(reason)
    return payload


def _active_paper_path_from_status(status: dict) -> Path | None:
    candidates = [
        status.get("paper", {}).get("latest_active"),
        status.get("latest_active"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        p = Path(candidate)
        if not p.is_file():
            return None
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        return p
    return None


def _paper_metrics_from_status(status: dict) -> dict | None:
    m = (
        status.get("paper", {}).get("latest_metrics")
        or status.get("latest_metrics")
        or status.get("paper", {}).get("result")
        or status.get("result")
    )
    if isinstance(m, dict) and ("excess_return_pct" in m or "total_return_pct" in m):
        return dict(m)
    return None


def _normalize_paper(status: dict) -> dict:
    metrics = _paper_metrics_from_status(status)
    if metrics is None:
        return {}
    latest_active = status.get("paper", {}).get("latest_active") or status.get("latest_active")
    return {
        "file": (Path(latest_active).name if latest_active else None),
        "latest_active": latest_active,
        "symbol": metrics.get("symbol") or status.get("paper", {}).get("symbol"),
        "timeframe": metrics.get("timeframe") or status.get("paper", {}).get("timeframe"),
        "start": metrics.get("start") or status.get("paper", {}).get("start"),
        "end": metrics.get("end") or status.get("paper", {}).get("end"),
        "trades": metrics.get("trades"),
        "total_return_pct": metrics.get("total_return_pct"),
        "buy_hold_return_pct": metrics.get("buy_hold_return_pct"),
        "excess_return_pct": metrics.get("excess_return_pct"),
        "sharpe": metrics.get("sharpe"),
        "max_drawdown_pct": metrics.get("max_drawdown_pct"),
        "fees_pct_of_start": metrics.get("fees_pct_of_start"),
        "bars_in_market_pct": metrics.get("bars_in_market_pct"),
        "verdict": metrics.get("verdict") or status.get("paper", {}).get("verdict"),
        "latest_metrics": metrics,
    }


def main() -> int:
    sources = [STATUS, EXPLORE, ROBUSTNESS]

    status = load(STATUS)
    paper = _normalize_paper(status)

    if not paper.get("excess_return_pct") and paper.get("total_return_pct") is None:
        print("status.json has no usable paper reading; stop")
        return 1

    active_paper_path = _active_paper_path_from_status(status)
    if active_paper_path is None:
        print("active paper artifact referenced by status.json is missing or unreadable; stop")
        return 1

    payload = json.loads(active_paper_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        print("active paper artifact is not a JSON object; stop")
        return 1
    active_metrics = payload.get("metrics") or {}

    explore = load(EXPLORE)
    sweep = explore.get("breadth_read")
    notes = explore.get("notes") or status.get("notes") or []
    if sweep is None:
        print("explore_notes.json has no breadth_read block; stop")
        return 1

    robustness = load(ROBUSTNESS)

    stale = _freshness_guard(sources, OUT)
    if stale:
        # Replacing a stale snapshot is this command's job: warn, then rebuild
        # instead of refusing until someone manually bumps the file's mtime.
        print(f"{stale}; rebuilding")

    paper_excess_from_status = bool(
        paper.get("excess_return_pct") == status.get("paper", {}).get("latest_metrics", {}).get("excess_return_pct")
    )

    paper_excess_source = paper.get("latest_active") or paper.get("file")
    if isinstance(paper_excess_source, str) and not paper_excess_source.strip():
        paper_excess_source = None
    if not paper_excess_source:
        print("no paper_excess_source identifier available; stop")
        return 1

    src_path = Path(paper_excess_source)
    paper_excess_source = str(src_path.resolve())
    src_path = Path(paper_excess_source)
    if not src_path.is_file():
        print(f"active paper source {paper_excess_source} does not exist; stop")
        return 1
    if abs((active_metrics.get("excess_return_pct") or 0) - (paper.get("excess_return_pct") or 0)) >= 1e-9:
        print(
            f"active paper source {paper_excess_source} contradicts the paper block from status.json; "
            f"active_metrics.excess_return_pct={active_metrics.get('excess_return_pct')} "
            f"paper.excess_return_pct={paper.get('excess_return_pct')}; stop"
        )
        return 1

    excess = {
        "generated": GENERATED,
        "based_on": "results/status.json + results/explore_notes.json (frozen breadcrumb from the current sweep)",
        "paper": {
            "symbol": paper.get("symbol"),
            "timeframe": paper.get("timeframe"),
            "start": paper.get("start"),
            "end": paper.get("end"),
            "file": paper.get("file"),
            "trades": paper.get("trades"),
            "total_return_pct": paper.get("total_return_pct"),
            "buy_hold_return_pct": paper.get("buy_hold_return_pct"),
            "excess_return_pct": paper.get("excess_return_pct"),
            "sharpe": paper.get("sharpe"),
            "max_drawdown_pct": paper.get("max_drawdown_pct"),
            "fees_pct_of_start": paper.get("fees_pct_of_start"),
            "bars_in_market_pct": paper.get("bars_in_market_pct"),
            "verdict": paper.get("verdict"),
        },
        "breadth": {
            "planned": sweep.get("planned"),
            "ran": sweep.get("ran"),
            "beat_buy_hold_pct": sweep.get("beat_buy_hold_pct"),
            "clean_pct": sweep.get("clean_pct"),
            "median_excess_pct": sweep.get("median_excess_pct"),
            "median_total_return_pct": sweep.get("median_total_return_pct"),
            "median_sharpe": sweep.get("median_sharpe"),
            "median_max_drawdown_pct": sweep.get("median_max_drawdown_pct"),
            "total_trades": sweep.get("total_trades"),
            "best_cell": sweep.get("best_cell"),
            "best_excess_pct": sweep.get("best_excess_pct"),
            "by_market": sweep.get("by_market"),
            "null_tests": sweep.get("null_tests"),
            "interpretation": sweep.get("interpretation"),
            "next_question": sweep.get("next_question"),
        },
        "notes": notes if isinstance(notes, list) else [],
        "verification": {
            "breadth_median_matches_robustness": sweep.get("median_excess_pct") == robustness.get("summary", {}).get("median_excess_pct"),
            "breadth_best_cell_matches_robustness": (
                sweep.get("best_cell") == robustness.get("summary", {}).get("best", {}).get("label")
            ),
            "breadth_planned_matches_robustness": sweep.get("planned") == robustness.get("summary", {}).get("planned"),
            "breadth_nulls_count_matches_robustness": (len(sweep.get("null_tests") or []) == len(robustness.get("nulls") or [])),
            "breadth_median_value": sweep.get("median_excess_pct"),
            "robustness_median_excess_pct": robustness.get("summary", {}).get("median_excess_pct"),
            "paper_excess_value": paper.get("excess_return_pct"),
            "paper_excess_source": paper_excess_source,
            "paper_excess_source_exists": True,
            "paper_excess_source_matches_active_artifact": abs((active_metrics.get("excess_return_pct") or 0) - (paper.get("excess_return_pct") or 0)) < 1e-9,
            "paper_excess_from_status_reading": paper_excess_from_status,
            "note": (
                "paper.excess_return_pct is the active paper artifact's single-window excess; "
                "it is not a sweep result. results/robustness.json is a multi-cell sweep summary "
                "and has no equivalent single-paper excess field, so this builder does not assert "
                "a live cross-check for paper.excess_return_pct here. "
                "Do not hand-edit this note to make "
                "paper.excess_return_pct and breadth.median_excess_pct look equal. "
                "They measure different things on purpose. "
                "paper.excess_return_pct and breadth.median_excess_pct are different measurements by design: "
                "one is the active paper artifact's single-window excess vs buy & hold for that one run; "
                "the other is the median across the swept cells in the current robustness study."
            ),
            "type": "applied",
        },
    }

    OUT.write_text(json.dumps(excess, indent=2) + "\n", encoding="utf-8")
    print("wrote", OUT)
    print("  paper.bits  ->", excess["paper"].get("symbol"), excess["paper"].get("timeframe"))
    print("  breadth.planned / ran ->", excess["breadth"].get("planned"), "/", excess["breadth"].get("ran"))
    print("  verification:")
    for k, v in excess["verification"].items():
        print(f"    {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
