#!/bin/bash
# Warn when the daily paper run has not happened at all.
#
#   tasks/check_paper_stale.sh                 # alert if the last run is older than 26h
#   tasks/check_paper_stale.sh --hours 30      # custom threshold
#   tasks/check_paper_stale.sh --dry-run       # report the decision, send nothing
#
# Installed as com.quant.paperwatch by tasks/install_paper_watchdog_macos.sh,
# which runs it a few times a day. Paper only: it reads the saved run state and
# never contacts the exchange. A run that failed is left to paper_daily.sh's own
# alert, so you are not told twice about one bad run.
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
LOG="$ROOT/results/paper/watchdog.log"
STATE="$ROOT/results/paper/.stale_alert_state"
HOURS=26
DRY=0

usage() {
    echo "usage: $(basename "$0") [--hours N] [--dry-run]" >&2
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --hours) shift; HOURS="${1:-}" ;;
        --hours=*) HOURS="${1#*=}" ;;
        --dry-run) DRY=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unexpected argument: $1" >&2; usage; exit 2 ;;
    esac
    shift
done

case "$HOURS" in
    ''|*[!0-9.]*) echo "hours must be a number: $HOURS" >&2; exit 2 ;;
esac

log_line() {
    mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
    printf '%s %s\n' "$(date '+%a %m/%d/%Y %H:%M:%S')" "$1" >> "$LOG" 2>/dev/null || true
    [ -t 1 ] && echo "$1"
}

if [ ! -x "$PY" ]; then
    msg="error: $PY missing, cannot read paper state"
    log_line "$msg"
    [ "$DRY" -eq 1 ] || "$ROOT/tasks/notify_failure.sh" --stale none "unknown" >/dev/null 2>&1 || true
    exit 1
fi

DECISION="$("$PY" - "$ROOT" "$HOURS" <<'PY'
import json
import sys
import time
from datetime import datetime

root, hours = sys.argv[1], float(sys.argv[2])


def read(name):
    try:
        with open(f"{root}/results/paper/{name}", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def stamp(blob):
    if not isinstance(blob, dict):
        return None
    try:
        return datetime.fromisoformat(str(blob["created_at"])).timestamp()
    except (KeyError, TypeError, ValueError):
        return None


last_run = read("last_run.json")
last_failure = read("last_failure.json")
run_ts = stamp(last_run)
fail_ts = stamp(last_failure)
now = time.time()

if run_ts is None:
    print("state=stale")
    print("age_hours=none")
    print("run_id=none")
elif fail_ts is not None and fail_ts > run_ts and (now - fail_ts) / 3600.0 <= hours:
    print("state=failed_recent")
    print(f"age_hours={(now - run_ts) / 3600.0:.1f}")
    print(f"run_id={last_run.get('run_id', 'none')}")
elif (now - run_ts) / 3600.0 > hours:
    print("state=stale")
    print(f"age_hours={(now - run_ts) / 3600.0:.1f}")
    print(f"run_id={last_run.get('run_id', 'none')}")
else:
    print("state=ok")
    print(f"age_hours={(now - run_ts) / 3600.0:.1f}")
    print(f"run_id={last_run.get('run_id', 'none')}")
PY
)"

state=""; age=""; run_id=""
while IFS='=' read -r key value; do
    case "$key" in
        state) state="$value" ;;
        age_hours) age="$value" ;;
        run_id) run_id="$value" ;;
    esac
done <<EOF
$DECISION
EOF

case "$state" in
    ok)
        log_line "ok: last run ${age}h ago (threshold ${HOURS}h), run ${run_id}"
        ;;
    failed_recent)
        log_line "recent failure already alerted: last run ${age}h ago, run ${run_id}"
        ;;
    stale)
        already=0
        if [ -f "$STATE" ]; then
            read -r prior_epoch prior_run < "$STATE" 2>/dev/null || true
            case "${prior_epoch:-}" in ''|*[!0-9]*) prior_epoch=0 ;; esac
            if [ "${prior_run:-}" = "$run_id" ] && [ $(( $(date +%s) - prior_epoch )) -lt 86400 ]; then
                already=1
            fi
        fi
        if [ "$DRY" -eq 1 ]; then
            log_line "STALE (dry run): last run ${age}h ago (threshold ${HOURS}h), run ${run_id}, no alert sent"
        elif [ "$already" -eq 1 ]; then
            log_line "STALE, already alerted today: last run ${age}h ago, run ${run_id}"
        else
            "$ROOT/tasks/notify_failure.sh" --stale "$age" "$run_id" >/dev/null 2>&1 || true
            printf '%s %s\n' "$(date +%s)" "$run_id" > "$STATE" 2>/dev/null || true
            log_line "STALE: alerted, last run ${age}h ago (threshold ${HOURS}h), run ${run_id}"
        fi
        ;;
    *)
        log_line "error: could not read paper state (${DECISION//$'\n'/ })"
        exit 1
        ;;
esac

exit 0
