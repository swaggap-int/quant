# quant

An event-driven crypto backtester with realistic costs, plus a local web dashboard
for comparing runs. One strategy ships with it: an EMA crossover with an ATR
trailing stop and volatility-based position sizing.

The point of this repo is not the strategy. It is the machinery that stops a
backtest from lying to you: fills at the next bar's open, taker fees and slippage
on every order, stops tested intrabar, size solved from the stop distance, and a
verdict line that tells you when a result is not worth believing.

## Quickstart

```bash
pip install -r requirements.txt          # numpy, that is all
python run_backtest.py                   # BTC/USDT daily, last year
python run_backtest.py --symbol ETH/USDT --timeframe 4h --start 2022-01-01
python serve_dashboard.py --open         # browse every run at 127.0.0.1:8765
```

`numpy` is the only hard dependency. Data comes from public exchange endpoints
with no API key and no account, and every fetch is cached under `data/cache/`,
so a second run on the same symbol and timeframe needs no network at all.

## What the strategy does

`ema-atr-trend`, evaluated once per bar close, in this order:

1. **Trend filter** — only go long while price is above the slow EMA.
2. **Entry** — the fast EMA crosses above the slow EMA.
3. **Initial stop** — entry price minus `atr_stop_mult` × ATR(14).
4. **Size** — whatever quantity risks `risk_pct` of current equity to that stop.
   A wider stop automatically means a smaller position.
5. **Trail** — the stop ratchets up to `peak high since entry − trail_mult × ATR`
   and never moves down.
6. **Exit** — the fast EMA crosses back below the slow EMA, price falls three ATR
   from entry, or the stop is hit.

Long only by default (spot has no borrow). `--allow-short` enables shorts, with
100% margin and no funding or borrow cost modelled — see the caveats below.

## What the engine assumes

| Behaviour | Choice made | Why |
| --- | --- | --- |
| Signal timing | Decided on the bar close, filled at the **next** bar's open | Filling on the signal bar's own close is the single easiest way to fake a backtest |
| Fees | `fee_bps` per side, both sides | Default 10 bps = Binance spot taker |
| Slippage | `slippage_bps` per side, always against you | Default 5 bps; 30 bps of round-trip friction by default |
| Stops | Tested against the bar's low/high; a gap fills at the **open**, not the stop | Real stops slip on gaps; the test suite pins this down |
| Same-bar stop-out | An entry can be stopped out on the bar it entered | Avoids a free look at the rest of the day |
| Position sizing | Risk-based from the stop distance, capped by `max_position_pct` and available cash | No leverage, no overdrawing, no dust orders under $10 |
| Pyramiding | Rejected and counted in the run notes | Keeps PnL attribution exact |
| Orders on the final bar | Never fill, and it says so | You cannot trade the future |
| Lookahead | Structurally impossible: strategies receive a `BarWindow` truncated at the current bar | You cannot read tomorrow's close because you are not given it |

## Adding your own strategy

Subclass `Strategy`, return orders for the next open, and the engine handles the
rest:

```python
import numpy as np
from quant.broker import Order
from quant.datafeed import Candles
from quant.indicators import donchian
from quant.strategies import BarWindow, Strategy


class ChannelBreakout(Strategy):
    name = "channel-breakout"

    def __init__(self, lookback: int = 55, risk_pct: float = 0.02):
        self.lookback = lookback
        self.risk_pct = risk_pct
        self.windows = {"channel": lookback}   # sets the warmup length

    def prepare(self, candles: Candles) -> dict[str, np.ndarray]:
        upper, lower = donchian(candles.high, candles.low, self.lookback)
        return {"upper": upper, "lower": lower}

    def on_bar(self, w: BarWindow, portfolio: dict) -> list[Order]:
        # Shifted one bar: the channel has to exclude the bar being tested, or
        # today's high is already inside its own breakout level and nothing
        # ever fires. This off-by-one is the classic Donchian mistake.
        upper = w.last(w.ind("upper"), 1)
        lower = w.last(w.ind("lower"), 1)
        close = w.last(w.close)
        atr_now = float(np.mean(w.high[-14:] - w.low[-14:]))
        if portfolio["position"] != "flat":
            return [Order("sell", reason="channel exit")] if close < lower else []
        if np.isnan(upper) or close < upper:
            return []
        return [Order("buy", risk_pct=self.risk_pct, stop=close - 2 * atr_now, reason="breakout")]
```

