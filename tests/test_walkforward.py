"""Tests for the rolling walk-forward study.

Covers the three pieces that can silently lie:
- fold boundaries: consecutive, non-overlapping, and covering the judged history
- the reading: beat counts, risk-adjusted counts, median regret, modal pick
- the verdict: plain language that distinguishes "no edge" from "lower risk"

Plus one end-to-end roll on synthetic bars, so the study cannot break between
its parts without a test noticing.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from quant.config import BacktestConfig
from run_walkforward import (
    KEEP_STUDIES,
    build_parser,
    candidate_rows,
    families_for,
    main,
    fold_windows,
    prune_studies,
    run_symbol,
    study_stamp,
    summarize,
    verdict_for,
    _earns_more_per_drawdown,
)

from tests.helpers import candles_from


def _row(excess: float, ret: float, regret: float, pick: str, risk: bool | None) -> dict:
    return {
        "test_excess": excess,
        "test_ret": ret,
        "test_trades": 10,
        "regret": regret,
        "chosen_params": pick,
        "risk_adjusted_beat": risk,
    }


def test_fold_windows_are_consecutive_and_cover_the_judged_history() -> None:
    windows = fold_windows(1_000, 5)
    assert len(windows) == 4
    assert windows[0][0] == 0
    assert windows[0][1] == windows[0][2]  # train ends where test starts
    assert windows[-1][3] == 1_000
    for previous, current in zip(windows, windows[1:]):
        # The chunk just judged becomes the next chunk's training window.
        assert current[0] == previous[2]
        assert current[1] == previous[3]
    assert sum(hi - lo for _, _, lo, hi in windows) == 800


def test_fold_windows_put_the_remainder_in_the_last_chunk() -> None:
    windows = fold_windows(1_001, 5)
    sizes = {hi - lo for _, _, lo, hi in windows}
    assert windows[-1][3] == 1_001
    assert max(sizes) - min(sizes) <= 1


def test_fold_windows_reject_impossible_splits() -> None:
    for n, folds in ((5, 5), (10, 1), (10, 0)):
        try:
            fold_windows(n, folds)
        except ValueError:
            continue
        raise AssertionError(f"fold_windows({n}, {folds}) should raise")


def test_summary_counts_beats_regret_and_the_modal_pick() -> None:
    rows = [
        _row(10.0, 20.0, -2.0, "a", True),
        _row(-5.0, -10.0, -4.0, "a", False),
        _row(-20.0, -30.0, -3.0, "b", None),
    ]
    summary = summarize(rows, "ema", "BTC/USDT")

    assert summary["windows"] == 3
    assert summary["beat_buy_hold"] == 1
    assert summary["beat_buy_hold_pct"] == 33.3
    assert summary["median_test_excess"] == -5.0
    assert summary["median_test_ret"] == -10.0
    assert summary["median_regret"] == -3.0
    assert summary["modal_pick"] == "a"
    assert summary["modal_pick_pct"] == 66.7
    # A None risk reading is excluded rather than counted as a loss.
    assert summary["risk_adjusted_beat"] == 1
    assert summary["risk_adjusted_windows"] == 2
    assert summary["risk_adjusted_pct"] == 50.0
    assert summary["regret_note"] is None
    assert "Did not hold up" in summary["verdict"]


def test_summary_flags_noisy_selection_when_regret_is_large() -> None:
    rows = [
        _row(10.0, 20.0, -2.0, "a", True),
        _row(10.0, 20.0, -40.0, "a", True),
        _row(10.0, 20.0, -30.0, "a", True),
    ]
    summary = summarize(rows, "donchian", "ETH/USDT")
    assert summary["regret_note"] is not None
    assert "selection is mostly noise" in summary["regret_note"]


def test_verdict_reads_like_plain_language_not_a_score() -> None:
    assert "Too few unseen chunks" in verdict_for(1, 2)
    assert "Held up in 3 of 4" in verdict_for(3, 4)
    assert "Mixed" in verdict_for(2, 4)
    assert "Did not hold up" in verdict_for(0, 4)
    # A family that rarely beats the benchmark but usually earns more per unit
    # of drawdown is described as lower risk, not as a failure.
    assert "lower-risk path" in verdict_for(1, 4, risk_beat=4, risk_windows=4)


def test_bollinger_is_the_default_family_and_has_three_candidates() -> None:
    args = build_parser().parse_args([])
    assert args.family == "bollinger"

    candidates = candidate_rows("bollinger")
    assert len(candidates) == 3
    assert [label for label, _ in candidates] == ["bb20 k2.0", "bb30 k2.0", "bb50 k2.5"]
    assert all(build().__class__.__name__ == "BollingerReversion" for _, build in candidates)


def test_all_expands_to_the_three_families() -> None:
    args = build_parser().parse_args(["--family", "all"])
    assert args.family == "all"
    assert families_for("all") == ["bollinger", "ema", "donchian"]
    assert families_for("both") == ["ema", "donchian"]
    assert families_for("donchian") == ["donchian"]


def test_bollinger_family_runs_end_to_end_on_synthetic_bars() -> None:
    rows = []
    price = 100.0
    for i in range(1_500):
        drift = 0.35 if (i // 120) % 2 == 0 else -0.25
        price = max(5.0, price + drift)
        rows.append((price, price + 1.5, price - 1.5, price + (0.2 if i % 2 else -0.2), 1.0))

    out = run_symbol(
        BacktestConfig(symbol="TEST/USDT", timeframe="1d"),
        candles_from(rows),
        folds=3,
        families=["bollinger"],
    )

    family = out["families"]["bollinger"]
    assert family["windows"] == 2
    assert len(family["rows"]) == 2
    for row in family["rows"]:
        assert row["train_start"] < row["test_start"]
        assert row["best_test_ret"] >= row["test_ret"]
        assert row["regret"] <= 0.0
    assert family["verdict"]


def test_risk_adjustment_ignores_meaningless_drawdowns() -> None:
    assert _earns_more_per_drawdown(20.0, 5.0, 10.0, 10.0) is True
    assert _earns_more_per_drawdown(5.0, 10.0, 10.0, 10.0) is False
    assert _earns_more_per_drawdown(20.0, 0.4, 10.0, 10.0) is None
    assert _earns_more_per_drawdown(20.0, 5.0, 10.0, 0.5) is None


def test_rolling_study_runs_end_to_end_on_synthetic_bars() -> None:
    rows = []
    price = 100.0
    for i in range(1_200):
        drift = 0.4 if (i // 150) % 2 == 0 else -0.25
        price = max(5.0, price + drift)
        rows.append((price, price + 1.5, price - 1.5, price + (0.2 if i % 2 else -0.2), 1.0))

    candles = candles_from(rows)
    cfg = BacktestConfig(symbol="TEST/USDT", timeframe="1d")
    out = run_symbol(cfg, candles, folds=3, families=["donchian"])

    assert out["chunks"] == 3
    assert out["windows"] == 2
    family = out["families"]["donchian"]
    assert family["windows"] == 2
    assert len(family["rows"]) == 2
    for row in family["rows"]:
        assert row["train_start"] < row["test_start"]
        assert row["best_test_ret"] >= row["test_ret"]
        assert row["regret"] <= 0.0
    assert family["verdict"]


def _study_file(directory: Path, name: str, study: str = "rolling_walkforward") -> Path:
    path = directory / name
    path.write_text(json.dumps({"study": study}), encoding="utf-8")
    return path


def _dated(directory: Path, day: int) -> Path:
    return _study_file(directory, f"walkforward_rolling_4h_202610{day:02d}T120000Z.json")


def test_study_stamp_reads_only_its_own_dated_names(tmp_path) -> None:
    assert study_stamp(tmp_path / "walkforward_rolling_4h_20261001T120000Z.json")
    assert study_stamp(tmp_path / "walkforward_rolling_1d.json") is None
    assert study_stamp(tmp_path / "walkforward_rolling_4h_20261399T120000Z.json") is None
    assert study_stamp(tmp_path / "run.json") is None


def test_prune_keeps_the_newest_dated_studies(tmp_path: Path) -> None:
    kept = [_dated(tmp_path, day) for day in (3, 4)]
    old = [_dated(tmp_path, day) for day in (1, 2)]
    base = _study_file(tmp_path, "walkforward_rolling_4h.json")

    removed = prune_studies(tmp_path, keep=2)

    assert sorted(p.name for p in removed) == sorted(p.name for p in old)
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        p.name for p in kept + [base]
    )


def test_prune_orders_by_filename_not_file_time(tmp_path: Path) -> None:
    older = _dated(tmp_path, 1)
    newer = _dated(tmp_path, 2)
    # A restore or copy can leave the older study with a newer mtime; the date in
    # the name is the only ordering the archive should trust.
    os.utime(older, (2_000_000_000, 2_000_000_000))
    os.utime(newer, (1_000_000_000, 1_000_000_000))

    removed = prune_studies(tmp_path, keep=1)

    assert [p.name for p in removed] == [older.name]
    assert newer.exists()


def test_prune_leaves_files_it_cannot_own(tmp_path: Path) -> None:
    broken = tmp_path / "walkforward_rolling_4h_20250101T120000Z.json"
    broken.write_text("not json", encoding="utf-8")
    untouchable = [
        _study_file(tmp_path, "walkforward_rolling_4h.json"),
        _study_file(tmp_path, "walkforward_rolling_4h_20250102T120000Z.json", study="other"),
        _study_file(tmp_path, "walkforward_rolling_4h_20261399T120000Z.json"),
        _study_file(tmp_path, "run.json"),
        broken,
    ]
    dated = [_dated(tmp_path, day) for day in (1, 2, 3)]

    removed = prune_studies(tmp_path, keep=1)

    assert sorted(p.name for p in removed) == sorted([dated[0].name, dated[1].name])
    assert all(p.exists() for p in untouchable + [dated[2]])


def test_prune_keeps_everything_under_the_limit_or_when_disabled(tmp_path: Path) -> None:
    dated = [_dated(tmp_path, day) for day in (1, 2)]

    assert prune_studies(tmp_path, keep=10) == []
    assert len(dated) == 2 and all(p.exists() for p in dated)

    assert prune_studies(tmp_path, keep=0) == []
    assert all(p.exists() for p in dated)


def test_the_cli_keeps_a_handful_by_default() -> None:
    args = build_parser().parse_args([])
    assert args.keep_studies == KEEP_STUDIES == 10


def test_dry_run_cli_lists_exact_candidates_without_running_or_deleting(tmp_path, capsys) -> None:
    older = _dated(tmp_path, 1)
    newer = _dated(tmp_path, 2)
    undated = _study_file(tmp_path, "walkforward_rolling_4h.json")
    out = tmp_path / "next.json"
    assert main(["--out", str(out), "--keep-studies", "1", "--dry-run-prune"]) == 0
    output = capsys.readouterr().out
    assert older.name in output
    assert newer.name not in output
    assert undated.name not in output
    assert "Would prune 1" in output
    assert all(path.exists() for path in (older, newer, undated))
    assert not out.exists()


def test_dry_run_has_nothing_to_remove(tmp_path, capsys) -> None:
    older = _dated(tmp_path, 1)
    assert main(["--out", str(tmp_path / "next.json"), "--dry-run-prune"]) == 0
    assert "No study files would be pruned" in capsys.readouterr().out
    assert older.exists()


def test_normal_prune_prints_names_before_removing(tmp_path, capsys) -> None:
    older = _dated(tmp_path, 1)
    newer = _dated(tmp_path, 2)
    assert prune_studies(tmp_path, keep=1) == [older]
    assert f"pruning study: {older.name}" in capsys.readouterr().out
    assert not older.exists()
    assert newer.exists()
