#!/bin/sh
# Daily paper-trading run for this project (macOS equivalent of the old
# tasks/paper_daily.cmd). Paper only: it never sends a real order.
#
# Every run appends to results/paper/daily_run.log. Install it as a launchd
# job with tasks/install_paper_daily_macos.sh; run it by hand with:
#   tasks/paper_daily.sh
set -u

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd) || exit 1
cd "$ROOT" || exit 1

# launchd starts jobs with a minimal PATH, so prefer the project virtualenv.
if [ -x "$ROOT/.venv/bin/python" ]; then
    PY="$ROOT/.venv/bin/python"
else
    PY=$(command -v python3 || true)
fi
if [ -z "${PY:-}" ]; then
    echo "paper_daily: no .venv/bin/python and no python3 on PATH" >&2
    exit 127
fi

# This python.org build ships no CA bundle, so point OpenSSL at certifi's copy
# (the same SSL_CERT_FILE the virtualenv's activate script exports).
CERT=$(ls "$ROOT"/.venv/lib/python3.*/site-packages/certifi/cacert.pem 2>/dev/null | head -1)
if [ -n "${CERT:-}" ]; then
    SSL_CERT_FILE="$CERT"
    export SSL_CERT_FILE
fi

mkdir -p "$ROOT/results/paper" || exit 1
LOG="$ROOT/results/paper/daily_run.log"
# Where this run's output starts, so a failure alert can quote it.
START_LINE=0
[ -f "$LOG" ] && START_LINE=$(wc -l < "$LOG" | tr -d ' ')

{
    echo
    echo "==== $(date '+%a %m/%d/%Y %H:%M:%S') daily paper run ===="
} >> "$LOG"

# Same fixed window and venue as the Windows task, so runs stay comparable.
"$PY" paper_trade.py --symbol BTC/USDT --timeframe 1d --provider okx --bars 500 \
    --fast 20 --slow 50 --atr-period 14 --atr-stop-mult 2.5 --trail-mult 3.0 \
    --refresh --prune-older-than-days 30 --quiet >> "$LOG" 2>&1
RC=$?

echo "==== $(date '+%a %m/%d/%Y %H:%M:%S') exit=$RC ====" >> "$LOG"

# Tell me about failures: a banner plus a block in results/paper/alerts.log.
# This never changes RC, and it cannot place an order.
if [ "$RC" -ne 0 ]; then
    "$ROOT/tasks/notify_failure.sh" "$RC" "$LOG" "$START_LINE" || true
fi

# Heartbeat for the off-machine watcher, so a Mac that dies goes noticed.
# No-op unless a heartbeat URL is configured; it never changes RC.
if [ "$RC" -eq 0 ]; then
    "$ROOT/tasks/send_heartbeat.sh" || true
else
    "$ROOT/tasks/send_heartbeat.sh" fail || true
fi

exit "$RC"
