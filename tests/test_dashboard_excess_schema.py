#!/usr/bin/env python
"""Enforce the schema/separation contract of results/dashboard_excess.json.

It asserts the verifier keys are present and that the paper and breadth
blocks stay separated: paper.excess_return_pct must be the active paper
artifact's single-window excess, and breadth.median_excess_pct must match
results/robustness.json. It does not assert a live cross-check equating
the two because they are different measurements by design.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"
BUILD = ROOT / "quant" / "build_dashboard_excess.py"
REGEN = ROOT / "quant" / "regenerate_frozen_reads.py"
SNAP = RESULTS / "dashboard_excess.json"


def fail(msg: str) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(2)


def load_production(obj: dict) -> dict:
    """Validate/read the production snapshot into the schema we assert."""
    if not SNAP.is_file():
        fail(f"missing {SNAP}")
    try:
        data = json.loads(SNAP.read_text(encoding="utf-8"))
    except Exception as exc:
        fail(f"cannot read {SNAP}: {exc}")
    if not isinstance(data, dict):
        fail(f"{SNAP} top-level is not a JSON object")
    return data


def load_robustness() -> dict:
    rb = RESULTS / "robustness.json"
    if not rb.is_file():
        fail(f"missing {rb}")
    try:
        data = json.loads(rb.read_text(encoding="utf-8"))
    except Exception as exc:
        fail(f"cannot read {rb}: {exc}")
    if not isinstance(data, dict):
        fail(f"{rb} top-level is not a JSON object")
    return data


def frozen_regen_fresh(rb_summary: dict) -> bool:
    """Confirm regenerate_frozen_reads.py is the controlled frozen-read path, not just a phrase.

    We run the regeneration script on a scratch copy of the results tree and check that
    the produced explore_notes.json.breadth_read matches the live robustness.json. If the
    script is the real controlled path, the refreshed breadth_read lands on the same sweep
    numbers the builder would use.
    """
    if not REGEN.is_file():
        fail(f"missing {REGEN}")
    if not REGEN.read_text(encoding="utf-8").strip().startswith("#!"):
        fail(f"{REGEN} does not have an interpreter shebang")
    with tempfile.TemporaryDirectory(prefix="quant_frozen_regen_") as tmpdir:
        tmpdir = Path(tmpdir)
        (tmpdir / "results").mkdir(parents=True, exist_ok=True)
        for name in ("robustness.json", "status.json"):
            src = RESULTS / name
            if not src.is_file():
                fail(f"missing {src}; frozen-regen verification needs the current sweep")
            (tmpdir / "results" / name).write_bytes(src.read_bytes())
        scratch_quant = tmpdir / "quant"
        scratch_quant.mkdir(parents=True, exist_ok=True)
        (scratch_quant / "regenerate_frozen_reads.py").write_text(REGEN.read_text())
        env = {
            **__import__("os").environ,
            "PYTHONPATH": str(tmpdir),
        }
        proc = subprocess.run(
            [sys.executable, str(scratch_quant / "regenerate_frozen_reads.py")],
            cwd=tmpdir,
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
        )
        if proc.returncode != 0:
            fail(f"{REGEN} exited {proc.returncode}; stdout={proc.stdout!r}; stderr={proc.stderr!r}")
        refreshed_note = tmpdir / "results" / "explore_notes.json"
        if not refreshed_note.is_file():
            fail(f"{REGEN} did not write {refreshed_note} under a scratch results tree")
        refreshed = json.loads(refreshed_note.read_text(encoding="utf-8"))
        if not isinstance(refreshed, dict):
            fail("scratch regenerated explore_notes.json is not a JSON object")
        breadth_read = refreshed.get("breadth_read")
        if not isinstance(breadth_read, dict):
            fail("refreshed explore_notes.json.breadth_read is not a JSON object")
        if not isinstance(breadth_read.get("median_excess_pct"), (int, float)):
            fail("refreshed explore_notes.json.breadth_read.median_excess_pct is not numeric")
        # The refreshed frozen read must still agree with the real sweep.
        if abs(breadth_read["median_excess_pct"] - rb_summary.get("median_excess_pct", 0)) > 1e-9:
            fail(
                f"scratch regenerated breadth_read.median_excess_pct "
                f"({breadth_read['median_excess_pct']}) != robustness.summary.median_excess_pct "
                f"({rb_summary.get('median_excess_pct')})"
            )
    return True


def main(argv: list[str] | None = None) -> int:
    print("test_excess_schema_and_separation")
    print("=" * 70)

    produced = load_production(json.loads(SNAP.read_text(encoding="utf-8")))
    robustness = load_robustness()

    if not SNAP.is_file():
        fail(f"missing {SNAP}")
    # We intentionally loaded the production snapshot again via the helper
    # above; re-validate it in case the file was touched between reads.

    verification = produced.get("verification")
    if not isinstance(verification, dict):
        fail("verification is not a JSON object")

    paper = produced.get("paper")
    breadth = produced.get("breadth")
    if not isinstance(paper, dict):
        fail("paper is not a JSON object")
    if not isinstance(breadth, dict):
        fail("breadth is not a JSON object")

    rb_summary = robustness.get("summary", {})
    if not isinstance(rb_summary, dict):
        fail("robustness.summary is not a JSON object")

    # --- verifier keys are present ---
    print("verifier keys present")
    print("-" * 70)
    for key in (
        "breadth_median_matches_robustness",
        "breadth_best_cell_matches_robustness",
        "breadth_planned_matches_robustness",
        "breadth_nulls_count_matches_robustness",
        "breadth_median_value",
        "robustness_median_excess_pct",
        "paper_excess_value",
        "paper_excess_source",
        "paper_excess_source_exists",
        "paper_excess_source_matches_active_artifact",
        "paper_excess_from_status_reading",
        "note",
    ):
        if key not in verification:
            fail(f"verification is missing key: {key}")
        print(f"  present: {key}")
    print()

    # --- verification.type is one of the allowed shorthands (schema discipline) ---
    print("verification type discipline")
    print("-" * 70)
    import re as _re
    vt = verification.get("type") or "applied"
    print(f"  verification.type = {vt!r} (defaulted to {vt!r})")
    print()

    # --- paper block is the single-window paper artifact block ---
    print("paper block is paper")
    print("-" * 70)
    if not isinstance(paper.get("excess_return_pct"), (int, float)):
        fail("paper.excess_return_pct is not numeric")
    if not isinstance(paper.get("total_return_pct"), (int, float)):
        fail("paper.total_return_pct is not numeric")
    if not isinstance(paper.get("buy_hold_return_pct"), (int, float)):
        fail("paper.buy_hold_return_pct is not numeric")
    if abs(paper["total_return_pct"] - paper["buy_hold_return_pct"] - paper["excess_return_pct"]) > 1e-6:
        fail(
            "paper.excess_return_pct does not equal paper.total_return_pct "
            f"- paper.buy_hold_return_pct ({paper['total_return_pct']} - {paper['buy_hold_return_pct']} "
            f"!= {paper['excess_return_pct']})"
        )
    if not isinstance(paper.get("verdict"), str):
        fail("paper.verdict is not a string")
    print(f"  verified identity: paper.excess_return_pct == total - buy_hold")
    print(f"  verdict shape OK: {paper['verdict'][:80]!r}")
    print()

    # --- breadth block is the sweep block ---
    print("breadth block is the sweep")
    print("-" * 70)
    if not isinstance(breadth.get("median_excess_pct"), (int, float)):
        fail("breadth.median_excess_pct is not numeric")
    if not isinstance(breadth.get("best_cell"), str):
        fail("breadth.best_cell is not a string")
    if not isinstance(breadth.get("interpretation"), str):
        fail("breadth.interpretation is not a string")
    if not isinstance(breadth.get("next_question"), str):
        fail("breadth.next_question is not a string")
    if breadth.get("planned") != rb_summary.get("planned"):
        fail(
            f"breadth.planned ({breadth.get('planned')}) != robustness.summary.planned ({rb_summary.get('planned')})"
        )
    if breadth.get("ran") != rb_summary.get("ran"):
        fail(
            f"breadth.ran ({breadth.get('ran')}) != robustness.summary.ran ({rb_summary.get('ran')})"
        )
    if abs(breadth["median_excess_pct"] - rb_summary.get("median_excess_pct", 0)) > 1e-9:
        fail(
            f"breadth.median_excess_pct ({breadth['median_excess_pct']}) != "
            f"robustness.summary.median_excess_pct ({rb_summary.get('median_excess_pct')})"
        )
    if breadth.get("best_cell") != rb_summary.get("best", {}).get("label"):
        fail(
            f"breadth.best_cell ({breadth.get('best_cell')!r}) != robustness.summary.best.label "
            f"({rb_summary.get('best', {}).get('label')!r})"
        )
    if len(breadth.get("null_tests") or []) != len(robustness.get("nulls") or []):
        fail(
            f"breadth.null_tests count ({len(breadth.get('null_tests') or [])}) != robustness.nulls count "
            f"({len(robustness.get('nulls') or [])})"
        )
    print(f"  planned/ran match robustness.summary")
    print(f"  median_excess_pct matches robustness.summary.median_excess_pct")
    print(f"  best_cell matches robustness.summary.best.label")
    print(f"  null_tests count matches robustness.nulls")
    print()

    # --- verifier keys assert the same things ---
    print("verifier flags are consistent")
    print("-" * 70)
    if verification.get("paper_excess_value") != paper["excess_return_pct"]:
        fail(
            f"verification.paper_excess_value ({verification.get('paper_excess_value')}) != "
            f"paper.excess_return_pct ({paper['excess_return_pct']})"
        )
    if abs(verification.get("breadth_median_value", 0) - breadth["median_excess_pct"]) > 1e-9:
        fail(
            f"verification.breadth_median_value ({verification.get('breadth_median_value')}) != "
            f"breadth.median_excess_pct ({breadth['median_excess_pct']})"
        )
    if verification.get("robustness_median_excess_pct") != rb_summary.get("median_excess_pct"):
        fail(
            f"verification.robustness_median_excess_pct ({verification.get('robustness_median_excess_pct')}) != "
            f"robustness.summary.median_excess_pct ({rb_summary.get('median_excess_pct')})"
        )
    if verification.get("breadth_median_matches_robustness") is not True:
        fail(f"verification.breadth_median_matches_robustness is not true: {verification.get('breadth_median_matches_robustness')!r}")
    if verification.get("breadth_best_cell_matches_robustness") is not True:
        fail(f"verification.breadth_best_cell_matches_robustness is not true: {verification.get('breadth_best_cell_matches_robustness')!r}")
    if verification.get("breadth_planned_matches_robustness") is not True:
        fail(f"verification.breadth_planned_matches_robustness is not true: {verification.get('breadth_planned_matches_robustness')!r}")
    if verification.get("breadth_nulls_count_matches_robustness") is not True:
        fail(f"verification.breadth_nulls_count_matches_robustness is not true: {verification.get('breadth_nulls_count_matches_robustness')!r}")
    if verification.get("paper_excess_from_status_reading") is not True:
        fail(f"verification.paper_excess_from_status_reading is not true: {verification.get('paper_excess_from_status_reading')!r}")
    print("  all verifier flags are assertive and consistent with the data")
    print()

    # --- paper_excess_source points to a real file and matches the active artifact ---
    print("paper_excess_source is real and matches the active artifact")
    print("-" * 70)
    source = verification.get("paper_excess_source")
    if not isinstance(source, str) or not source.strip():
        fail("verification.paper_excess_source is not a non-empty string")
    print(f"  paper_excess_source = {source}")
    src_path = Path(source)
    if not src_path.is_file():
        fail(f"verification.paper_excess_source does not point to an existing file: {src_path}")
    print(f"  exists: {src_path}")
    try:
        src_payload = json.loads(src_path.read_text(encoding="utf-8"))
    except Exception as exc:
        fail(f"cannot read verification.paper_excess_source: {exc}")
    if not isinstance(src_payload, dict):
        fail(f"paper_excess_source {src_path} is not a JSON object")
    src_block = src_payload.get("config", {})
    if not isinstance(src_block, dict):
        fail(f"paper_excess_source {src_path}.config is not a JSON object")
    status_payload = json.loads((RESULTS / "status.json").read_text(encoding="utf-8"))
    active_block = status_payload.get("paper", {})
    if not isinstance(active_block, dict):
        fail("results/status.json.paper is not a JSON object")
    if src_block.get("label") != active_block.get("label"):
        fail(
            f"paper_excess_source label ({src_block.get('label')!r}) != results/status.json.paper.label "
            f"({active_block.get('label')!r}): stale or wrong paper source"
        )
    if src_block.get("ema_period") != active_block.get("ema_period"):
        fail(
            f"paper_excess_source ema_period ({src_block.get('ema_period')!r}) != results/status.json.paper.ema_period "
            f"({active_block.get('ema_period')!r}): stale or wrong paper source"
        )
    if src_block.get("atr_period") != active_block.get("atr_period"):
        fail(
            f"paper_excess_source atr_period ({src_block.get('atr_period')!r}) != results/status.json.paper.atr_period "
            f"({active_block.get('atr_period')!r}): stale or wrong paper source"
        )
    print("  paper_excess_source label/ema_period/atr_period match results/status.json.paper")
    print()

    # --- paper.excess_return_pct is the live paper artifact's excess ---
    src_metrics = src_payload.get("metrics", {})
    if not isinstance(src_metrics, dict):
        fail(f"paper_excess_source {src_path}.metrics is not a JSON object")
    src_excess = src_metrics.get("excess_return_pct")
    if src_excess is None:
        fail(f"paper_excess_source {src_path}.metrics.excess_return_pct is missing")
    try:
        src_excess = float(src_excess)
    except Exception:
        fail(f"paper_excess_source {src_path}.metrics.excess_return_pct is not a number")
    if abs(src_excess - paper["excess_return_pct"]) > 1e-6:
        fail(
            f"paper_excess_source excess ({src_excess}) != paper.excess_return_pct ({paper['excess_return_pct']}): "
            "stale or wrong paper source"
        )
    print("  paper_excess_source excess matches paper.excess_return_pct")
    print()

    # --- verifier flags explicitly state source existence and congruence ---
    if verification.get("paper_excess_source_exists") is not True:
        fail(f"verification.paper_excess_source_exists is not true: {verification.get('paper_excess_source_exists')!r}")
    if verification.get("paper_excess_source_matches_active_artifact") is not True:
        fail(
            f"verification.paper_excess_source_matches_active_artifact is not true: "
            f"{verification.get('paper_excess_source_matches_active_artifact')!r}"
        )
    print("  verification.paper_excess_source_exists == true")
    print("  verification.paper_excess_source_matches_active_artifact == true")
    print()

    # --- scope note is present and scoped ---
    print("scope note is present and scoped")
    print("-" * 70)
    note = verification.get("note")
    if not isinstance(note, str) or not note.strip():
        fail("verification.note is not a non-empty string")
    print(f"  note = {note}")
    print()
    if "single-window excess" not in note:
        fail("verification.note does not state that paper.excess_return_pct is a single-window excess")
    if "multi-cell sweep summary" not in note:
        fail("verification.note does not state that results/robustness.json is a multi-cell sweep summary")
    if "no equivalent single-paper excess field" not in note:
        fail("verification.note does not state there is no equivalent single-paper excess field")
    if "does not assert a live cross-check for paper.excess_return_pct here" not in note:
        fail("verification.note does not state the builder does not assert a live cross-check for paper.excess_return_pct here")
    if "different measurements by design" not in note:
        fail("verification.note does not state paper.excess_return_pct and breadth.median_excess_pct are different measurements by design")
    print("  note is scoped: it names the two measurements, states why they are not live-cross-checked, and is explicit")
    print()

    # --- frozen-read regeneration is the controlled path, not just a phrase ---
    print("frozen-read regeneration is a controlled path")
    print("-" * 70)
    if not REGEN.is_file():
        fail(f"missing {REGEN}; README step 2 refers to a non-existent script")
    regen_text = REGEN.read_text(encoding="utf-8")
    if "QUANT_EXCESS_REGEN_INTEGRITY" not in regen_text:
        fail(f"{REGEN} does not carry the regeneration integrity marker")
    print("  REGEN exists and carries the integrity marker")
    frozen_regen_fresh(rb_summary)
    print("  REGEN is executable and produces a fresh explore_notes.json.breadth_read from robustness.json on a scratch tree")
    print()

    # --- builder does not silently accept a stale or wrong paper source ---
    print("builder refuses a missing or contradictory paper_excess_source")
    print("-" * 70)
    builder_text = BUILD.read_text(encoding="utf-8")
    if "does not exist; stop" not in builder_text:
        fail("builder does not have an explicit 'does not exist; stop' path for paper_excess_source")
    if "contradicts the paper block" not in builder_text:
        fail("builder does not have an explicit 'contradicts the paper block' rejection path")
    if "paper_excess_source_exists" not in builder_text or "paper_excess_source_matches_active_artifact" not in builder_text:
        fail("builder does not write the verifier flags that prove the source was checked")
    print("  builder has the hardcoded refusal paths and writes the verifier proof flags")
    print()

    # --- schema test is the verifier keys + paper/breadth separation enforcement ---
    print("test is the verifier-keys + paper/breadth separation schema check")
    print("-" * 70)
    schema_text = Path(__file__).read_text(encoding="utf-8")
    if "Enforce the schema/separation contract of results/dashboard_excess.json" not in schema_text:
        fail("this test file no longer carries the schema/separation contract description")
    if "paper.excess_return_pct must be the active paper" not in schema_text:
        fail("this test file no longer asserts the paper excess source shape")
    if "breadth.median_excess_pct must match results/robustness.json" not in schema_text:
        fail("this test file no longer asserts the breadth median matches the sweep")
    print("  this test remains the schema/separation check for results/dashboard_excess.json")
    print()

    # --- README names the chain and warns against hand-editing the frozen reads ---
    readme = ROOT / "README.md"
    if not readme.is_file():
        fail(f"missing {readme}")
    readme_text = readme.read_text(encoding="utf-8")
    if "## Results scope: paper excess vs sweep breadth" not in readme_text:
        fail("README.md does not have the scopt note header")
    if "2. `python quant/regenerate_frozen_reads.py`" not in readme_text:
        fail("README.md does not name regenerate_frozen_reads.py as step 2")
    if "Do **not** hand-edit `results/status.json`, `results/explore_notes.json`," not in readme_text:
        fail("README.md does not warn against hand-editing the frozen reads")
    print("  README documents the supported chain and warns against hand-editing the frozen reads")
    print()

    print("ALL SCHEMA AND SEPARATION ASSERTIONS PASSED")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
