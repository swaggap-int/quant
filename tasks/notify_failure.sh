#!/bin/bash
# Alert when the scheduled daily paper run fails, or has not run at all.
#
#   tasks/notify_failure.sh EXIT_CODE LOG_FILE START_LINE   # called by paper_daily.sh
#   tasks/notify_failure.sh --stale AGE_HOURS RUN_ID        # called by check_paper_stale.sh
#   tasks/notify_failure.sh --test                          # send a sample alert
#
# Delivery is a macOS notification banner plus a block in
# results/paper/alerts.log, and, when a channel is configured, the same text
# sent off-machine. Configure that once with tasks/setup_alert_channel.sh; it
# writes ~/.quant/alert.conf (mode 600), which is sourced here and never lives
# in the repo.
#
# This script never changes the paper job's exit status, and nothing here can
# place an order: the daily job stays paper-only.
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ALERTS="$ROOT/results/paper/alerts.log"
TITLE="Quant paper run failed"
MAXLEN=200
CONF="${QUANT_ALERT_CONF:-$HOME/.quant/alert.conf}"
REMOTE_STATUS=""

if [ -f "$CONF" ]; then
    # shellcheck disable=SC1090
    . "$CONF" 2>/dev/null || true
fi

append_alert() {
    mkdir -p "$(dirname "$ALERTS")" 2>/dev/null || true
    {
        printf '\n==== %s ====\n' "$(date '+%a %m/%d/%Y %H:%M:%S')"
        for line in "$@"; do printf '%s\n' "$line"; done
    } >> "$ALERTS" 2>/dev/null || true
}

escape_as() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

json_text() {
    printf '%s' "$1" | tr '\n\t' '  ' | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

# 0 when osascript accepted the banner; its exit code otherwise.
send_banner() {
    local code=0
    /usr/bin/osascript \
        -e "display notification \"$(escape_as "$2")\" with title \"$(escape_as "$1")\" sound name \"Basso\"" \
        >/dev/null 2>&1 || code=$?
    return "$code"
}

# Off-machine copy: a Slack-compatible incoming webhook and/or SMTP. Both are
# optional and neither can fail the caller; the result is reported and logged.
send_remote() {
    local title="$1" body="$2" code payload
    REMOTE_STATUS=""

    if [ -n "${QUANT_ALERT_WEBHOOK_URL:-}" ]; then
        payload="{\"text\": \"$(json_text "$title: $body")\"}"
        code="$(curl -sS -m 15 -o /dev/null -w '%{http_code}' -X POST \
            -H 'Content-Type: application/json' --data "$payload" \
            "$QUANT_ALERT_WEBHOOK_URL" 2>/dev/null || true)"
        case "$code" in
            2*) REMOTE_STATUS="webhook ok ($code)" ;;
            *) REMOTE_STATUS="webhook failed (${code:-no response})" ;;
        esac
    fi

    if [ -n "${QUANT_ALERT_SMTP_URL:-}" ] && [ -n "${QUANT_ALERT_SMTP_USER:-}" ] && [ -n "${QUANT_ALERT_EMAIL_TO:-}" ]; then
        if {
            printf 'To: %s\nFrom: %s\nSubject: %s\nContent-Type: text/plain; charset=UTF-8\n\n%s\n' \
                "$QUANT_ALERT_EMAIL_TO" "$QUANT_ALERT_SMTP_USER" "$title" "$body"
        } | curl -sS -m 20 --url "$QUANT_ALERT_SMTP_URL" \
            --mail-from "$QUANT_ALERT_SMTP_USER" \
            --mail-rcpt "$QUANT_ALERT_EMAIL_TO" \
            --user "$QUANT_ALERT_SMTP_USER:${QUANT_ALERT_SMTP_PASS:-}" \
            -T - >/dev/null 2>&1; then
            REMOTE_STATUS="${REMOTE_STATUS:+$REMOTE_STATUS; }email ok"
        else
            REMOTE_STATUS="${REMOTE_STATUS:+$REMOTE_STATUS; }email failed"
        fi
    fi

    [ -z "$REMOTE_STATUS" ] && REMOTE_STATUS="not configured (tasks/setup_alert_channel.sh)"
    return 0
}

