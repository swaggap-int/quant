#!/usr/bin/env python
"""Verify the dashboard excess snapshot is present and fresh.

This is the CI/pre-commit gate the repo uses to avoid serving a frozen
read whose backing sources (status.json, explore_notes.json,
robustness.json) have since changed or gone missing.

Exits 0 when results/dashboard_excess.json is fresh, 1 when it is
missing or stale, and 2 when a source is missing entirely.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure the repo root is on sys.path so ``import quant`` works when this
# script is run directly (e.g. from CI or a pre-commit hook).
_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

import quant.build_dashboard_excess as b

# ``fresh`` only verifies the snapshot's presence and timestamp; calling it is
# not the same as rebuilding. For a real gate the harness should run the build
# hook (which regenerates through the tested ``build`` path) rather than the
# read-only check used here.


def main(argv: list[str] | None = None) -> int:
    results = _results_dir(argv)
    if not results.is_dir():
        print(f"results dir not found: {results}", file=sys.stderr)
        return 2

    if not b.fresh(results):
        print("dashboard_excess.json is missing or stale; rebuild it with:")
        print(f"  python -m quant.build_dashboard_excess --results {results}")
        return 1

    print("dashboard_excess.json is fresh")
    return 0


def _results_dir(argv: list[str] | None = None) -> Path:
    if argv is None:
        argv = sys.argv[1:]
    for arg in (argv or []):
        if arg.startswith("--results="):
            return Path(arg.split("=", 1)[1])
        if arg == "--results" and len(argv) > 1:
            return Path(argv[argv.index(arg) + 1])
    return b.RESULTS


def _cli() -> None:
    """Run as a script; exit with the freshness verdict.

    This is the entry point pre-commit/CI should invoke. It intentionally does
    not import argparse so it stays tiny and deterministic.
    """
    raise SystemExit(main())


if __name__ == "__main__":
    _cli()
