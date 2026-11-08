#!/bin/bash
# Install (or remove) the backtest dashboard as a macOS launchd agent, so it
# starts at login and is restarted if it ever exits.
#
#   tasks/install_dashboard_macos.sh              # install on port 8765, start now
#   tasks/install_dashboard_macos.sh 9000         # install on another port
#   tasks/install_dashboard_macos.sh --verify     # install, then poll it once
#   tasks/install_dashboard_macos.sh --uninstall  # stop and remove the agent
#
# This replaces starting serve_dashboard.py by hand. Loopback only: the
# dashboard binds 127.0.0.1, so it is never reachable from the network.
set -euo pipefail

LABEL="com.quant.dashboard"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
AGENT_DIR="$HOME/Library/LaunchAgents"
PLIST="$AGENT_DIR/$LABEL.plist"
DOMAIN="gui/$(id -u)"
PORT=8765
VERIFY=0

usage() {
    echo "usage: $(basename "$0") [--uninstall] [--verify] [PORT]" >&2
}

uninstall() {
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST" "$ROOT/.dashboard.pid"
    echo "removed  $LABEL from $AGENT_DIR"
}

for arg in "$@"; do
    case "$arg" in
        --uninstall) uninstall; exit 0 ;;
        --verify) VERIFY=1 ;;
        -h|--help) usage; exit 0 ;;
        [0-9]*) PORT="$arg" ;;
        *) echo "unexpected argument: $arg" >&2; usage; exit 2 ;;
    esac
done

if ! [ "$PORT" -ge 1 ] 2>/dev/null || [ "$PORT" -gt 65535 ]; then
    echo "port out of range: $PORT" >&2
    exit 2
fi

if [ ! -x "$PY" ]; then
    echo "error: $PY is missing; create the virtualenv first (see README, Setup on macOS)" >&2
    exit 1
fi
[ -f "$ROOT/serve_dashboard.py" ] || { echo "serve_dashboard.py not found in $ROOT" >&2; exit 1; }

xml_escape() {
    printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'
}
ROOT_XML="$(xml_escape "$ROOT")"
PY_XML="$(xml_escape "$PY")"

mkdir -p "$AGENT_DIR"

TMP="$PLIST.tmp.$$"
cat > "$TMP" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PY_XML</string>
        <string>$ROOT_XML/serve_dashboard.py</string>
        <string>--port</string>
        <string>$PORT</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$ROOT_XML</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>$ROOT_XML/dashboard.log</string>
    <key>StandardErrorPath</key>
    <string>$ROOT_XML/dashboard.log</string>
</dict>
</plist>
PLIST

plutil -lint "$TMP" >/dev/null
mv "$TMP" "$PLIST"

# Replace any previous copy so re-running this script is idempotent.
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl enable "$DOMAIN/$LABEL"

printf 'job       %s\n' "$LABEL"
printf 'plist     %s\n' "$PLIST"
printf 'command   %s %s/serve_dashboard.py --port %s\n' "$PY" "$ROOT" "$PORT"
printf 'url       http://127.0.0.1:%s\n' "$PORT"
printf 'policy    starts at login, restarts on exit\n'

if [ "$VERIFY" -eq 1 ]; then
    CODE=""
    for _ in $(seq 1 20); do
        sleep 1
        CODE="$(curl -s -o /dev/null -m 2 -w '%{http_code}' "http://127.0.0.1:$PORT/" 2>/dev/null || true)"
        [ "$CODE" = "200" ] && break
    done
    if [ "$CODE" != "200" ]; then
        echo >&2
        echo "dashboard did not answer on http://127.0.0.1:$PORT/ (last http code: ${CODE:-none})" >&2
        echo "check the log, then retry:  tail -20 $ROOT/dashboard.log" >&2
        if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
            echo "note: something else is already listening on port $PORT:" >&2
            lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >&2 || true
            echo "      stop it, or install on another port: $(basename "$0") --verify 9000" >&2
        fi
        exit 3
    fi
    PID="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -1)"
    [ -n "$PID" ] && echo "$PID" > "$ROOT/.dashboard.pid"
    printf 'verify    http 200, serving as pid %s\n' "${PID:-unknown}"
fi

launchctl print "$DOMAIN/$LABEL" 2>/dev/null \
    | grep -E 'state =|pid =|last exit code' | sed 's/^[[:space:]]*/          /' || true