Then run it with `Engine(BacktestConfig(symbol="BTC/USDT"), ChannelBreakout()).run()`.
Because `on_bar` only ever sees `w.close[-1]` and data before it, lookahead is not
something you have to remember to avoid.

## The dashboard

`python serve_dashboard.py` reads every `results/*.json` and serves a single
self-contained page — no framework, no build step, no CDN, no webfont. A rail on
the left lists the saved runs and the vertical scale; the cards on the right are
the instrument.

* the equity curve as the hero, with buy & hold overlaid and an underwater
  drawdown band beneath it, sharing one time axis
* three labelled vertical scales — both series, strategy only (so a curve that
  trails buy & hold is still legible), and logarithmic
* a hover crosshair with a per-bar readout: close, equity, cumulative return,
  drawdown
* a spec sheet of returns, risk, trade quality and cost metrics
* a fragility strip that names the largest losers and the share of the result they
  explain, so a good-looking run is not mistaken for a robust one
* monthly returns, and the full round-trip trade log (or the raw fill log)
* a **New run** form that re-runs the backtester with different parameters, so
  you can see how fragile a result is to `fast`/`slow`/risk

**Design rule used throughout:** the dashboard tries to make the weakest part of a
result visible first. A pretty curve is still shown, but the page also names the
drawdown, the trade count, the biggest losers and, when available, the share of the
result those losers or winners explain. That is the difference between "looks good"
and "looks good for a reason you can inspect". the CLI prints the result, the verdict,
and, when the result has weak points, a short "Why this might flatter the strategy" list that
ends by pointing at `run_robustness.py --grid small`. The idea is that one pretty run is the
easiest place to fool yourself, so the tool says the skeptical next step out loud instead of
making you remember it.
* a **best setting** helper (`b`) that opens the robustness sweep with a default
  small grid, so the first skeptical question is one keypress away
* a **robustness** helper (`B`) that opens the full robustness sweep with the same
  market, timeframe and settings as the run you are looking at, so skeptical questions
  do not require re-typing the run's parameters

**Light and dark.** The toggle in the top bar switches theme and remembers the
choice in `localStorage`; with nothing stored it follows `prefers-color-scheme`.
The theme is applied by an inline script before first paint, so there is no flash
of the wrong one. Both palettes are held to a 4.5:1 contrast floor, including the
11px labels, and the canvas traces are redrawn from the active theme's tokens on
every switch.

**Motion.** One staggered entrance per run, the equity trace drawing itself in,
a single sweeping headline figure, and hover transitions on cards, run rows,
table rows and month cells. Everything is written under
`@media (prefers-reduced-motion: no-preference)`, so a reader who asks for
reduced motion gets a still page rather than a slower one. The pointer ring — a
lagging accent ring that grows over anything interactive — is additive only: the
real cursor is never hidden, and it stands down on touch devices and under
reduced motion.

**Keyboard.** The chart takes the keyboard as well as the pointer. `←` and `→`
step a bar at a time, `Shift` with either arrow jumps a twentieth of the series
(so a decade of daily bars is twenty presses), `j` and `k` move between saved
runs, and `Esc` clears the marker. The keys are printed under the chart rather
than hidden behind a menu, every one of them is ignored while you are typing in a
field or while the guide is open, and scrubbing announces each bar through a
screen-reader live region while drawing an amber outline so a keyboard reader can
tell it apart from a stray keypress.

**A written guide.** *How to read this* in the rail (or `?` from anywhere) opens
a dialog covering the curve, the three scales, the verdict rulebook, the spec
sheet, the trade log, the keys and what the form does. `Esc` closes it and focus
returns to the button that opened it.

It binds to loopback only. The form posts to the CLI with a validated argument
list — never a shell string — so a mistyped symbol is rejected rather than
executed.

## CLI reference

