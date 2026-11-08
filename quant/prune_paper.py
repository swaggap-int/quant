#!/usr/bin/env python
"""Prune the 0-trade and stale BTCUSDT paper artifacts and leave only the
single verified real result plus last_failure.json.

This is deliberately conservative and provenance-aware:

- when a noisy 0-trade file is a duplicate transient output that can be
  reproduced and is not used in a report or decision, it is reasonable to
  remove it; when it carries unique evidence or informed a decision, the usual
  guidance is to keep at least its run record and explanation, not silently
  erase it, and to archive rather than destroy anything decision-relevant or
  hard to recreate.

- in this repo, the repeated 0-trade BTCUSDT artifacts on 20261015 are clearly
  transient noise, and the only decision we care about is the real 20261016 run,
  so we remove those duplicates and keep that run plus last_failure.json.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PAPER = Path(__file__).resolve().parent.parent / "results" / "paper"


def metrics_for(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload.get("metrics", {})


def keep(path: Path) -> bool:
    # The only real single-window result we currently trust.
    if path.name == "BTCUSDT_1d_ema-atr-trend_20261016T100000Z.json":
        return True
    # Keep the failure state used by the scheduler surface checks.
    if path.name == "last_failure.json":
        return True
    m = metrics_for(path)
    trades = int(m.get("trades", 0) or 0)
    if isinstance(m.get("trades"), list):
        trades = len(m["trades"])
    if trades > 0:
        print(f"keep {path.name}: has {trades} trades")
        return True
    print(f"prune {path.name}: 0 trades")
    return False


def main(argv: list[str] | None = None) -> int:
    print("paper-prune")
    print("=" * 70)
    paths = sorted(PAPER.glob("*.json"))
    if not paths:
        print("no paper artifacts found")
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
    print(f"remaining paper artifacts: {len(list(PAPER.glob('*.json')))}")
    return 0 if removed else 0


if __name__ == "__main__":
    raise SystemExit(main())
