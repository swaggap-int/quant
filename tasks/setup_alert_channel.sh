#!/bin/bash
# Point the paper alerts at a channel that reaches you off this Mac, and
# optionally at an off-machine heartbeat watcher.
#
#   tasks/setup_alert_channel.sh --slack https://hooks.slack.com/services/...
#   tasks/setup_alert_channel.sh --email you@example.com [--from you@example.com]
#   tasks/setup_alert_channel.sh --heartbeat https://hc-ping.com/xxxxxxxx-...
#   tasks/setup_alert_channel.sh --show      # what is configured now
#   tasks/setup_alert_channel.sh --test      # send a test alert through it
#   tasks/setup_alert_channel.sh --clear     # remove the configuration
#
# Everything is written to ~/.quant/alert.conf with mode 600, outside this
# repository, and sourced by tasks/notify_failure.sh (alerts) and
# tasks/send_heartbeat.sh (the ping). Each option changes only its own field,
# so you can add a heartbeat later without losing the alert channel.
set -u

CONF="$HOME/.quant/alert.conf"
MODE=""
SMTP_DEFAULT="${QUANT_ALERT_SMTP_URL:-smtps://smtp.gmail.com:465}"

# Keep whatever is already configured; a --flag overrides just its own value.
W=""; S=""; U=""; P=""; T=""; HB=""
if [ -f "$CONF" ]; then
    # shellcheck disable=SC1090
    . "$CONF" 2>/dev/null || true
fi
W="${QUANT_ALERT_WEBHOOK_URL:-}"
S="${QUANT_ALERT_SMTP_URL:-}"
U="${QUANT_ALERT_SMTP_USER:-}"
P="${QUANT_ALERT_SMTP_PASS:-}"
T="${QUANT_ALERT_EMAIL_TO:-}"
HB="${QUANT_HEARTBEAT_URL:-}"

usage() {
    cat >&2 <<'MSG'
usage: setup_alert_channel.sh --slack WEBHOOK_URL
       setup_alert_channel.sh --email you@example.com [--from you@example.com] [--smtp smtps://host:465]
       setup_alert_channel.sh --heartbeat https://watcher.example/ping/UUID
       setup_alert_channel.sh --show | --test | --clear
MSG
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --slack) MODE="slack"; W="${2:-}"; shift ;;
        --email) MODE="email"; T="${2:-}"; shift ;;
        --from) U="${2:-}"; shift ;;
        --smtp) S="${2:-}"; shift ;;
        --heartbeat) MODE="heartbeat"; HB="${2:-}"; shift ;;
        --show) MODE="show" ;;
        --test) MODE="test" ;;
        --clear) MODE="clear" ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unexpected argument: $1" >&2; usage; exit 2 ;;
    esac
    shift
done

write_conf() {
    mkdir -p "$(dirname "$CONF")" || exit 1
    chmod 700 "$(dirname "$CONF")" 2>/dev/null || true
    umask 077
    {
        echo "# Off-machine targets for the quant paper jobs."
        echo "# Read by tasks/notify_failure.sh and tasks/send_heartbeat.sh."
        echo "# Keep private; this file is not part of the repository."
        printf 'QUANT_ALERT_WEBHOOK_URL=%q\n' "$1"
        printf 'QUANT_ALERT_SMTP_URL=%q\n' "$2"
        printf 'QUANT_ALERT_SMTP_USER=%q\n' "$3"
        printf 'QUANT_ALERT_SMTP_PASS=%q\n' "$4"
        printf 'QUANT_ALERT_EMAIL_TO=%q\n' "$5"
        printf 'QUANT_HEARTBEAT_URL=%q\n' "$6"
    } > "$CONF"
    chmod 600 "$CONF"
}

show_conf() {
    if [ ! -f "$CONF" ]; then
        echo "no channel configured ($CONF does not exist)"
        echo "set one with: $(basename "$0") --slack WEBHOOK_URL  |  --email you@example.com"
        return 0
    fi
    echo "config    $CONF"
    if [ -n "$W" ]; then
        echo "webhook   configured (**${W: -4})"
    else
        echo "webhook   not configured"
    fi
    if [ -n "$S" ] && [ -n "$T" ]; then
        echo "email     ${U:-?} -> ${T} via ${S} (password stored)"
    else
        echo "email     not configured"
    fi
    if [ -n "$HB" ]; then
        echo "heartbeat configured (**${HB: -4})"
    else
        echo "heartbeat not configured (nothing outside this Mac watches the job)"
    fi
}

case "$MODE" in
    slack)
        case "$W" in
            https://*) ;;
            *) echo "expected an https webhook URL, got: ${W:-nothing}" >&2; exit 2 ;;
        esac
        write_conf "$W" "$S" "$U" "$P" "$T" "$HB"
        echo "webhook saved to $CONF (mode 600)"
        echo "next: $(basename "$0") --test"
        ;;
    email)
        [ -n "$T" ] || { echo "missing address: --email you@example.com" >&2; exit 2; }
        [ -n "$U" ] || U="$T"
        printf 'SMTP host [%s]: ' "$SMTP_DEFAULT"
        read -r answer || true
        [ -n "${answer:-}" ] && S="$answer"
        [ -n "$S" ] || S="$SMTP_DEFAULT"
        printf 'SMTP password for %s (an app password, not your normal one): ' "$U"
        read -r -s pass || true
        echo
        [ -n "${pass:-}" ] || { echo "no password given; nothing saved" >&2; exit 2; }
        P="$pass"
        write_conf "$W" "$S" "$U" "$P" "$T" "$HB"
        echo "email saved to $CONF (mode 600)"
        echo "next: $(basename "$0") --test"
        ;;
    heartbeat)
        case "$HB" in
            https://*|http://127.0.0.1*) ;;
            *) echo "expected an https ping URL, got: ${HB:-nothing}" >&2; exit 2 ;;
        esac
        write_conf "$W" "$S" "$U" "$P" "$T" "$HB"
        echo "heartbeat saved to $CONF (mode 600)"
        echo "next: tasks/send_heartbeat.sh && tail -1 results/paper/heartbeat.log"
        ;;
    show)
        show_conf
        ;;
    test)
        exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/notify_failure.sh" --test
        ;;
    clear)
        rm -f "$CONF"
        echo "removed $CONF"
        ;;
    *)
        usage
        exit 2
        ;;
esac
