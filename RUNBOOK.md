# Runbook: when the paper job or its alerts go quiet

Everything here is paper only. Nothing in this project can place a real order,
so a broken schedule costs you information, never money. Run all commands from
the project root (`~/quant`).

## 1. Four commands that answer most questions

```sh
launchctl print "gui/$(id -u)/com.quant.paperdaily" | grep -E "state|last exit|runs"
tail -20 results/paper/daily_run.log
. .venv/bin/activate && python paper_trade.py --status
tail -3 results/paper/heartbeat.log
```

`paper_trade.py --status` is the single best summary: whether the bot is healthy,
the last run id, and the last recorded failure reason.

## 2. Did it run at all?

- Schedule: `~/Library/LaunchAgents/com.quant.paperdaily.plist`, 07:30 local,
  `RunAtLoad` false. A Mac asleep at 07:30 runs the job when it wakes.
- `results/paper/daily_run.log` appends a `==== date daily paper run ====` block
  per run and closes it with `exit=0` or a non-zero code. A block with no
  closing line means the run was killed mid-flight.
- `results/paper/last_run.json` is the last *saved* run (its `created_at` is the
  honest timestamp to compare against).
- If the job has never run today and yesterday's is the newest, the schedule is
  the problem, not the strategy: reinstall with
  `tasks/install_paper_daily_macos.sh --verify`.

## 3. Did it run and fail?

- A failure writes `results/paper/last_failure.json` **and** pings the failure
  URL. That file is deliberately never cleared on later success, so check its
  `created_at` before trusting it — an old entry is history, not current state.
- Each failure also appends a block to `results/paper/alerts.log` with the
  reason, the local banner result, and `remote:` delivery status.
- Common causes seen in practice: exchange unreachable (DNS), HTTP 451 from a
  venue that geo-blocks this IP (use `--provider okx`; Binance returns 451 here),
  and TLS errors from a missing CA bundle (the wrapper exports `SSL_CERT_FILE`
  from certifi; running `paper_trade.py` without the venv activated can still
  fail this way).

## 4. Did anything notice?

Three independent watchers, in order of reach:

1. **Banner + `alerts.log`** — fires on the same Mac when a run fails.
2. **`com.quant.paperwatch`** — `tasks/check_paper_stale.sh --hours 26`, runs
   08:15, 14:15, 20:15, and warns if no successful run is that old. It skips the
   alert when a newer failure already explained the gap, and repeats at most
   once per run id per 24 hours (`results/paper/.stale_alert_state`).
   Log: `results/paper/watchdog.log`. Dry run: `tasks/check_paper_stale.sh --dry-run`.
3. **Healthchecks.io** — off this Mac, so it still works when the Mac is off,
   asleep or broken. After each run `tasks/send_heartbeat.sh` pings the URL in
   `~/.quant/alert.conf`; a failed run pings `<url>/fail` instead, which alerts
   immediately rather than waiting out the period. Check is 1 day period,
   2 hours grace, email integration.

If the check says paused, it will never alert — resume it. If it says **up** but
`heartbeat.log` shows `FAILED ... (000)`, the ping is not leaving the Mac and
the check is only up because of an older ping; look for a DNS/proxy problem.

```sh
tasks/setup_alert_channel.sh --show      # redacted view of what is configured
tasks/send_heartbeat.sh                  # manual ping, look at the logged code
```

Note that `--show` reports the *local mirror* (Slack/SMTP) only; the healthchecks
email target lives in their account, not in that file.

## 5. The dashboard

`com.quant.dashboard` keeps it up: `.venv/bin/python serve_dashboard.py --port
8765`, restarted on exit, started at login.

```sh
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8765/
curl -s http://127.0.0.1:8765/api/paper | head -c 200
```

If it will not start, something else owns 8765: `lsof -nP -iTCP:8765 -sTCP:LISTEN`.
Reinstall with `tasks/install_dashboard_macos.sh --verify` (or another port:
`--verify 9000`).

## 6. Repair commands

```sh
tasks/install_paper_daily_macos.sh --verify     # reschedule the daily run
tasks/install_paper_watchdog_macos.sh --verify   # reschedule the stale check
tasks/install_dashboard_macos.sh --verify        # reschedule the dashboard
tasks/install_paper_daily_macos.sh --uninstall   # remove any of them
```

`--verify` runs the job once and prints its exit code, so it is the fastest way
to distinguish "misconfigured" from "the job runs but fails".

A launchd job cannot read the project from `~/Documents` (TCC blocks the shell;
exit 126). That is why the project lives at `~/quant`. If it is ever moved back,
expect every scheduled run to fail with `Operation not permitted`.

## 7. Secrets

`~/.quant/alert.conf` (mode 600) holds the heartbeat URL and any Slack/SMTP
target. It lives outside the repo on purpose — never copy it in, never paste it
into a log or an issue. `alerts.log`, `heartbeat.log` and `watchdog.log` record
delivery status but not the URL itself.