truncate_text() {
    local s="$1"
    if [ "${#s}" -gt "$MAXLEN" ]; then printf '%s...' "${s:0:$MAXLEN}"; else printf '%s' "$s"; fi
}

if [ "${1:-}" = "--stale" ]; then
    age="${2:-none}"
    run="${3:-none}"
    if [ "$age" = "none" ] || [ "$age" = "-1" ]; then
        body="No paper run has ever been recorded. Check the daily job: tasks/install_paper_daily_macos.sh --verify"
    else
        body="Last paper run was ${age}h ago (run $run). The daily job may not be running; check tasks/install_paper_daily_macos.sh --verify."
    fi
    body="$(truncate_text "$body")"
    send_banner "Quant paper run is stale" "$body"
    rc=$?
    send_remote "Quant paper run is stale" "$body"
    append_alert "kind: stale run (daily job did not run)" \
        "last_run_age_hours: $age" \
        "run_id: $run" \
        "notify: osascript exit $rc" \
        "remote: $REMOTE_STATUS" \
        "check: launchctl print gui/$(id -u)/com.quant.paperdaily"
    echo "stale alert sent (osascript exit $rc, remote: $REMOTE_STATUS); logged to $ALERTS"
    exit 0
fi

if [ "${1:-}" = "--test" ]; then
    body="Test alert. A failed or missed daily paper run reports here; the details go to results/paper/alerts.log."
    send_banner "$TITLE (test)" "$body"
    rc=$?
    send_remote "$TITLE (test)" "$body"
    append_alert "kind: test (no run failed)" "notify: osascript exit $rc" "remote: $REMOTE_STATUS" "body: $body"
    echo "test alert sent (osascript exit $rc, remote: $REMOTE_STATUS); logged to $ALERTS"
    exit 0
fi

if [ "$#" -lt 1 ]; then
    echo "usage: $(basename "$0") EXIT_CODE LOG_FILE START_LINE | --stale AGE RUN_ID | --test" >&2
    exit 2
fi

RC="$1"
LOG="${2:-}"
START="${3:-0}"
case "$START" in ''|*[!0-9]*) START=0 ;; esac

reason=""
context=""
if [ -n "$LOG" ] && [ -f "$LOG" ]; then
    section="$(tail -n +"$((START + 1))" "$LOG" 2>/dev/null)"
    reason="$(printf '%s\n' "$section" \
        | grep -iE 'error|traceback|failed|refused|no provider|timed out' \
        | tail -1 | sed 's/^[[:space:]]*//')"
    context="$(printf '%s\n' "$section" | grep -v '^[[:space:]]*$' | tail -6)"
fi

# paper_trade.py records a structured reason, but an old last_failure.json is
# never cleared on success, so only trust one written in the last 30 minutes.
lf="$ROOT/results/paper/last_failure.json"
if [ -z "$reason" ] && [ -f "$lf" ]; then
    created="$(sed -n 's/.*"created_at": "\([^"]*\)".*/\1/p' "$lf" | head -1)"
    if [ -n "$created" ]; then
        base="${created%%+*}"
        base="${base%%.*}"
        stamp="$(date -j -f "%Y-%m-%dT%H:%M:%S" "$base" +%s 2>/dev/null || true)"
        now="$(date +%s)"
        if [ -n "$stamp" ] && [ $((now - stamp)) -le 1800 ]; then
            reason="$(sed -n 's/.*"reason": "\(.*\)".*/\1/p' "$lf" | head -1)"
        fi
    fi
fi
[ -z "$reason" ] && reason="exit $RC, no reason captured"

body="$(truncate_text "exit $RC - $reason")"
send_banner "$TITLE" "$body"
notify_rc=$?
send_remote "$TITLE" "$body"

append_alert "kind: daily run failure" \
    "exit: $RC" \
    "reason: $reason" \
    "notify: osascript exit $notify_rc" \
    "remote: $REMOTE_STATUS" \
    "log: ${LOG:-none}" \
    "context:" \
    "${context:-(none)}"

exit 0
