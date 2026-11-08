#!/usr/bin/env python
"""Rolling walk-forward: choose on one chunk, judge on the next unseen chunk.

The single 70/30 split answered one question once: it picked settings on the
first 70% of history and scored them on the last 30%. That is better than an
in-sample sweep, but it is still one guess with one score -- a single friendly
window can carry it.

This rolls the same test. History is cut into ``--folds`` consecutive chunks.
For each pair, the earlier chunk picks parameters (highest return) and the
following chunk -- which the picker never saw -- judges them. Every chunk after
the first is judged exactly once, so the question changes from "did that pick
work once?" to "does this family keep holding up as the market moves?"

Two readings worth looking at:

- **beat rate**: how often the honestly chosen settings beat buy & hold on the
  next unseen chunk. One or two out of four is noise; most of them is worth a
  longer look.
- **regret**: how far the chosen settings landed below that chunk's best
  possible settings. Large regret means selection is mostly noise, even when
  the family itself is fine.

Examples:
    python run_walkforward.py                            # Bollinger reversion, BTC + ETH, 4h
    python run_walkforward.py --family both              # legacy EMA + Donchian comparison
    python run_walkforward.py --folds 4 --timeframe 1d
    python run_walkforward.py --symbols BTC/USDT --family donchian

Every run saves its own dated study file, and the directory keeps the newest
``KEEP_STUDIES`` of them: older dated studies are pruned after each run so a
daily rerun does not pile up an archive nobody reads. ``--keep-studies 0``
turns that housekeeping off.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from quant.config import BacktestConfig
from quant.datafeed import load_candles
from quant.engine import Engine
from quant.robustness import small_param_grid, make_strategy, short_params
from quant.strategies.bollinger import BollingerReversion
from quant.strategies.donchian import DonchianBreakout

DONCHIAN_GRID = [(20, 10), (30, 15), (55, 20)]
BOLLINGER_GRID = (
    {"period": 20, "entry_k": 2.0, "atr_stop_mult": 2.5},
    {"period": 30, "entry_k": 2.0, "atr_stop_mult": 2.5},
    {"period": 50, "entry_k": 2.5, "atr_stop_mult": 3.0},
)
FAMILY_LABEL = {"bollinger": "Bollinger reversion", "ema": "EMA-ATR", "donchian": "Donchian"}
MIN_CHUNK_BARS = 300

STUDY_GLOB = "walkforward_rolling_*.json"
KEEP_STUDIES = 10
_STUDY_STAMP = re.compile(
    r"^walkforward_rolling_(?P<label>.+)_(?P<stamp>\d{8}T\d{6}Z)\.json$"
)


def study_stamp(path: Path) -> datetime | None:
    """The run time recorded in a dated study filename, or ``None`` for other names."""
    match = _STUDY_STAMP.match(path.name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group("stamp"), "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def _readable_study(path: Path) -> bool:
    """True only for a complete rolling-walk-forward study."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("study") == "rolling_walkforward"


