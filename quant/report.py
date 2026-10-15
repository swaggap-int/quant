"""Console reporting. Terminal-first, because that is where a backtest gets read."""

from __future__ import annotations

from typing import Any

from quant.engine import BacktestResult


def _money(x: float) -> str:
    return f"{x:,.2f}"


def _pct(x: float, places: int = 2) -> str:
    return f"{x * 100:+.{places}f}%"


def _ratio(x: float) -> str:
    if x == float("inf"):
        return "inf"
    return f"{x:.2f}"


def _rule(width: int = 74, char: str = "-") -> str:
    return char * width


def format_report(result: BacktestResult) -> str:
    """Human-readable summary of a run."""
    m = result.metrics
    d = result.candles
    cfg = result.config
    strat = result.strategy["params"]
    lines: list[str] = []

    lines.append(_rule(74, "="))
    lines.append(f" {d.symbol} {d.timeframe}   {d.start_iso} -> {d.end_iso}   [{result.run_id}]")
    lines.append(
        f" strategy  {result.strategy['name']}  "
        f"{' '.join(f'{k}={v}' for k, v in strat.items() if not k.startswith('_'))}"
    )
    lines.append(
        f" costs     fee {cfg.costs.fee_bps:g}bps + slip {cfg.costs.slippage_bps:g}bps per side "
        f"= {cfg.costs.round_trip_bps:g}bps round trip"
    )
    lines.append(_rule(74, "="))

    lines.append(" RETURNS")
    lines.append(f"  start / final equity   {_money(m['start_equity']):>14} -> {_money(m['final_equity'])}")
    lines.append(f"  total return           {_pct(m['total_return']):>14}   over {m['years']:.2f} years")
    lines.append(f"  CAGR                   {_pct(m['cagr']):>14}")
    lines.append(f"  buy & hold             {_pct(m.get('buy_hold_return', 0.0)):>14}   (same window, no costs)")
    lines.append(f"  excess vs buy & hold   {_pct(m.get('excess_return_pct', 0.0) / 100.0):>14}")

    lines.append(" RISK")
    lines.append(f"  max drawdown           {_pct(m['max_drawdown']):>14}   ({m['max_drawdown_bars']} bars at worst)")
    lines.append(f"  annual volatility      {_pct(m['annual_vol']):>14}")
    lines.append(f"  Sharpe / Sortino / Calmar   {_ratio(m['sharpe'])} / {_ratio(m['sortino'])} / {_ratio(m['calmar'])}")
    lines.append(f"  time in market         {m['bars_in_market_pct']:>13.1f}%   ({m['bars']} bars total)")

    lines.append(" TRADES")
    lines.append(f"  count                  {m['trades']:>14}")
    lines.append(f"  win rate               {m['win_rate_pct']:>13.1f}%   avg win {_money(m['avg_win'])}, avg loss {_money(m['avg_loss'])}")
    lines.append(f"  profit factor          {_ratio(m['profit_factor']):>14}   expectancy {_money(m['expectancy'])}/trade")
    lines.append(f"  best / worst           {_money(m['best_trade']):>14} / {_money(m['worst_trade'])}")
    lines.append(f"  avg bars held          {m['avg_bars_held']:>14.1f}   avg R {_ratio(m['avg_r_multiple'])}")

    lines.append(" COSTS")
    lines.append(f"  fees paid              {_money(m['fees_paid']):>14}   ({m['fees_pct_of_start']:.2f}% of start equity)")
    lines.append(f"  turnover               {_ratio(m['turnover_multiple']):>14}x   (notional traded / avg equity)")

    if result.monthly:
        recent = result.monthly[-12:]
        lines.append(" RECENT MONTHS")
        cells = "  ".join(f"{r['month'][2:]}:{r['return_pct']:+.1f}%" for r in recent)
        lines.append("  " + cells)

    if result.notes:
        lines.append(" NOTES")
        for note in result.notes:
            lines.append(f"  - {note}")

    lines.append(_rule(74, "="))
    return "\n".join(lines)


def weak_points(metrics: dict[str, Any]) -> list[str]:
    """The reasons this single backtest result might flatter the strategy.

    Ordered toward the commonest self-deception: low trade count, window luck,
    parameter luck, cost luck, regime luck. Together with ``verdict`` this is
    the honest part of a single-run report.
    """
    m = metrics
    trades = m.get("trades", 0)
    points: list[str] = []
    if trades < 20:
        points.append(f"only {trades} trades — too few to be calm about")
    if m.get("excess_return_pct", 0.0) <= 0:
        points.append("it did not beat buy & hold in this window")
    if abs(m.get("max_drawdown", 0.0)) > 0.5:
        points.append(f"{_pct(m['max_drawdown'])} drawdown")
    if m.get("profit_factor", 0.0) < 1.2 and trades:
        points.append(f"profit factor {_ratio(m.get('profit_factor', 0.0))}")
    if m.get("fees_pct_of_start", 0.0) > 0.25 * max(m.get("total_return_pct", 0.0), 0.0) and m.get("fees_paid", 0.0) > 0:
        points.append("fees eat a large share of the return")
    return points


def verdict(metrics: dict[str, Any]) -> str:
    """One-line sanity judgement from a metrics dict, deliberately conservative.

    Takes the dict rather than a result so the dashboard, the CLI and any
    future caller all read the same rulebook.
    """
    problems = weak_points(metrics)
    if not problems:
        return "No red flags in this sample. That is not the same as an edge: test other windows and assets."
    return "Read with care: " + "; ".join(problems) + "."