```
--symbol BTC/USDT     pair (both / and - separators accepted)
--timeframe 1d        1m 5m 15m 1h 4h 1d 1w
--start 2021-01-01    inclusive; default is one year back
--end   YYYY-MM-DD    inclusive; default is now
--provider auto       auto | binance | okx | kraken | coinbase
--csv path.csv        read bars from disk, no network at all
--refresh             ignore the cache and refetch
--cash 10000          starting equity
--fee-bps 10          taker fee per side
--slippage-bps 5      slippage per fill
--risk 0.02           fraction of equity risked per trade
--max-position 1.0    max notional as a fraction of equity
--allow-short         enable shorts (no funding modelled)
--fast 20 --slow 50   EMA lengths
--atr-period 14 --atr-stop-mult 2.5 --trail-mult 3.0
--no-trend-filter     trade crosses without the slow-EMA filter
--json                machine-readable result on stdout
--no-save             print only
```

If a fetch fails, `--provider auto` moves down the list: Binance (global, and the
US mirror, because `api.binance.com` returns HTTP 451 from US IPs), then OKX,
then Kraken, then Coinbase. Coinbase has no weekly candles, so `--timeframe 1w`
falls through to another venue.

## After one backtest, read this before trusting it

A single backtest is one window, one symbol, one parameter set and one history.
That is useful, and it is not enough. The first honest question is not "did it make
money?" but "what would make this result look better than it is?". For this engine
and the shipped strategy, the usual suspects are:

* **Window luck.** The result may depend heavily on the exact start and end dates. Try
  another window; a real effect should survive a few of them.
* **Parameter luck.** Neighbouring settings often give neighbouring results. If the
  headline is carried by one tight corner of the grid, you have a setting, not an edge.
* **Low trade count.** Few trades can look very good or very bad by chance. Around 15
  trades is the floor for a calm read; fewer than that is mostly noise.
* **Cost luck.** Zero fees or zero slippage can flatter a result that would not survive
  them. If you turned costs down, say so.
* **Regime luck.** A trend system can look great in one regime and bad in another. The
  same curve that worked in one market can fail in another.

The default next step is `run_robustness.py --grid small`. It is deliberately quick and
deliberately unkind: it asks whether the result survives other markets, other settings
and random entry timing. If you are deciding whether to care about a number, that is the
command that matters.

## Robustness: is it an edge, or luck?

One backtest tells you what happened in the window you chose. `run_robustness.py`
asks whether the result survives being asked again somewhere else.

```bash
python run_robustness.py                              # 4 markets x 15 settings + luck test
python run_robustness.py --grid small --null-runs 50   # quick version
python run_robustness.py --json --out results/robustness.json
python run_robustness.py --windows 2020-01-01,2022-01-01 # same question, two windows
```

It reports three things, in the order they matter:

**Breadth.** Every setting is run on every market, and the headline is not the
best cell but *how often* the strategy beat buy & hold at all. One cell in fifty
that works is the shape of curve fitting; a plateau of neighbouring settings that
all work is the shape of something real. A cell that closed fewer than
`--min-trades` trades never counts as good, however large its return.

**Concentration.** What share of the aggregate excess comes from the single best
setting. If one cell carries the story, you have a setting, not an edge.

**Luck.** The unkind one. The same engine is re-run on *randomly timed* entries —
same number of trades, holding periods resampled from the real ones, same ATR
sizing, same stop rule, same fees, slippage and fill-on-next-open — 200 times per
market. The `p-value` is the share of those coin-flip schedules that matched or
beat the real signal. The `best of N rnd` column is the selection adjustment: how
good the best of as many random schedules looks, given how many settings were
tried. If the best real cell does not clear it, the winner is selection rather
than signal.

### What it says about the shipped strategy

BTC and ETH, daily and 4-hourly, from 2021: 60 combinations, 200 random
schedules per market, `--seed 7`.

| market | settings | beat b&h | median excess | median Sharpe | trades | luck p |
| --- | --- | --- | --- | --- | --- | --- |
| BTC/USDT 1d | 15 | 0 | −165 pts | 0.58 | 244 | 0.15 |
| ETH/USDT 1d | 15 | 0 | −237 pts | 0.15 | 270 | 0.46 |
| BTC/USDT 4h | 15 | 0 | −118 pts | 0.76 | 1758 | 0.03 |
| ETH/USDT 4h | 15 | 0 | −183 pts | 0.67 | 1767 | 0.04 |

(excess is in percentage points of return against buy & hold over the same window)

Two findings, and they are not the same finding:

* **As a way to beat holding, it fails everywhere.** 0 of 60 combinations beat
  buy & hold, exactly as the caveat below about trend systems in bull markets
  predicts. 52 of 60 still made money; they made less than doing nothing.
* **As a timing signal, the 4-hour version clears the luck test and the daily one
  does not.** On 4h the real signal beat 96–97% of 200 randomly timed schedules
  (p = 0.03 and 0.04), and the best real cell (+127% on BTC 4h) cleared the
  best-of-15-random bar (+44%). On daily, random timing landed within 0.15–0.46
  of it — not distinguishable from luck on this sample.

So: a real, frequency-dependent timing effect that is nowhere near enough to beat
holding the asset. That is a more useful sentence than "+18.5% return", and none
of it is visible from a single backtest. The study is reproducible to the digit
(same data, same `--seed`) and is saved to `results/robustness.json`, which the
dashboard ignores because it is not a run.

## What this project does and does not model

**It models:** fills at the next bar's open, taker fees and slippage on every order,
stops tested against the bar's low/high with gap fills at the open, risk-based sizing
from the stop distance, and a verdict line that calls out the numbers it does not trust.

**It does not model:** leverage limits, borrow or funding costs, liquidation, exchange
outages, partial fills, slippage that varies by volatility or liquidity, portfolio
construction across symbols, correlation or cross-asset risk, and anything that happens
inside a bar. Shorting assumes 100% margin with no funding and no liquidation.

In one sentence: this is a careful replay machine for one narrow idea, not a live trading
system and not a prediction of what will happen next.

## Caveats — read before trusting a number

* **Trend systems look bad in bull markets.** On BTC/USDT daily from 2021 the
  shipped strategy returns roughly +18% against buy & hold's +180%, while only
  being in the market 17% of the time and risking a 7% drawdown. That is the
  trade-off working as designed, not a bug: it is a lower-volatility, lower-return
  path. Compare risk-adjusted figures, not headline returns.
* **Shorting is optimistic.** `--allow-short` assumes 100% margin with no funding,
  no borrow fee and no liquidation. On perpetuals those costs are material.
* **One symbol at a time.** No portfolio construction, correlation or
  cross-asset risk budget.
* **The robustness sweep is in-sample.** It tests breadth — many markets, timeframes
  and settings — but every cell still sees the whole window. A true walk-forward
  split, where parameters are chosen on past data and judged on data they never
  saw, is the honest next step; `quant/robustness.py` is the scaffolding for it.
* **The new multi-window mode is a first cut, not a real out-of-sample study.* **The dashboard now names fragility explicitly.** On a run page it can show the largest
  losers and the share of the result those few trades explain, because a good-looking
  equity curve can still be mostly one or two lucky trades. That is the dashboard's job:
  make the weak part of the story hard to miss.
* **After a single backtest, the runner now says the honest next step out loud.** If the
  result has weak points, it lists them and points at `run_robustness.py --grid small`,
  because one pretty run is the easiest place to fool yourself.* **The roadmap and the intent are now explicit.** This is not a package that claims to be
  done; it is a study toolkit with a stated direction: harder to fool yourself, easier to
  ask the next skeptical question, and clearer about what it does not model.

##### Extended roadmap — the bigger moves, in the order I would do them if this were the whole plan

1. **A real walk-forward study.** Choose parameters on one chunk of history and judge them on
   a later chunk they never saw. That is the first genuinely out-of-sample test and the natural
   next layer above the current in-sample sweep.
2. **More strategy skeletons, not just more parameters for one strategy.** The useful comparison is
   usually "does this family of ideas survive" rather than "can I tune one idea harder".
3. **Better cost realism.** Slippage that can rise with volatility or thin liquidity, and a cleaner
   treatment of the places where fills are less polite than the model assumes.
4. **A confidence/inference layer.** Something that can tell you when a result is interesting but
   still too thin to trust, instead of letting a small win look like a finding.
5. **A small set of reusable study helpers.** So you can ask "where does this break" without wiring
   a new CLI each time.
6. **Easier comparison between ideas.** Side-by-side runs, shared grids, and export paths that make it
   painless to bring a result into a notebook or a second opinion.
7. **A clearer line between exploration and decision.** The toolkit should get better at saying when a
   result is merely suggestive, not when it is worthy of real money.