def prune_studies(directory: Path, keep: int = KEEP_STUDIES, *, dry_run: bool = False) -> list[Path]:
    """List or delete the oldest dated studies, keeping the newest ``keep``.

    Archive housekeeping, not research: every run keeps its own file so studies
    stay comparable, but a daily rerun would otherwise pile up hundreds. Only
    files this script names are candidates -- a study with no date stamp, a file
    whose name is not a run time, or one whose contents are not a study is left
    alone. Order comes from the filename rather than the file's mtime, so copying
    or restoring the archive does not change which study looks oldest.
    """
    if keep < 1:
        return []
    dated: list[tuple[datetime, Path]] = []
    for path in directory.glob(STUDY_GLOB):
        stamp = study_stamp(path)
        # A file that no longer parses as a study is kept, and does not use up
        # one of the slots: it is not something this archive can vouch for.
        if stamp is not None and path.is_file() and _readable_study(path):
            dated.append((stamp, path))
    dated.sort(key=lambda item: (item[0], item[1].name), reverse=True)

    candidates = [path for _, path in dated[keep:]]
    if dry_run:
        return candidates

    removed: list[Path] = []
    for path in candidates:
        print(f"pruning study: {path.name}", flush=True)
        try:
            path.unlink()
        except OSError as exc:
            print(f"could not prune {path.name}: {exc}", file=sys.stderr)
            continue
        removed.append(path)
    return removed


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Rolling walk-forward: pick on one chunk, judge on the next.")
    p.add_argument("--symbols", default="BTC/USDT,ETH/USDT", help="comma-separated pairs")
    p.add_argument("--timeframe", default="4h")
    p.add_argument("--start", default="2021-01-01")
    p.add_argument("--folds", type=int, default=5, help="consecutive chunks (3-10); folds-1 are judged")
    p.add_argument("--family", default="bollinger", choices=["bollinger", "ema", "donchian", "both"])
    p.add_argument(
        "--out",
        default=None,
        help="output JSON (default results/walkforward_rolling_<tf>_<UTCstamp>.json)",
    )
    p.add_argument(
        "--keep-studies",
        type=int,
        default=KEEP_STUDIES,
        metavar="N",
        help=(f"dated studies to keep in the output directory; older ones are pruned "
              f"(default {KEEP_STUDIES}, 0 keeps every file)"),
    )
    p.add_argument(
        "--dry-run-prune",
        action="store_true",
        help="list files that would be pruned without running the study or deleting anything",
    )
    return p


