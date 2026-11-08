# Paper launch discipline report (Milestone 7)

Paper-only. No live venue, no live money.

## Reviews

- **Live path** (`quant/live_runner.py`): one pass over bars, closed-bar-only
  evaluation, `Engine` fill order (`process_open` → excursion → stop → mark
  at close → strategy → queue for next open). Strategy exceptions are caught,
  logged as `error` events, counted in `strategy_errors`, and do not kill the
  run. Policy trips are recorded in `notes` + `policy_trips`, never silent.
- **Adapter** (`quant/exchange.py` on `quant/paper.py` on `Broker`): same fill
  model as the backtester by construction (next-open, fees, slippage, stops,
  sizing). Order ids are locally issued (`paper-N`), `client_id` reissue is
  idempotent, cancel-of-filled and unknown ids raise explicitly, partials are
  recorded only via `record_partial_fill`, rate limits raise explicitly, and
  `check_consistency`/`startup_reconcile` report instead of silently fixing.
- **Risk policy** (`LiveRunPolicy`): max-position, exposure guard, daily-loss
  limit, per-bar/per-run rate limits, duplicate protection, kill switch. All
  enforced before `submit`; every block is a `policy_trips` entry + note.

## Kill switch and restart

- Tested in `tests/test_paper_execution.py`, `tests/test_live_runner_contract.py`,
  `tests/test_paper_launch.py`: killed runner/venue emits no fills; restart
  from `quant/state_store.py` snapshot resumes without duplicate fills;
  crash-mid-bar replay is idempotent (`ts <= last ts` is a no-op);
  lost-response reissue with the same `client_id` returns the same order id.

## Defined paper period

60-bar alternating-entry run, scripted buys/sells every 10 bars:

- live fills: 12, paper fills: 12, parity: true
- Machine-readable: `results/paper_launch.json` (`mode: paper_launch_check`).

## Divergence reasons

None observed in the defined period. Expected benign sources, documented in
the report payload:

- Same fill model, warmup, and closed-bar rule in both paths.
- Final-bar pending orders never fill in either path.
- Policy-trip bookkeeping may timestamp one bar apart without changing fills.
- Any future mismatch must surface via `policy_trips`, `check_consistency`,
  or ops-view alerts — not silence.
