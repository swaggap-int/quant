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
    scope.add_argument("--start", default="2021-01-01", help="YYYY-MM-DD (inclusive)")
    scope.add_argument("--end", default=None, help="YYYY-MM-DD (inclusive); default now")
    scope.add_argument("--provider", default="auto", choices=["auto", *PROVIDERS])
    scope.add_argument("--refresh", action="store_true", help="ignore the candle cache and refetch")
    scope.add_argument(
        "--grid",
        default="default",
        choices=["small", "default"],
        help="small = 3 settings, default = 15 spanning trend speed and stop width",
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    markets = parse_markets(args)
    grid = small_param_grid(args.risk) if args.grid == "small" else default_param_grid(args.risk)

    config = BacktestConfig(
        symbol=markets[0].symbol,
        timeframe=markets[0].timeframe,
        start=args.start,
        end=args.end,
        initial_cash=args.cash,
        costs=Costs(fee_bps=args.fee_bps, slippage_bps=args.slippage_bps),
        risk=RiskConfig(risk_per_trade=args.risk, max_position_pct=1.0),
        provider=args.provider,
    )

    progress = None if args.quiet or args.json else _progress
    try:
        report = run_study(
            markets,
            grid,
            base_config=config,
            min_trades=args.min_trades,
            refresh=args.refresh,
            # With --json or --quiet, stdout belongs to the caller: no data chatter.
            verbose=not args.quiet and not args.json,
            null_runs=args.null_runs,
            seed=args.seed,
            on_progress=progress,
        )
    except (DataError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, allow_nan=False))
    else:
        print(report)

    if not args.no_save:
        path = Path(args.out) if args.out else DEFAULT_OUT
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report.to_dict(), indent=2, allow_nan=False), encoding="utf-8")
        if not args.json:
            print(f" saved -> {path}")

    # Breadth is the headline: nothing ran means the caller should know.
    return 0 if report.summary["ran"] else 2


def _progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
