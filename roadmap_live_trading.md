# Live trading roadmap

This is the path from "careful backtester + sweep + dashboard" to "automated paper trading bot with a safe live path later."

Goal
- Being able to run one strategy automatically against live market data, in paper mode first, with execution safety and observability, and only later a controlled live-money path.
- This is a platform effort, not just another backtest. The interesting risk is operational: ordering, state, restarts, errors, rate limits, and making sure what runs is what we think runs.

Non-goal for now
- Live money.
- Multi-strategy portfolio optimization.
- Any promise that the current strategy is an edge. The robustness study already says it is not enough to beat buy & hold in this study; this roadmap is about building the machinery to run something safely, not to pretend a number is good.

Current assets we can reuse
- Engine with next-open fills, fees, slippage, stops, sizing, trade records, last-bar rejection.
- Robustness sweep and frozen-read chain you already hardened.
- Dashboard read path and the verification/integrity work around results.
- The existing strategy skeleton and BarWindow write-once-bar semantics.

Phase 0 — Decide what "automated" means here
- Pick one concrete first target:
  - paper trade one symbol on one timeframe using the existing strategy, with the same cost and risk model as the backtester
  - or paper trade a small fixed list
- Decide the smallest useful bot behavior:
  - connect to live candle feed or polling
  - evaluate on closed bars only
  - generate orders for next open
  - log everything
  - never trade real money in phase 1
- Decision needed: do we want exchange API integration now, or start with a simulated live feed first?
  - Recommended: start with a simulated live feed and a clear exchange adapter interface, then plug a real exchange in for paper trading once the loop is honest.

Phase 1 — Live vs backtest contract
- Define a single shared "strategy runner" contract so backtest and live use the same decision function.
- Separate three concerns clearly:
  - signal logic: BarWindow -> orders
  - execution model: orders -> fills, fees, slippage, stops
  - runtime: data, clock, state, restart, logging
- Make the live loop use only information available at decision time. Live bars must not act on the current open/decision bar the way a backtest can cheat.
- Add an explicit "live evaluation rule" that mirrors the backtest rule:
  - decide on close
  - fill at next open
  - no future bars in the window
- Write a small integration test that runs the same strategy through:
  - a historical backtest path
  - a simulated live path
  - assert they can use the same strategy code
- This phase is mostly architecture and test discipline, not exchange integration.

Phase 2 — Simulated live data and clock
- Build a live-data adapter interface:
  - fetch latest candle
  - know when a bar is closed
  - handle missing data, reorgs, gaps, clock skew
- Add a simulated live mode:
  - replay the existing historical feed bar-by-bar through the live loop
  - this gives a "live-like" run that can be compared to the backtest
- Add a clock/align layer:
  - bar boundary detection
  - sleep/wake policy
  - clear behavior for the partial bar at the moment of evaluation
- Make the simulated live run emit the same result shape as the backtest so the dashboard and verifier can read it.

Phase 3 — Paper execution layer
- Add a paper execution adapter with the same fill model as the backtester:
  - next-open fills
  - fees and slippage
  - stop logic
  - sizing from stop distance
- The paper adapter must be deterministic enough to compare against the backtest when replayed on the same data.
- Add paper account state:
  - cash
  - positions
  - open orders
  - pending stops
  - trade log
- Add a runtime policy layer before any order is sent:
  - max position
  - max leverage-like exposure if applicable
  - daily loss limit
  - per-symbol guards
  - duplicate order protection
  - order rate limiting
- Add a kill switch:
  - stop trading immediately
  - optionally flatten with a defined policy
  - must be testable

Phase 4 — Real exchange adapter for paper trading
- Define the exchange adapter contract:
  - connect
  - fetch candles / latest price
  - place order
  - cancel order
  - read account state
  - read order state
- Start with one exchange in paper mode only.
- Add retry and backoff for transient errors.
- Add safe handling for:
  - network failures
  - auth/token refresh
  - order state mismatch
  - partial fills if the exchange supports them
  - rate limits
- Do not allow the live path to depend on assumptions the backtester never made.
- If the exchange does not match the model, the adapter must say so explicitly rather than silently papering over it.

Phase 5 — State, persistence, and restart safety
- Persist runtime state so a restart does not duplicate trades or lose position context:
  - last processed bar
  - open positions
  - open orders
  - locally tracked order ids
- Add idempotency:
  - re-running after a crash should not reissue the same order blindly
- Add reconciliation:
  - on startup, compare local state to the exchange/paper account where possible
  - flag mismatch instead of guessing
- Add a clear recovery policy:
  - what happens if the bot crashed mid-bar
  - what happens if an order was sent but the response was lost
  - what happens if local and remote state disagree

Phase 6 — Observability and guardrails
- Structured logging for:
  - every decision
  - every order sent
  - every fill or state change
  - every guard trip
  - every error and retry
- Metrics or at least measurable traces for:
  - bars processed
  - orders placed
  - orders filled
  - rejections
  - downtime
- Alerts for:
  - repeated errors
  - guard trips
  - account state mismatch
  - process health
- A dashboard or console view for the live paper runner that is separate from the historical dashboard, because live ops have different questions:
  - is it running
  - what did it just do
  - what is exposed right now
  - what would happen if it keeps going

Phase 7 — Policy and launch discipline for paper
- Before turning the bot on even in paper:
  - code review of the live path
  - review of the adapter
  - review of the risk policy
  - test of the kill switch
  - test of restart behavior
- Run paper for a defined period and compare:
  - paper fills vs expected fills
  - paper behavior vs simulated live replay
  - divergence reasons if any
- Keep paper trading separate from any live-money decision.

Phase 8 — Live-money path only after the above
- Only consider live money after:
  - paper trading runs safely for long enough to trust the ops
  - the adapter matches reality closely enough
  - the risk policy is real and enforced
  - restart, alert, and kill-switch behavior is proven
- If we ever get there, the first live step should be:
  - tiny size
  - strict limits
  - easy stop
  - clear review after a short period
- This roadmap does not promise live trading. It makes live trading possible only after the boring safety stuff is real.

Recommended order
1. Phase 0 decision first.
2. Phase 1 and Phase 2 together, because the live loop is meaningless without a data/clock contract.
3. Phase 3 before any real exchange integration.
4. Phase 4 only after paper execution is honest.
5. Phase 5 and Phase 6 in parallel once there is something persistent to observe.
6. Phase 7 before any thought of Phase 8.

Chosen starting point for this repo
- Milestone path: roadmap_live_trading.md
- Concrete checklist: milestones_live_bot.md
- First milestone: simulated-live replay, one symbol, one strategy, paper only.
- First code work: live-vs-backtest contract + integration test.

What "done" looks like for the first milestone
- One strategy can run in a simulated-live replay and produce the same kind of result the backtester and dashboard already understand.
- A paper execution layer exists with the same fill/slip/stop/sizing model.
- The bot has guardrails, logging, restart safety, and a kill switch.
- A real exchange paper adapter is pluggable but not required to prove the loop works.

What would block this
- Trying to paper-trade before the live vs backtest contract is clean.
- Trying live money before restart safety and observability exist.
- Letting the adapter hide model mismatches instead of surfacing them.
- Treating the robustness result as if it were positive; the roadmap is about safe automation, not about rescuing this strategy's numbers.

If you want, I can turn this into a milestone checklist and start with Phase 0 as a concrete decision plus the smallest Phase 1 contract test.
