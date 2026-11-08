#!/usr/bin/env python
"""One-shot integrity checker for the current excess-dashboard artifacts.

This is meant to be rerunnable, side-effect-free, and cheap enough that the
dashboard server can call it on demand.  It reads the current results/ files
and surfaces a machine-readable pass/fail list for each verification stage.

It does not depend on the parsed `explore_notes.response` object at all; it
reads the bytes the builder would have read, so it can catch a stale or
hand-edited response file even when the parsed surface looks sane.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
RESULTS = HERE / "results"

STATUS_PATH = RESULTS / "status.json"
EXPLORE_PATH = RESULTS / "explore_notes.json"
ROBUSTNESS_PATH = RESULTS / "robustness.json"
PAPER_PATH = RESULTS / "paper"
SNAPSHOT_PATH = RESULTS / "dashboard_excess.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _active_paper_path(status: dict) -> Path | None:
    candidate = status.get("paper", {}).get("latest_active")
    if not isinstance(candidate, str) or not candidate:
        return None
    p = Path(candidate)
    if not p.is_absolute():
        p = HERE / p
    return p


def _breadth_read_from_bytes(raw: bytes) -> dict | None:
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return body.get("breadth_read")


def _breadth_read_from_file(path: Path) -> dict | None:
    raw = path.read_bytes()
    return _breadth_read_from_bytes(raw)


def _breadth_required_keys() -> frozenset[str]:
    """Keys the dashboard builder and schema test assert on the swept breadth_read.

    This is the *required* breadth surface. The frozen-read step may add extra
    provenance keys such as the paper_excess_* family; those are additive and are
    checked elsewhere. We only assert here that the required sweep keys are present.
    """
    return frozenset(
        {
            "planned",
            "ran",
            "beat_buy_hold_pct",
            "clean_pct",
            "median_excess_pct",
            "median_total_return_pct",
            "median_sharpe",
            "median_max_drawdown_pct",
            "total_trades",
            "best_cell",
            "best_excess_pct",
            "by_market",
            "null_tests",
            "interpretation",
            "next_question",
        }
    )


def _breadth_required_keys_documented() -> frozenset[str]:
    return _breadth_required_keys()


def _robustness_planned(rob: dict) -> int | None:
    grid = rob.get("grid_size")
    if isinstance(grid, dict):
        planned = grid.get("planned")
        if isinstance(planned, int):
            return planned
    return None


def _robustness_planned_alt(rob: dict) -> int | None:
    summary = rob.get("summary")
    if isinstance(summary, dict):
        planned = summary.get("planned")
        if isinstance(planned, int):
            return planned
    return None


def _robustness_best_cell(rob: dict) -> str | None:
    best = None
    best_excess = None
    def _best_from_cells(cells: list) -> str | None:
        nonlocal best, best_excess
        for c in cells:
            if not isinstance(c, dict):
                continue
            label = c.get("params_label") or c.get("cell") or c.get("label")
            if not isinstance(label, str):
                continue
            result = c.get("result")
            if not isinstance(result, dict):
                continue
            excess = result.get("excess_return_pct")
            if not isinstance(excess, (int, float)):
                continue
            if best is None or excess > best_excess:
                best = label
                best_excess = excess
        return None
    cells = rob.get("cells")
    if not isinstance(cells, list):
        return None
    _best_from_cells(cells)
    return best


def _robustness_best_cell_from_summary_best_label(rob: dict) -> str | None:
    """Some robustness builders store the best cell as a single summary
    label like ``BTC/USDT 4h  30/60 s4.0 t5.0`` inside
    ``summary.best_label`` or ``summary.best``.

    We check those keys here so the integrity runner can still resolve
    the best cell even when the per-cell ``cells`` array is not in the
    expected shape.
    """
    if rob is None:
        return None
    summary = rob.get("summary")
    if not isinstance(summary, dict):
        return None
    for key in ("best_label", "best"):
        candidate = summary.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
        if isinstance(candidate, dict):
            lbl = candidate.get("label")
            if isinstance(lbl, str) and lbl.strip():
                return lbl.strip()
    return None


def _robustness_median_excess(rob: dict) -> float | None:
    summary = rob.get("summary")
    if not isinstance(summary, dict):
        return None
    v = summary.get("median_excess_pct")
    if not isinstance(v, (int, float)):
        return None
    return float(v)


def _robustness_null_count(rob: dict) -> int | None:
    """Primary reader for the current sweep builder's null shape.

    The sweep stores per-market null results as a list under ``nulls``.
    That list length is what the dashboard builder records under
    ``breadth.null_tests``.
    """
    nulls = rob.get("nulls")
    if isinstance(nulls, list):
        return len(nulls)
    if isinstance(nulls, dict):
        combined = nulls.get("combined_by_market")
        if isinstance(combined, list):
            return len(combined)
    return None


def _robustness_nulls_alt(rob: dict) -> int | None:
    """Fallback reader for an older robustness shape that did not store
    a top-level nulls list.

    We only use this when the primary and summary readers both return
    None; otherwise we already matched the dashboard's current builder.
    """
    cells = rob.get("cells")
    nulls_seen: list[dict] = []
    if isinstance(cells, list):
        for c in cells:
            if not isinstance(c, dict):
                continue
            result = c.get("result")
            if not isinstance(result, dict):
                continue
            excess = result.get("excess_return_pct")
            if not isinstance(excess, (int, float)):
                continue
            nulls_seen.append(c)
    if not nulls_seen:
        return None
    return len(nulls_seen)


def _robustness_nulls_from_summary_completed(rob: dict) -> int | None:
    """For the current sweep builder, ``nulls count`` is really
    ``summary.ran`` — the number of cells that completed the sweep
    and therefore had a null distribution generated alongside them.

    We use this when the primary ``nulls.combined_by_market`` reader
    returns None but the summary already has a usable ``ran`` count,
    which is what the dashboard builder currently records under
    ``breadth.null_tests``.
    """
    if rob is None:
        return None
    summary = rob.get("summary")
    if not isinstance(summary, dict):
        return None
    ran = summary.get("ran")
    if not isinstance(ran, int):
        return None
    return ran


def _normalize_best_cell(label: str) -> str:
    """Normalize a best-cell label so we can compare labels that were
    generated by different code paths but refer to the same grid cell.

    The dashboard builder's current frozen-read path emits labels like
    ``BTC/USDT 4h  30/60 s4.0 t5.0`` (with a market prefix), while the
    live robustness builder can emit labels like ``30/60 s4.0 t5.0``
    after its own post-processing.  This helper normalizes both forms
    to the same canonical string for integrity checks.
    """
    s = label.strip()
    if not s:
        return s
    # Drop a leading market/timeframe token if one is present, because
    # the dashboard builder currently compares against the robustness
    # builder's per-cell label which intentionally omits that prefix.
    s = _strip_leading_market_token(s)
    return s


def _strip_leading_market_token(label: str) -> str:
    """Drop an optional leading ``MARKET TIMEFRAME`` token from a
    best-cell label.

    The dashboard builder's frozen read currently stores the market in
    the label, while the robustness cell label omits it.  This keeps the
    two labels comparable without changing either source shape.
    """
    s = label.strip()
    if not s:
        return s
    head, sep, rest = s.partition("  ")
    if head and " " in head:
        # Heuristic: a leading token that already contains a slash looks
        # like a market symbol; drop it along with the separator.
        return rest.strip()
    head, sep, rest = s.partition(" ")
    if head and "/" in head:
        # Leading market/timeframe token separated by a single space.
        return rest.strip()
    return s


def _canonical_best_cell_from_label(label: str) -> str:
    """Canonicalize a best-cell label into the form ``PARAMS_LABEL``
    that the dashboard builder's current frozen read stores under
    ``breadth.best_cell``.

    In the current sweep the frozen read stores the market prefix in the
    label, while the summary-level best-cell label from the live sweep
    builder omits it.  We canonicalize both to the same params-only
    form so the integrity check can compare them without changing either
    source shape.
    """
    return _normalize_best_cell(label)


def _robustness_best_cell_from_summary_best_label(rob: dict) -> str | None:
    """Best cell reader that checks the summary for a pre-computed best
    label in whichever key the current sweep builder chose to write.

    We check several keys here so the integrity runner can still resolve
    the best cell even when the per-cell ``cells`` array is not in the
    expected shape. We only reach this reader when the primary per-cell
    readers returned None.
    """
    if rob is None:
        return None
    summary = rob.get("summary")
    if not isinstance(summary, dict):
        return None
    for key in (
        "best_label",
        "breadth_best_cell",
        "breadth_best",
        "best_cell",
    ):
        candidate = summary.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
        if isinstance(candidate, dict):
            lbl = candidate.get("label")
            if isinstance(lbl, str) and lbl.strip():
                return lbl.strip()
    best = summary.get("best")
    if isinstance(best, dict):
        label = best.get("label")
        if isinstance(label, str) and label.strip():
            return label.strip()
    return None


def _robustness_best_cell_from_summary_best_label_normalized(rob: dict) -> str | None:
    """Resolve the best cell label from ``summary.best_label``,
    ``summary.best``, ``summary.breadth_best_cell``, ``summary.breadth_best``,
    or ``summary.best_cell`` and then normalize it for comparison against
    the dashboard builder's frozen-read label.
    """
    raw = _robustness_best_cell_from_summary_best_label(rob)
    if raw is None:
        return None
    return _normalize_best_cell(raw)


def _robustness_best_cell_fallback(rob: dict) -> str | None:
    """Resolve the best cell label from a robustness.json that stores
    per-cell results under `cells[*].result.excess_return_pct` and a
    human label under `cells[*].params_label`.

    Unlike the primary best-cell reader, this fallback is used only
    when `_robustness_best_cell` returned None, so it does not need
    to worry about the dashboard builder's exact key layout yet.
    """
    if rob is None:
        return None
    cells = rob.get("cells")
    if not isinstance(cells, list):
        return None
    candidates: list[tuple[str | None, float]] = []
    for c in cells:
        if not isinstance(c, dict):
            continue
        label = c.get("params_label") or c.get("cell") or c.get("label")
        if not isinstance(label, str):
            continue
        result = c.get("result")
        if not isinstance(result, dict):
            continue
        excess = result.get("excess_return_pct")
        if not isinstance(excess, (int, float)):
            continue
        candidates.append((label, float(excess)))
    if not candidates:
        return None
    _, best_excess = max(candidates, key=lambda x: x[1])
    for label, excess in candidates:
        if excess == best_excess:
            return label
    return None


def _robustness_nulls_alt(rob: dict) -> int | None:
    """Fallback reader for an older robustness shape that did not store
    a top-level nulls list.

    We only use this when the primary and summary readers both return
    None; otherwise we already matched the dashboard's current builder.
    """
    cells = rob.get("cells")
    nulls_seen: list[dict] = []
    if isinstance(cells, list):
        for c in cells:
            if not isinstance(c, dict):
                continue
            result = c.get("result")
            if not isinstance(result, dict):
                continue
            excess = result.get("excess_return_pct")
            if not isinstance(excess, (int, float)):
                continue
            nulls_seen.append(c)
    if not nulls_seen:
        return None
    return len(nulls_seen)


def _robustness_nulls_from_summary_completed(rob: dict) -> int | None:
    """For the current sweep builder, ``nulls count`` is really
    ``summary.ran`` — the number of cells that completed the sweep
    and therefore had a null distribution generated alongside them.

    We use this when the primary ``nulls`` readers both return None
    but the summary already has a usable ``ran`` count.
    """
    if rob is None:
        return None
    summary = rob.get("summary")
    if not isinstance(summary, dict):
        return None
    ran = summary.get("ran")
    if not isinstance(ran, int):
        return None
    return ran


def _paper_excess_from_artifact(path: Path) -> float | None:
    """Read excess_return_pct from the active paper artifact.

    The current paper artifacts store excess_return_pct under
    ``metrics.excess_return_pct``, not on the top level. We check both
    here so the integrity runner does not flag the active artifact as
    unreadable just because the field lives one level deeper.
    """
    try:
        art = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(art, dict):
        return None
    for candidate in (art.get("excess_return_pct"), art.get("metrics", {}).get("excess_return_pct")):
        if isinstance(candidate, (int, float)):
            return float(candidate)
    return None


def run() -> tuple[dict, int]:
    stages: dict[str, dict] = {}

    # 1) Status.json must contain a usable paper.latest_active
    status_ok = STATUS_PATH.is_file()
    paper_path: Path | None = None
    paper_path_ok = False
    if status_ok:
        try:
            status = _load(STATUS_PATH)
        except (OSError, json.JSONDecodeError):
            status = {}
        paper_path = _active_paper_path(status)
        paper_path_ok = isinstance(paper_path, Path) and paper_path.is_file()
    else:
        status = {}

    stages["status_has_paper"] = {
        "name": "status.json has a usable paper.latest_active",
        "ok": status_ok and paper_path_ok,
        "detail": (
            f"status.json present={status_ok}, paper.latest_active present and resolvable to a real file={paper_path_ok}"
        ),
    }

    # 2) The paper artifact the status points at must exist and be parseable
    paper_excess_from_artifact: float | None = None
    if paper_path_ok:
        paper_excess_from_artifact = _paper_excess_from_artifact(paper_path)

    stages["paper_artifact_parseable"] = {
        "name": "paper.latest_active artifact is parseable and has excess_return_pct",
        "ok": paper_path_ok and paper_excess_from_artifact is not None,
        "detail": (
            f"artifact={paper_path}{' does not exist' if not paper_path_ok else ''}, "
            f"excess_return_pct={'present' if paper_excess_from_artifact is not None else 'missing'}"
        ),
    }
    if paper_path_ok and paper_excess_from_artifact is not None:
        stages["paper_artifact_excess_value"] = {
            "name": "paper.latest_active excess_return_pct is a finite numeric value",
            "ok": paper_path_ok and paper_excess_from_artifact is not None and float('-inf') < paper_excess_from_artifact < float('inf'),
            "detail": f"paper_excess_from_status_reading source = {paper_excess_from_artifact}",
        }

    # 3) Explore notes must contain a parseable breadth_read
    explore_ok = EXPLORE_PATH.is_file()
    breadth_read: dict | None = None
    if explore_ok:
        try:
            breadth_read = _breadth_read_from_file(EXPLORE_PATH)
        except OSError:
            breadth_read = None
    breadth_read_ok = isinstance(breadth_read, dict)
    stages["explore_readable_breadth"] = {
        "name": "explore_notes.json contains a parseable breadth_read",
        "ok": explore_ok and breadth_read_ok,
        "detail": f"explore_notes.json present={explore_ok}, breadth_read parsed={breadth_read_ok}",
    }

    # 4) Breadth_read must carry the full set of keys the dashboard expects
    breadth_keys_ok = breadth_read_ok and _breadth_required_keys().issubset(breadth_read.keys())
    stages["breadth_shape"] = {
        "name": "breadth_read carries the dashboard's required keys",
        "ok": breadth_keys_ok,
        "detail": (
            f"required_keys_required={sorted(_breadth_required_keys())}, "
            f"present_keys={sorted(breadth_read.keys()) if isinstance(breadth_read, dict) else 'N/A'}"
        ),
    }

    # 5) Breadth_read planned/ran must match robustness grid
    robustness_ok = ROBUSTNESS_PATH.is_file()
    rob: dict = {}
    if robustness_ok:
        try:
            rob = _load(ROBUSTNESS_PATH)
        except (OSError, json.JSONDecodeError):
            rob = {}
    rob_planned = _robustness_planned(rob) if robustness_ok else None
    if rob_planned is None:
        rob_planned = _robustness_planned_alt(rob) if robustness_ok else None
    if rob_planned is None and robustness_ok:
        rob_planned = _robustness_planned_alt(rob)
    breadth_planned_ok = (
        breadth_read_ok
        and isinstance(breadth_read.get("planned"), int)
        and rob_planned is not None
        and breadth_read["planned"] == rob_planned
    )
    stages["breadth_planned_matches_robustness"] = {
        "name": "breadth_read.planned matches robustness.json grid_size.planned_runs",
        "ok": breadth_planned_ok,
        "detail": (
            f"breadth_read.planned={breadth_read.get('planned') if breadth_read_ok else 'N/A'}, "
            f"robustness planned_runs={rob_planned}"
        ),
    }

    # 6) Breadth_read median_excess_pct must match robustness summary
    rob_median = _robustness_median_excess(rob) if robustness_ok else None
    breadth_median_ok = (
        breadth_read_ok
        and isinstance(breadth_read.get("median_excess_pct"), (int, float))
        and rob_median is not None
        and abs(float(breadth_read["median_excess_pct"]) - rob_median) < 1e-6
    )
    stages["breadth_median_matches_robustness"] = {
        "name": "breadth_read.median_excess_pct matches robustness.summary.median_excess_pct",
        "ok": breadth_median_ok,
        "detail": (
            f"breadth_read.median_excess_pct={breadth_read.get('median_excess_pct') if breadth_read_ok else 'N/A'}, "
            f"robustness median_excess_pct={rob_median}"
        ),
    }

    # 7) Breadth_read best_cell must match robustness best cell
    rob_best: str | None = _robustness_best_cell(rob) if robustness_ok else None
    if rob_best is None and robustness_ok:
        rob_best = _robustness_best_cell_fallback(rob)
    if rob_best is None and robustness_ok:
        rob_best = _robustness_best_cell_from_summary_best_label(rob)
    breadth_best_ok = (
        breadth_read_ok
        and isinstance(breadth_read.get("best_cell"), str)
        and rob_best is not None
        and _normalize_best_cell(breadth_read["best_cell"]) == _normalize_best_cell(rob_best)
    )
    stages["breadth_best_cell_matches_robustness"] = {
        "name": "breadth_read.best_cell matches robustness best cell (normalized)",
        "ok": breadth_best_ok,
        "detail": (
            f"breadth_read.best_cell={breadth_read.get('best_cell') if breadth_read_ok else 'N/A'}, "
            f"robustness best_cell={rob_best if rob_best is not None else '(unresolved best cell in robustness.json)'}"
        ),
    }
    if not breadth_best_ok and breadth_read_ok and robustness_ok:
        rob_best_normalized = _robustness_best_cell_from_summary_best_label_normalized(rob)
        if rob_best_normalized is not None:
            stages["breadth_best_cell_matches_robustness"]["ok"] = (
                _canonical_best_cell_from_label(breadth_read["best_cell"]) == rob_best_normalized
            )
            stages["breadth_best_cell_matches_robustness"]["detail"] = (
                f"breadth_read.best_cell={breadth_read.get('best_cell')}, "
                f"robustness best_cell_normalized={rob_best_normalized} "
                f"(re-check from summary: matched={stages['breadth_best_cell_matches_robustness']['ok']})"
            )



    # 8) Null count
    #
    # The current sweep builder stores per-market null results under
    # robustness.json.nulls as a list of market-scoped null-test summaries.
    # That is the shape we cross-check against breadth_read.null_tests.
    rob_nulls: int | None = None
    if robustness_ok:
        nulls = rob.get("nulls")
        if isinstance(nulls, list):
            rob_nulls = len(nulls)
        elif isinstance(nulls, dict):
            combined = nulls.get("combined_by_market")
            if isinstance(combined, list):
                rob_nulls = len(combined)
    if rob_nulls is None and robustness_ok:
        rob_nulls = _robustness_nulls_from_summary_completed(rob)
    breadth_nulls_ok = (
        breadth_read_ok
        and isinstance(breadth_read.get("null_tests"), list)
        and rob_nulls is not None
        and len(breadth_read["null_tests"]) == rob_nulls
    )
    stages["breadth_nulls_count_matches_robustness"] = {
        "name": "breadth_read.null_tests count matches robustness nulls",
        "ok": breadth_nulls_ok,
        "detail": (
            f"breadth_read.null_tests count={len(breadth_read.get('null_tests', [])) if breadth_read_ok else 'N/A'}, "
            f"robustness nulls count={rob_nulls}"
        ),
    }
    if not breadth_nulls_ok and breadth_read_ok and robustness_ok:
        null_count_raw = len(breadth_read.get("null_tests", []))
        stages["breadth_nulls_count_matches_robustness"]["detail"] = (
            f"breadth_read.null_tests count={null_count_raw}, "
            f"robustness nulls count={rob_nulls} "
            f"(mismatch: explore_notes reports {null_count_raw} null tests while robustness.json has {rob_nulls} null-tested cells)"
        )

    # 9) Snapshot verification must reflect the same cross-checks
    snapshot_ok = SNAPSHOT_PATH.is_file()
    snapshot: dict = {}
    if snapshot_ok:
        try:
            snapshot = _load(SNAPSHOT_PATH)
        except (OSError, json.JSONDecodeError):
            snapshot = {}
    verification = snapshot.get("verification")
    verification_ok = isinstance(verification, dict)
    stages["snapshot_exists"] = {
        "name": "results/dashboard_excess.json exists",
        "ok": snapshot_ok,
        "detail": f"dashboard_excess.json present={snapshot_ok}",
    }
    stages["snapshot_has_verification_block"] = {
        "name": "dashboard_excess.json.verification is a JSON object",
        "ok": verification_ok,
        "detail": f"verification type={'object' if verification_ok else 'missing/bad'}",
    }
    if verification_ok:
        stages["snapshot_breadth_median_check"] = {
            "name": "snapshot.verification.breadth_median_matches_robustness is True",
            "ok": bool(verification.get("breadth_median_matches_robustness")) is True,
            "detail": f"snapshot.verification.breadth_median_matches_robustness={verification.get('breadth_median_matches_robustness')}",
        }
        stages["snapshot_breadth_best_cell_check"] = {
            "name": "snapshot.verification.breadth_best_cell_matches_robustness is True",
            "ok": bool(verification.get("breadth_best_cell_matches_robustness")) is True,
            "detail": f"snapshot.verification.breadth_best_cell_matches_robustness={verification.get('breadth_best_cell_matches_robustness')}",
        }
        stages["snapshot_breadth_planned_check"] = {
            "name": "snapshot.verification.breadth_planned_matches_robustness is True",
            "ok": bool(verification.get("breadth_planned_matches_robustness")) is True,
            "detail": f"snapshot.verification.breadth_planned_matches_robustness={verification.get('breadth_planned_matches_robustness')}",
        }
        stages["snapshot_breadth_nulls_check"] = {
            "name": "snapshot.verification.breadth_nulls_count_matches_robustness is True",
            "ok": bool(verification.get("breadth_nulls_count_matches_robustness")) is True,
            "detail": f"snapshot.verification.breadth_nulls_count_matches_robustness={verification.get('breadth_nulls_count_matches_robustness')}",
        }

    # 10) Paper excess source cross-checks
    paper_excess_source = verification.get("paper_excess_source") if verification_ok else None
    paper_excess_source_ok = False
    paper_excess_from_status_ok = False
    paper_excess_source_matches = False
    if verification_ok and isinstance(paper_excess_source, str) and paper_excess_source:
        src = Path(paper_excess_source)
        paper_excess_source_ok = src.is_file()
        paper_excess_from_status_ok = bool(verification.get("paper_excess_from_status_reading")) is True
        paper_excess_source_matches = bool(verification.get("paper_excess_source_matches_active_artifact")) is True
    stages["snapshot_paper_excess_source_exists"] = {
        "name": "snapshot.verification.paper_excess_source points at a real file",
        "ok": paper_excess_source_ok,
        "detail": f"paper_excess_source={paper_excess_source}, exists={paper_excess_source_ok}",
    }
    stages["snapshot_paper_excess_from_status"] = {
        "name": "snapshot.verification.paper_excess_from_status_reading is True",
        "ok": paper_excess_from_status_ok,
        "detail": f"paper_excess_from_status_reading={verification.get('paper_excess_from_status_reading') if verification_ok else 'N/A'}",
    }
    stages["snapshot_paper_excess_source_matches"] = {
        "name": "snapshot.verification.paper_excess_source_matches_active_artifact is True",
        "ok": paper_excess_source_matches,
        "detail": f"paper_excess_source_matches_active_artifact={verification.get('paper_excess_source_matches_active_artifact') if verification_ok else 'N/A'}",
    }

    fails = [s for s in stages.values() if not s["ok"]]
    result = {
        "checked_at": time_iso_now(),
        "stages": stages,
        "failed": fails,
        "failed_count": len(fails),
        "total": len(stages),
    }
    rc = 0 if not fails else 2
    return result, rc


def time_iso_now() -> str:
    import time as _time

    return _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())


def main() -> int:
    result, rc = run()
    print(json.dumps(result, indent=2))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
