#!/bin/bash
# Tell an off-machine watcher that the daily paper job ran.
#
#   tasks/send_heartbeat.sh          # success ping (after a clean run)
#   tasks/send_heartbeat.sh fail     # failure ping, so the watcher can alert at once
#
# The URL comes from ~/.quant/alert.conf (QUANT_HEARTBEAT_URL, set with
# tasks/setup_alert_channel.sh --heartbeat URL). The watcher lives somewhere
# other than this Mac, so it keeps alerting even if the Mac is off, asleep or
# broken - this script is the only part that needs the Mac to be alive.
#
# With no URL configured it does nothing, so the daily job never depends on it.
# It always exits 0 and never changes the paper job's exit status.
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONF="${QUANT_ALERT_CONF:-$HOME/.quant/alert.conf}"
LOG="$ROOT/results/paper/heartbeat.log"
MODE="${1:-ok}"

if [ -f "$CONF" ]; then
    # shellcheck disable=SC1090
    . "$CONF" 2>/dev/null || true
fi
URL="${QUANT_HEARTBEAT_URL:-}"

log() {
    mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
    printf '%s %s\n' "$(date '+%F %T')" "$1" >> "$LOG" 2>/dev/null || true
}

if [ -z "$URL" ]; then
    log "skip: no heartbeat URL configured (tasks/setup_alert_channel.sh --heartbeat URL)"
    exit 0
fi

target="$URL"
[ "$MODE" != "ok" ] && target="${URL%/}/fail"

code="$(curl -sS -m 10 -o /dev/null -w '%{http_code}' --retry 2 --retry-delay 5 \
    "$target" 2>/dev/null || true)"

case "$code" in
    2*) log "sent $MODE ping ($code)" ;;
    *) log "FAILED $MODE ping (${code:-no response}) $target" ;;
esac

exit 0
