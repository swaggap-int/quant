#!/usr/bin/env python
"""Run one backtest from the command line.

Examples
--------
    python run_backtest.py                                   # BTC/USDT daily, 1y
    python run_backtest.py --symbol ETH/USDT --timeframe 4h --start 2022-01-01
    python run_backtest.py --provider kraken --refresh        # force a refetch
    python run_backtest.py --csv data/mycandles.csv           # fully offline
    python run_backtest.py --fee-bps 0 --slippage-bps 0        # frictionless (unrealistic)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from quant.config import BacktestConfig, Costs, RiskConfig
from quant.datafeed import PROVIDERS, DataError, load_candles
from quant.engine import Engine
from quant.report import format_report, verdict
from quant.strategies import EmaAtrTrend

RESULTS_DIR = Path(__file__).resolve().parent / "results"
DEFAULT_START = None  # let the data layer pick (one year back)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_backtest.py",
        description="Event-driven crypto backtester (fees, slippage, stops, risk sizing).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    data = p.add_argument_group("data")
    data.add_argument("--symbol", default="BTC/USDT", help="pair, e.g. BTC/USDT")
    data.add_argument("--timeframe", default="1d", choices=["1m", "5m", "15m", "1h", "4h", "1d", "1w"])
    data.add_argument("--start", default=DEFAULT_START, help="YYYY-MM-DD (inclusive)")
    data.add_argument("--end", default=None, help="YYYY-MM-DD (inclusive); default now")
    data.add_argument("--provider", default="auto", choices=["auto", *PROVIDERS])
    data.add_argument("--csv", default=None, help="read bars from a local CSV (no network)")
    data.add_argument("--refresh", action="store_true", help="ignore the cache and refetch")

    money = p.add_argument_group("account")
    money.add_argument("--cash", type=float, default=10_000.0, help="starting equity")
    money.add_argument("--fee-bps", type=float, default=10.0, help="taker fee per side, basis points")
    money.add_argument("--slippage-bps", type=float, default=5.0, help="slippage per fill, basis points")
    money.add_argument("--risk", type=float, default=0.02, help="equity fraction risked per trade")
    money.add_argument("--max-position", type=float, default=1.0, help="max notional as fraction of equity")
    money.add_argument("--allow-short", action="store_true", help="allow short entries (no funding modelled)")

    strat = p.add_argument_group("strategy: EMA cross + ATR trailing stop")
    strat.add_argument("--fast", type=int, default=20)
    strat.add_argument("--slow", type=int, default=50)
    strat.add_argument("--atr-period", type=int, default=14)
    strat.add_argument("--atr-stop-mult", type=float, default=2.5)
    strat.add_argument("--trail-mult", type=float, default=3.0)
    strat.add_argument("--no-trend-filter", action="store_true", help="trade crosses without the slow-EMA filter")

    out = p.add_argument_group("output")
    out.add_argument("--out", default=None, help="result JSON path (default results/<run_id>.json)")
    out.add_argument("--no-save", action="store_true", help="print only, write nothing")
    out.add_argument("--json", action="store_true", help="emit the full result JSON to stdout")
    out.add_argument("--quiet", action="store_true", help="suppress the data/cache chatter")
    return p


def make_config(args: argparse.Namespace) -> BacktestConfig:
    return BacktestConfig(
        symbol=args.symbol,
        timeframe=args.timeframe,
        start=args.start,
        end=args.end,
        initial_cash=args.cash,
        costs=Costs(fee_bps=args.fee_bps, slippage_bps=args.slippage_bps),
        risk=RiskConfig(
            risk_per_trade=args.risk,
            max_position_pct=args.max_position,
            allow_short=args.allow_short,
        ),
        provider=args.provider,
    )


def make_strategy(args: argparse.Namespace) -> EmaAtrTrend:
    return EmaAtrTrend(
        fast=args.fast,
        slow=args.slow,
        atr_period=args.atr_period,
        atr_stop_mult=args.atr_stop_mult,
        trail_mult=args.trail_mult,
        risk_pct=args.risk,
        max_weight=args.max_position,
        trend_filter=not args.no_trend_filter,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = make_config(args)
    strategy = make_strategy(args)

    try:
        candles = load_candles(
            config.symbol,
            config.timeframe,
            config.start,
            config.end,
            provider=config.provider,
            refresh=args.refresh,
            csv_path=args.csv,
            verbose=not args.quiet and not args.json,
        )
        result = Engine(config, strategy).run(candles)
    except (DataError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    from quant.report import weak_points

    if args.json:
        import json

        print(json.dumps(result.to_dict(), indent=2, allow_nan=False))
    else:
        print(format_report(result))
        print(f" {verdict(result.metrics)}")
        w = weak_points(result.metrics)
        if w:
            print(" Why this might flatter the strategy:")
            for line in w:
                print(f"   - {line}")
            print(" Next step if you care about the result: run_robustness.py --grid small")
        else:
            print(" Next step if you care about the result: run_robustness.py --grid small")

    if not args.no_save:
        path = Path(args.out) if args.out else RESULTS_DIR / f"{result.run_id}.json"
        if args.json:
            print(f"saved: {path}", file=sys.stderr)
        else:
            print(f" saved -> {path}")
        result.save(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
