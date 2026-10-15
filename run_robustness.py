#!/usr/bin/env python
"""Sweep the strategy across markets, timeframes and settings to see if the
result generalises, and control for luck with randomly timed entries.

Examples
--------
    python run_robustness.py                          # 4 markets x 15 settings + luck test
    python run_robustness.py --quick                   # 3 settings, 50 random schedules
    python run_robustness.py --symbols BTC/USDT --timeframes 1d,4h
    python run_robustness.py --null-runs 0             # breadth only, no luck test
    python run_robustness.py --json --out results/robustness.json

The report's headline is "beat buy & hold in X% of combinations". The luck test
is the honest part: it re-runs the same engine on coin-flip entry timing with
the same trade count, holding times, sizing, stops, fees and slippage.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from quant.config import BacktestConfig, Costs, RiskConfig
from quant.datafeed import PROVIDERS, DataError
from quant.robustness import (
    DEFAULT_MIN_TRADES,
    DEFAULT_NULL_RUNS,
    Market,
    default_markets,
    default_param_grid,
    run_study,
    small_param_grid,
    summarize,
    SweepCell,
    SweepReport,
)

DEFAULT_OUT = Path(__file__).resolve().parent / "results" / "robustness.json"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_robustness.py",
        description="Ask whether a backtest result survives other markets, timeframes and settings.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    scope = p.add_argument_group("scope")
    scope.add_argument("--symbols", default=None, help="comma-separated, e.g. BTC/USDT,ETH/USDT")
    scope.add_argument("--timeframes", default=None, help="comma-separated, e.g. 1d,4h")
    scope.add_argument("--start", default="2021-01-01", help="YYYY-MM-DD (inclusive); default window start")
    scope.add_argument("--end", default=None, help="YYYY-MM-DD (inclusive); default now")
    scope.add_argument("--provider", default="auto", choices=["auto", *PROVIDERS])
    scope.add_argument("--refresh", action="store_true", help="ignore the candle cache and refetch")
    scope.add_argument(
        "--grid",
        default="default",
        choices=["small", "default"],
        help="small = 3 settings, default = 15 spanning trend speed and stop width",
    )
    scope.add_argument(
        "--windows",
        default=None,
        help="comma-separated start dates to test across time, e.g. 2020-01-01,2022-01-01,2024-01-01; each is paired with the same end",
    )

    money = p.add_argument_group("account")
    money.add_argument("--cash", type=float, default=10_000.0)
    money.add_argument("--fee-bps", type=float, default=10.0)
    money.add_argument("--slippage-bps", type=float, default=5.0)
    money.add_argument("--risk", type=float, default=0.02, help="equity fraction risked per trade")

    luck = p.add_argument_group("luck test")
    luck.add_argument(
        "--null-runs",
        type=int,
        default=DEFAULT_NULL_RUNS,
        help="random-timing controls per market; 0 skips the test",
    )
    luck.add_argument("--seed", type=int, default=7, help="random seed, so a study is reproducible")
    luck.add_argument(
        "--min-trades",
        type=int,
        default=DEFAULT_MIN_TRADES,
        help="a cell must close this many trades before its return counts as good",
    )

    out = p.add_argument_group("output")
    out.add_argument("--json", action="store_true", help="emit the full study JSON to stdout")
    out.add_argument("--out", default=None, help=f"save the study here (default {DEFAULT_OUT})")
    out.add_argument("--no-save", action="store_true", help="print only, write nothing")
    out.add_argument("--quiet", action="store_true", help="suppress progress chatter")
    return p


def parse_markets(args: argparse.Namespace) -> list[Market]:
    if not args.symbols and not args.timeframes:
        return default_markets()
    symbols = [s.strip().upper() for s in (args.symbols or "BTC/USDT").split(",") if s.strip()]
    timeframes = [t.strip() for t in (args.timeframes or "1d").split(",") if t.strip()]
    return [Market(s, t) for s in symbols for t in timeframes]


def parse_windows(args: argparse.Namespace, default_start: str | None, default_end: str | None) -> list[tuple[str | None, str | None]]:
    """Return a small list of (start, end) windows to test breadth across time.

    If ``--windows`` is given, use those starts and pair each with a common end.
    Otherwise return the single default window so existing callers are unchanged.
    """
    if not args.windows:
        end = args.end or default_end
        return [(args.start or default_start, end)]
    starts = [s.strip() for s in args.windows.split(",") if s.strip()]
    if not starts:
        end = args.end or default_end
        return [(args.start or default_start, end)]
    end = args.end or default_end
    return [(start, end) for start in starts]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    markets = parse_markets(args)
    grid = small_param_grid(args.risk) if args.grid == "small" else default_param_grid(args.risk)

    windows = parse_windows(args, default_start="2021-01-01", default_end=None)

    parts: list[SweepReport] = []
    progress = None if args.quiet or args.json else _progress
    try:
        for start, end in windows:
            if start is None and end is None and len(windows) == 1:
                effective_start = args.start or "2021-01-01"
                effective_end = args.end
            else:
                effective_start = start or "2021-01-01"
                effective_end = args.end or end
            config = BacktestConfig(
                symbol=markets[0].symbol if markets else "BTC/USDT",
                timeframe=markets[0].timeframe if markets else "1d",
                start=effective_start,
                end=effective_end,
                initial_cash=args.cash,
                costs=Costs(fee_bps=args.fee_bps, slippage_bps=args.slippage_bps),
                risk=RiskConfig(risk_per_trade=args.risk, max_position_pct=1.0),
                provider=args.provider,
            )
            if progress:
                progress(f" window {effective_start or 'default'} -> {effective_end or 'default'}")
            parts.append(
                run_study(
                    markets,
                    grid,
                    base_config=config,
                    min_trades=args.min_trades,
                    refresh=args.refresh,
                    verbose=not args.quiet and not args.json,
                    null_runs=args.null_runs,
                    seed=args.seed,
                    on_progress=progress,
                )
            )
    except (DataError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(_merge_reports(parts).to_dict(), indent=2, allow_nan=False))
    else:
        for part in parts:
            print(part)
            print()
        print(_merge_reports(parts))
        print()

    if not args.no_save:
        path = Path(args.out) if args.out else DEFAULT_OUT
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_merge_reports(parts).to_dict(), indent=2, allow_nan=False), encoding="utf-8")
        if not args.json:
            print(f" saved -> {path}")

    merged = _merge_reports(parts)
    return 0 if merged.summary["ran"] else 2


def _progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _merge_reports(parts: list[SweepReport]) -> SweepReport:
    """Fold several windowed reports into one aggregate for the caller.

    Keeps the windowed prints above intact and gives the caller a single
    combined summary, cells and nulls. It is not a substitute for a proper
    walk-forward study, but it is a better first cut than a single window.
    """
    if len(parts) == 1:
        return parts[0]

    cells = [c for p in parts for c in p.cells]
    nulls = [n for p in parts for n in p.nulls]
    start_dates = sorted({p.window.get("start") for p in parts if p.window.get("start")})
    end_dates = sorted({p.window.get("end") for p in parts if p.window.get("end")})
    window = {
        "start": min(start_dates) if start_dates else parts[0].window.get("start"),
        "end": min(end_dates) if end_dates else parts[0].window.get("end"),
        "provider": parts[0].window.get("provider", "auto"),
        "fee_bps": parts[0].window.get("fee_bps", 10.0),
        "slippage_bps": parts[0].window.get("slippage_bps", 5.0),
        "windows": len(parts),
    }
    summary = summarize(cells, nulls, grid_size=parts[0].grid_size, min_trades=parts[0].summary.get("min_trades", DEFAULT_MIN_TRADES))
    return SweepReport(cells=cells, nulls=nulls, grid_size=parts[0].grid_size, window=window, summary=summary)


if __name__ == "__main__":
    raise SystemExit(main())
