# Live bot milestone checklist

Current decision (locked)
- Scope: BTCUSDT 1d, ema-atr-trend, closed-bar only, paper-only.
- Milestones 1–7 complete; Milestone 8 (live money) stays closed by design.

Milestone 0 — Lock scope
- [x] Choose the first target symbol and timeframe for paper/simulated live
  - BTCUSDT, 1d (contract tests use TEST/USDT 1d synthetics)
- [x] Choose the first strategy to run automatically
  - ema-atr-trend (`EmaAtrTrend`; scripted harness only for tests)
- [x] Decide if the live loop is "closed-bar only" from day one
  - Yes: closed-bar only; `BarClosedPolicy` forbids open-bar evaluation by default
- [x] Confirm paper-only for this milestone
  - Paper-only: replay + paper venue, no exchange connection, no live money
- [x] Write down the smallest behavior we agree counts as "done" for milestone 1
  - Done = shared runner contract, both-paths integration test, no-future-bars test (all passing)

Milestone 1 — Live-vs-backtest contract
- [x] Define a shared runner contract so the same strategy logic can be used in backtest and live
- [x] Separate signal logic, execution model, and runtime
- [x] State the live evaluation rule explicitly:
  - decide on close
  - fill at next open
  - no future bars in the window
- [x] Add a fixture or harness that can run the strategy two ways:
  - historical backtest path
  - simulated live path
- [x] Add an integration test proving both paths can use the same strategy code
- [x] Add a test that the live path cannot see future bars

Milestone 2 — Live data/clock adapter and bar-closed detection
- [x] Define a live data adapter interface:
  - latest candle
  - bar-closed detection
  - missing data / gap handling
- [x] Add a simulated live clock/align layer:
  - bar boundary detection
  - partial/current bar behavior at decision time
- [x] Make the simulated live path able to run on a stream of bars instead of only a prebuilt array
- [x] Make the simulated live run emit a result shape the dashboard/verifier can read

Milestone 3 — Paper execution layer
- [x] Add a paper execution adapter using the same fill model as the backtester:
  - next-open fills
  - fees and slippage
  - stops
  - sizing from stop distance
- [x] Add paper account state:
  - cash
  - positions
  - open orders
  - pending stops
  - trade log
- [x] Add runtime policy before order send:
  - max position
  - exposure guard
  - daily loss limit
  - duplicate order protection
  - rate limiting
- [x] Add a kill switch that can stop trading immediately
- [x] Test that the kill switch and policy guards actually trip

Milestone 4 — Real exchange paper adapter
- [x] Define the exchange adapter contract:
  - connect
  - candles/latest price
  - place order
  - cancel order
  - account state
  - order state
- [x] Implement one exchange paper adapter only after milestone 3 is real
- [x] Add retry/backoff for transient errors
- [x] Add safe handling for:
  - network failure
  - auth/token refresh
  - order state mismatch
  - partial fills if supported
  - rate limits
- [x] Make mismatches explicit, not silent

Milestone 5 — State, persistence, restart safety
- [x] Persist last processed bar
- [x] Persist open positions and open orders
- [x] Track locally issued order ids
- [x] Add idempotency so restarts do not blindly reissue orders
- [x] Add startup reconciliation between local state and exchange/paper account where possible
- [x] Define and test recovery behavior for:
  - crash mid-bar
  - lost order response
  - local/remote mismatch

Milestone 6 — Observability
- [x] Structured logs for decisions, orders, fills, guard trips, errors, retries
- [x] measurable traces for bars processed, orders placed/filled/rejected, downtime
- [x] alerts for repeated errors, guard trips, state mismatch, process health
- [x] a live paper ops view separate from the historical dashboard

Milestone 7 — Paper launch discipline
- [x] Code review of the live path
- [x] Review of the adapter
- [x] Review of the risk policy
- [x] Test kill switch
- [x] Test restart behavior
- [x] Run paper for a defined period
- [x] Compare paper fills and behavior to simulated live replay
- [x] Document divergence reasons if any

Milestone 8 — Live-money gate
- [ ] Do not open this milestone until milestone 7 has run long enough to trust ops
- [ ] Require adapter reality check
- [ ] Require enforced risk policy
- [ ] Require proven restart/alert/kill-switch behavior
- [ ] If ever opened, start tiny, strict, and easy to stop
- [ ] Treat this as a separate decision, not an automatic next step

First step from here
- Pick the symbol/timeframe/strategy for milestone 0
- Then write the shared runner contract and the integration test for milestone 1

Milestone gate reached
- Milestone 1 complete: shared live-vs-backtest contract, simulated-live replay, policy guard, kill switch, and integration tests.
- Next: Milestone 2 — turn the simulator into a stream-friendly live loop with explicit bar-closed detection.