def fold_windows(n: int, folds: int) -> list[tuple[int, int, int, int]]:
    """``(train_lo, train_hi, test_lo, test_hi)`` for each consecutive chunk pair.

    Chunks are equal-sized to within one bar. Fold *i* trains on chunk *i-1* and
    is judged on chunk *i*, so the first chunk is training only and every later
    chunk is judged once, immediately after it was still unseen.
    """
    if folds < 2:
        raise ValueError("need at least 2 chunks to have a train and a test")
    if n // folds < 2:
        raise ValueError(f"{n} bars cannot fill {folds} chunks with 2+ bars each")
    edges = [n * i // folds for i in range(folds + 1)]
    return [(edges[i - 1], edges[i], edges[i], edges[i + 1]) for i in range(1, folds)]


def candidate_rows(family: str) -> list[tuple[str, Callable[[], object]]]:
    """``(label, factory)`` pairs for one family; each factory makes a fresh strategy."""
    if family == "bollinger":
        return [
            (
                f"bb{int(p['period'])} k{float(p['entry_k']):.1f}",
                lambda p=p: BollingerReversion(
                    period=int(p["period"]),
                    entry_k=float(p["entry_k"]),
                    trend_period=200,
                    atr_period=14,
                    atr_stop_mult=float(p["atr_stop_mult"]),
                    risk_pct=0.02,
                    max_weight=1.0,
                    trend_filter=True,
                ),
            )
            for p in BOLLINGER_GRID
        ]
    if family == "ema":
        return [(short_params(p), (lambda params=p: make_strategy(params)))
                for p in small_param_grid()]
    return [(f"{entry}/{exit_}", (lambda entry=entry, exit_=exit_: DonchianBreakout(entry_n=entry, exit_m=exit_)))
            for entry, exit_ in DONCHIAN_GRID]


def run_family(cfg: BacktestConfig, train_c, test_c, family: str) -> dict:
    """Score every setting on train and test, then report the honest pick.

    The pick is made on train return only -- the test numbers are recorded
    afterwards and never influence the choice.
    """
    candidates = []
    for label, build in candidate_rows(family):
        tr = Engine(cfg, build()).run(train_c)
        te = Engine(cfg, build()).run(test_c)
        strat_dd = abs(float(te.metrics.get("max_drawdown_pct", 0.0)))
        hold_dd = abs(float(te.metrics.get("buy_hold_max_drawdown", 0.0))) * 100.0
        candidates.append({
            "params": label,
            "train_ret": round(float(tr.metrics["total_return_pct"]), 2),
            "train_trades": int(tr.metrics["trades"]),
            "test_ret": round(float(te.metrics["total_return_pct"]), 2),
            "test_trades": int(te.metrics["trades"]),
            "test_excess": round(float(te.metrics["excess_return_pct"]), 2),
            "test_dd_pct": round(strat_dd, 2),
            "test_buy_hold_dd_pct": round(hold_dd, 2),
            "risk_adjusted_beat": _earns_more_per_drawdown(
                float(te.metrics["total_return_pct"]), strat_dd,
                float(te.metrics["buy_hold_return_pct"]), hold_dd,
            ),
        })

    chosen = max(candidates, key=lambda r: r["train_ret"])
    best_test = max(candidates, key=lambda r: r["test_ret"])
    return {
        "family": family,
        "chosen_params": chosen["params"],
        "train_ret": chosen["train_ret"],
        "train_trades": chosen["train_trades"],
        "test_ret": chosen["test_ret"],
        "test_trades": chosen["test_trades"],
        "test_excess": chosen["test_excess"],
        "test_dd_pct": chosen["test_dd_pct"],
        "test_buy_hold_dd_pct": chosen["test_buy_hold_dd_pct"],
        "risk_adjusted_beat": chosen["risk_adjusted_beat"],
        "best_test_params": best_test["params"],
        "best_test_ret": best_test["test_ret"],
        "regret": round(chosen["test_ret"] - best_test["test_ret"], 2),
        "candidates": candidates,
    }


def _earns_more_per_drawdown(ret: float, dd: float, hold_ret: float, hold_dd: float) -> bool | None:
    """Return per unit of drawdown, or ``None`` when either side barely drew down.

    A strategy that beats buy & hold on return but risks three times as much is
    not the same result as one that beats it while risking less. Small drawdowns
    make the ratio meaningless, so those windows are left out of the count.
    """
    if dd < 1.0 or hold_dd < 1.0:
        return None
    return (ret / dd) > (hold_ret / hold_dd)


def verdict_for(beat: int, windows: int, risk_beat: int | None = None,
                risk_windows: int = 0) -> str:
    """One plain sentence about whether a family survived its unseen chunks."""
    if windows < 3:
        return f"Too few unseen chunks ({windows}) to read; use more history or fewer folds."
    pct = 100.0 * beat / windows
    if pct >= 75.0:
        return (f"Held up in {beat} of {windows} unseen chunks -- consistent enough to keep "
                f"studying, not enough to call an edge.")
    if risk_beat is not None and risk_windows and 100.0 * risk_beat / risk_windows >= 75.0:
        return (f"Beat buy & hold in only {beat} of {windows} unseen chunks, but earned more per "
                f"unit of drawdown in {risk_beat} of {risk_windows} -- a lower-risk path, not a "
                f"higher-return one.")
    if pct >= 50.0:
        return (f"Mixed: beat buy & hold in {beat} of {windows} unseen chunks -- no reliable "
                f"edge in this sample.")
    return (f"Did not hold up: beat buy & hold in only {beat} of {windows} unseen chunks; the "
            f"edge lived in the training window.")


def summarize(rows: list[dict], family: str, symbol: str) -> dict:
    """Aggregate one family's per-fold rows into a reading."""
    excess = [r["test_excess"] for r in rows]
    returns = [r["test_ret"] for r in rows]
    regrets = [r["regret"] for r in rows]
    trades = [r["test_trades"] for r in rows]
    beat = sum(1 for e in excess if e > 0)
    risk_rows = [r for r in rows if r["risk_adjusted_beat"] is not None]
    risk_beat = sum(1 for r in risk_rows if r["risk_adjusted_beat"])
    picks = [r["chosen_params"] for r in rows]
    modal = max(set(picks), key=picks.count) if picks else None
    summary = {
        "symbol": symbol,
        "family": family,
        "windows": len(rows),
        "beat_buy_hold": beat,
        "beat_buy_hold_pct": round(100.0 * beat / len(rows), 1) if rows else 0.0,
        "risk_adjusted_beat": risk_beat,
        "risk_adjusted_windows": len(risk_rows),
        "risk_adjusted_pct": round(100.0 * risk_beat / len(risk_rows), 1) if risk_rows else None,
        "median_test_ret": round(statistics.median(returns), 2) if returns else None,
        "median_test_excess": round(statistics.median(excess), 2) if excess else None,
        "median_regret": round(statistics.median(regrets), 2) if regrets else None,
        "median_test_trades": statistics.median(trades) if trades else None,
        "picks": picks,
        "modal_pick": modal,
        "modal_pick_pct": round(100.0 * picks.count(modal) / len(picks), 1) if modal else None,
        "regret_note": None,
        "verdict": verdict_for(beat, len(rows), risk_beat, len(risk_rows)),
    }
    if regrets and statistics.median(regrets) <= -5.0:
        summary["regret_note"] = (
            f"Picking settings on the past landed {abs(statistics.median(regrets)):.1f} points "
            f"below that chunk's best settings (median), so selection is mostly noise."
        )
    return summary


def run_symbol(cfg: BacktestConfig, candles, folds: int, families: list[str]) -> dict:
    """Roll the study across one market and return its per-family readings."""
    n = len(candles)
    windows = fold_windows(n, folds)
    families_out: dict[str, dict] = {}
    for family in families:
        rows = []
        for i, (train_lo, train_hi, test_lo, test_hi) in enumerate(windows, start=1):
            train_c = candles._take(train_lo, train_hi)
            test_c = candles._take(test_lo, test_hi)
            row = run_family(cfg, train_c, test_c, family)
            row.update({
                "fold": i,
                "train_start": train_c.start_iso,
                "train_end": train_c.end_iso,
                "test_start": test_c.start_iso,
                "test_end": test_c.end_iso,
            })
            rows.append(row)
            print(f"  fold {i}/{len(windows)} [{family}] picked {row['chosen_params']} "
                  f"train {row['train_ret']:+.2f}% -> test {row['test_ret']:+.2f}% "
                  f"excess {row['test_excess']:+.2f}%", flush=True)
        families_out[family] = summarize(rows, family, cfg.symbol)
        families_out[family]["rows"] = rows
    return {
        "symbol": cfg.symbol,
        "timeframe": cfg.timeframe,
        "bars": n,
        "chunks": folds,
        "chunk_bars": n // folds,
        "windows": len(windows),
        "first_train": candles.start_iso,
        "last_test": candles.end_iso,
        "families": families_out,
    }


def combine(symbols: list[dict], families: list[str]) -> dict:
    """Pool a family's windows across markets for one headline each."""
    out = {}
    for family in families:
        rows = [s["families"][family] for s in symbols]
        total_windows = sum(r["windows"] for r in rows)
        beat = sum(r["beat_buy_hold"] for r in rows)
        risk_beat = sum(r["risk_adjusted_beat"] for r in rows)
        risk_windows = sum(r["risk_adjusted_windows"] for r in rows)
        excess = [x for r in rows for x in [r["median_test_excess"]] if x is not None]
        out[family] = {
            "windows": total_windows,
            "beat_buy_hold": beat,
            "beat_buy_hold_pct": round(100.0 * beat / total_windows, 1) if total_windows else 0.0,
            "risk_adjusted_beat": risk_beat,
            "risk_adjusted_windows": risk_windows,
            "risk_adjusted_pct": round(100.0 * risk_beat / risk_windows, 1) if risk_windows else None,
            "markets": [r["symbol"] for r in rows],
            "verdict": verdict_for(beat, total_windows, risk_beat, risk_windows),
            "median_test_excess": round(statistics.median(excess), 2) if excess else None,
        }
    return out


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.keep_studies < 0:
        print("error: --keep-studies must be 0 or greater", file=sys.stderr)
        return 2
    if args.dry_run_prune:
        directory = Path(args.out).parent if args.out else Path("results")
        candidates = prune_studies(directory, args.keep_studies, dry_run=True)
        if candidates:
            print(f"Would prune {len(candidates)} study file(s) from {directory}:")
            for path in candidates:
                print(f"  {path.name}")
        else:
            print(f"No study files would be pruned from {directory}.")
        return 0
    if not 3 <= args.folds <= 10:
        print("error: --folds must be 3-10", file=sys.stderr)
        return 2
    fams = ["ema", "donchian"] if args.family == "both" else [args.family]
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        print("error: --symbols is empty", file=sys.stderr)
        return 2

    results = []
    for symbol in symbols:
        cfg = BacktestConfig(symbol=symbol, timeframe=args.timeframe, start=args.start)
        candles = load_candles(symbol, args.timeframe, cfg.start, None, provider=cfg.provider)
        if len(candles) // args.folds < MIN_CHUNK_BARS:
            print(f"error: {symbol} {args.timeframe} has {len(candles)} bars; {args.folds} "
                  f"folds leaves under {MIN_CHUNK_BARS} bars per chunk. Use fewer folds or an "
                  f"earlier --start.", file=sys.stderr)
            return 2
        print(f"== {symbol} {args.timeframe}: {len(candles)} bars from {candles.start_iso} "
              f"to {candles.end_iso}, {args.folds} chunks ==")
        results.append(run_symbol(cfg, candles, args.folds, fams))

    headline = combine(results, fams)
    print()
    print("ROLLING WALK-FORWARD VERDICT")
    for family in fams:
        h = headline[family]
        print(f"  {FAMILY_LABEL[family]}: beat buy & hold in {h['beat_buy_hold']} of "
              f"{h['windows']} unseen windows across {', '.join(h['markets'])} "
              f"({h['beat_buy_hold_pct']:.0f}%); better return-per-drawdown in "
              f"{h['risk_adjusted_beat']} of {h['risk_adjusted_windows']}.")
        print(f"    {h['verdict']}")
        for s in results:
            r = s["families"][family]
            note = f"  {r['regret_note']}" if r["regret_note"] else ""
            print(f"    - {s['symbol']}: {r['beat_buy_hold']}/{r['windows']} windows, "
                  f"median excess {r['median_test_excess']:+.2f} pts, "
                  f"risk-adjusted {r['risk_adjusted_beat']}/{r['risk_adjusted_windows']}, "
                  f"median regret {r['median_regret']:+.2f} pts, "
                  f"most-picked {r['modal_pick']} ({r['modal_pick_pct']:.0f}% of folds).{note}")

    out = {
        "study": "rolling_walkforward",
        "timeframe": args.timeframe,
        "folds": args.folds,
        "families": fams,
        "note": ("Each chunk picks parameters on the chunk immediately before it and is judged "
                 "on the next, never-seen chunk. Picks use train return only; test numbers are "
                 "recorded afterwards. Regret is the chosen test return minus that chunk's best "
                 "possible test return, so a large negative regret means selection is noisy."),
        "symbols": results,
        "headline": headline,
    }
    # Every run keeps its own dated file. One rolling filename would overwrite the
    # previous study, and the dashboard would lose the option to compare them.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    default_name = (
        f"results/walkforward_rolling_{args.timeframe.replace('/', '-')}_{stamp}.json"
    )
    path = Path(args.out) if args.out else Path(default_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write beside the target and rename, so a dashboard reading during a run
    # sees either the old study or the new one, never half a file.
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(out, indent=2))
    tmp.replace(path)
    print(f"\nsaved -> {path}")

    pruned = prune_studies(path.parent, args.keep_studies)
    if pruned:
        print(
            f"pruned {len(pruned)} older study file(s); "
            f"kept the newest {args.keep_studies}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
