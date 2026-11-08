#!/usr/bin/env python
"""Conservative robustness artifact pruner.

Keeps the real latest result and last_failure.json.
Removes 0-trade and stale assets from the last sweep.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROBUSTNESS = Path(__file__).resolve().parent.parent / "results" / "robustness.json"


def keep(path: Path) -> bool:
    if path.name == "BTCUSDT_1d_ema-atr-trend_20261016T100000Z.json":
        return True
    if path.name == "last_failure.json":
        return True
    if path.name == "decision.json":
        return True
    return False


def main(argv: list[str] | None = None) -> int:
    print("robustness-prune")
    print("=" * 70)
    paths = sorted(ROBUSTNESS.glob("*.json"))
    if not paths:
        print("no robustness artifacts found")
        return 2

    kept = []
    removed = []
    for path in paths:
        if keep(path):
            kept.append(path.name)
        else:
            removed.append(path.name)
            path.unlink()

    print()
    print("kept:")
    for name in kept:
        print("  ", name)
    print()
    print("removed:")
    for name in removed:
        print("  ", name)
    print()
    print(f"remaining robustness artifacts: {len(list(ROBUSTNESS.glob('*.json')))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