#### Extended roadmap — the bigger moves, in the order I would do them if this were the whole plan

1. **A real walk-forward study.** Choose parameters on one chunk of history and judge them on
   a later chunk they never saw. That is the first genuinely out-of-sample test and the natural
   next layer above the current in-sample sweep.
2. **More strategy skeletons, not just more parameters for one strategy.** The useful comparison is
   usually "does this family of ideas survive" rather than "can I tune one idea harder".
3. **Better cost realism.** Slippage that can rise with volatility or thin liquidity, and a cleaner
   treatment of the places where fills are less polite than the model assumes.
4. **A confidence/inference layer.** Something that can tell you when a result is interesting but
   still too thin to trust, instead of letting a small win look like a finding.
5. **A small set of reusable study helpers.** So you can ask "where does this break" without wiring
   a new CLI each time.
6. **Easier comparison between ideas.** Side-by-side runs, shared grids, and export paths that make it
   painless to bring a result into a notebook or a second opinion.
7. **A clearer line between exploration and decision.** The toolkit should get better at saying when a
   result is merely suggestive, not when it is worthy of real money.

#

* A real walk-forward study, where parameters are chosen on one chunk of history and judged
  on a later chunk they never saw. That is the first genuinely out-of-sample test and the
  natural next layer above the current in-sample sweep.
* More strategy skeletons, not just more parameters for one strategy. The useful comparison is
  usually "does this family of ideas survive" rather than "can I tune one idea harder".
* Better cost realism: slippage that can rise with volatility or thin liquidity, and a cleaner
  treatment of the places where fills are less polite than the model assumes.
* A confidence/inference layer that can tell you when a result is interesting but still too thin
  to trust, instead of letting a small win look like a finding.
* A small set of reusable study helpers so you can ask "where does this break" without wiring
  a new CLI each time.

** When
  you pass `--windows`, the runner repeats the whole sweep on each start date and then
  folds the results together into one aggregate. If the same story survives more than
  one start date, that is a better sign than a single window. It is still not a clean
  train/test split, because every window is still tested against its own best-looking
  settings; think of it as a stronger sanity check, not a free lunch.
* **Bar data only.** Intrabar paths are unknown, so a bar that touches both the
  stop and a profit target is resolved conservatively but still an assumption.
* **Survivorship of venues.** Data comes from live exchange APIs, so a delisted
  pair simply has no history.

## Roadmap file

The current plan is in `roadmap.md`. If this readme says one thing and `roadmap.md` says another, the roadmap is the more up-to-date statement of intent.

## Layout

```
run_backtest.py            CLI entry point
serve_dashboard.py         dashboard launcher
run_robustness.py          sweep across markets/settings + the random-timing luck test
quant/
  config.py                costs, risk limits, timeframe maths
  datafeed.py              four exchange adapters, pagination, retries, CSV cache
  indicators.py            EMA, SMA, RMA, ATR, RSI, Donchian, rolling std
  broker.py                fills, fees, slippage, stops, sizing, trade records
  strategies/              Strategy base, BarWindow, ema_atr.py
  engine.py                the bar loop and the result object
  metrics.py               Sharpe, Sortino, Calmar, drawdown, trade stats
  robustness.py            breadth sweep, aggregation, random-entry controls
  report.py                console report and the verdict rules
  dashboard/               loopback HTTP server + single-page UI
tests/                     135 tests, all offline (no network needed)
data/cache/                fetched candles, one CSV per provider/symbol/timeframe
results/                   one JSON per run, read by the dashboard
```

## Tests

```bash
python -m pytest -q        # or: python -m unittest discover -s tests -t .
```

No network required: parsers are exercised against fixture payloads, and the
engine tests run on synthetic candles. They cover the parts that are easy to get
quietly wrong — fill timing, the cash-flow identity (`cash == start + sells −
buys − fees` at every bar), exact loss-on-stop for a given risk percentage, gap
fills, rejection of pyramiding and dust orders, indicator warmup, and the fact
that an order on the last bar never fills.

The robustness tests pin the arithmetic that must not flatter the strategy: a
cell is good only if it traded enough *and* beat buy & hold, the random-entry
control takes the same trade count and holding times as the real strategy, and
the selection adjustment reduces to the sample mean when only one setting was
tried.
