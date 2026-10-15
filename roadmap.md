# Roadmap — make the toolkit harder to fool yourself with

## What this project is trying to be
A careful replay machine for one narrow trading idea, plus the plumbing that tells you whether a result is likely real or just a lucky window/setting/trade count. The current ambition is not "more features"; it is "harder to fool yourself, easier to ask the next skeptical question, clearer about what it does not model."

## Current state
- One strategy: EMA crossover with an ATR trailing stop and volatility-based sizing.
- One honest engine: fills at next open, fees and slippage on every order, stops tested against the bar range with gap fills at the open, no lookahead by construction.
- One honesty layer: a robustness sweep that tests breadth across markets/settings and a random-timing luck test, plus a conservative verdict line.
- One dashboard: a single self-contained page that reads saved run JSONs and shows the curve, drawdown, spec sheet, trade log, and keyboard/theme/motion behavior.

## What changed in this pass
1. **Post-backtest honesty is now unavoidable.**
   - `run_backtest.py` now prints the result, the verdict, and a short "Why this might flatter the strategy" list when the result has weak points, and it always names `run_robustness.py --grid small` as the next step.
   - `quant/report.py` gained `weak_points()` so the same skeptical read can be reused by the CLI and the dashboard.
2. **Cherry-picking is a little harder.**
   - `run_robustness.py` gained a `--windows` option so you can run the same small sweep across several start dates and fold them into one aggregate.
   - The README now says plainly that multi-window is a first cut, not a clean out-of-sample study.
3. **The "what it does not model" line is explicit.**
   - A new README section lists the modeled and unmodeled pieces in one place, plus the usual caveats about shorting, one-symbol-at-a-time, in-sample sweep, bar-only data, and venue survivorship.
4. **The dashboard now points at skepticism.**
   - Rail now has a "Robustness sweep" button, and the keyboard strip now lists `b` for best-setting and `B` for full robustness sweep, both wired to `run_robustness.py`.

## Roadmap, in the order I would do it

### 1. Make the honest next step the default behavior, not an extra command
- After one backtest, the tool should make it annoying to stop at "one pretty run."
- Small default robustness sweep should be the obvious next move from both the CLI and the dashboard.
- The weakest part of a result should be visible before the impressive part.

### 2. Reduce the easiest ways to flatter a result
- Make multi-window studies easier to run and read.
- Make it easier to compare a tuned corner of the grid against its neighbors.
- Keep the luck test visible, not buried in a report only power users read.
- Keep calling out low trade counts, cost assumptions, and single-window bias as first-class objections.

### 3. Improve realism where it matters most
- Better cost modeling: slippage that can rise with volatility or thin liquidity.
- Cleaner treatment of the cases where fills are less polite than the model assumes.
- Clearer notes on shorting, funding, borrow, liquidation, partial fills, and anything intrabar.
- Better explicit treatment of what "in-sample" means and where the current sweep can still flatter you.

### 4. Make the dashboard read risk first, not just reward
- Drawdown, time-in-market, trade count, and cost share should be as visible as the equity curve.
- Fragility should be surfaced: biggest losers, concentration, and whether the result leans on a few trades.
- The dashboard should make the skeptical view easy to open from the run page itself.

### 5. Make the software itself more trustworthy
- Cleaner separation of fetch / run / study / report, so each piece is easier to test and reuse.
- Better error messages and failure modes, especially for "nothing ran" and "only one window worked."
- A robustness-path test or smoke test, because the honest machinery deserves its own coverage.
- Fewer hidden assumptions in the CLI and dashboard, and clearer messages when they disagree.

### 6. Make learning and comparison easier, not just testing
- A couple of alternate strategy skeletons so you can compare ideas instead of only tuning one.
- Export paths that make it painless to take a result into a notebook or a second opinion.
- Reusable study helpers so you can ask "where does this break" without writing a new CLI each time.
- A clearer line between exploration and decision: when a result is merely suggestive, say so.

## The bigger moves, if this became the whole plan
1. A real walk-forward study: pick parameters on one chunk of history, judge them on a later chunk they never saw.
2. More strategy skeletons: the useful question is usually whether a family of ideas survives, not whether one idea can be tuned harder.
3. A confidence/inference layer: tell you when a result is interesting but still too thin to trust.
4. Easier comparison between ideas: shared grids, side-by-side reads, and exportable summaries.
5. A clearer decision line: the toolkit should get better at distinguishing "suggestive" from "worthy of real money."

## What would not be a good use of time next
- Making the dashboard prettier without also making the skeptical view harder to miss.
- Adding more knobs to one strategy before the toolkit can answer whether the idea survives at all.
- Treating a single good backtest as a finding instead of as a hypothesis worth one more honest test.

## One-sentence version
Build the system that makes it harder to fool yourself, easier to ask the next skeptical question, and clearer about what it does not know.
