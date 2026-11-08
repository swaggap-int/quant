#!/bin/bash
# Install (or remove) the stale-run watchdog as a macOS launchd agent.
#
#   tasks/install_paper_watchdog_macos.sh              # check at 08:15, 14:15, 20:15
#   tasks/install_paper_watchdog_macos.sh --hours 30   # custom staleness threshold
#   tasks/install_paper_watchdog_macos.sh --verify     # install, then run the check once
#   tasks/install_paper_watchdog_macos.sh --uninstall  # stop and remove the agent
#
# The daily job (com.quant.paperdaily) can fail loudly, but it cannot report
# that it never ran at all. This agent covers that gap: it runs
# tasks/check_paper_stale.sh, which looks at the saved run state and sends the
# same notification banner if the last run is older than the threshold.
set -euo pipefail

LABEL="com.quant.paperwatch"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK="$ROOT/tasks/check_paper_stale.sh"
AGENT_DIR="$HOME/Library/LaunchAgents"
PLIST="$AGENT_DIR/$LABEL.plist"
DOMAIN="gui/$(id -u)"
HOURS=26
VERIFY=0

usage() {
    echo "usage: $(basename "$0") [--uninstall] [--verify] [--hours N]" >&2
}

uninstall() {
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "removed  $LABEL from $AGENT_DIR"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --uninstall) uninstall; exit 0 ;;
        --verify) VERIFY=1 ;;
        --hours) shift; HOURS="${1:-}" ;;
        --hours=*) HOURS="${1#*=}" ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unexpected argument: $1" >&2; usage; exit 2 ;;
    esac
    shift
done

case "$HOURS" in
    ''|*[!0-9.]*) echo "hours must be a number: $HOURS" >&2; exit 2 ;;
esac

[ -f "$CHECK" ] || { echo "missing $CHECK" >&2; exit 1; }
chmod +x "$CHECK" 2>/dev/null || true

xml_escape() {
    printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'
}
ROOT_XML="$(xml_escape "$ROOT")"
CHECK_XML="$(xml_escape "$CHECK")"

mkdir -p "$AGENT_DIR" "$ROOT/results/paper"

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
        <string>/bin/bash</string>
        <string>$CHECK_XML</string>
        <string>--hours</string>
        <string>$HOURS</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$ROOT_XML</string>
    <key>StartCalendarInterval</key>
    <array>
        <dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>15</integer></dict>
        <dict><key>Hour</key><integer>14</integer><key>Minute</key><integer>15</integer></dict>
        <dict><key>Hour</key><integer>20</integer><key>Minute</key><integer>15</integer></dict>
    </array>
    <key>RunAtLoad</key>
    <false/>
    <key>ProcessType</key>
    <string>Background</string>
    <key>StandardOutPath</key>
    <string>$ROOT_XML/results/paper/watchdog.log</string>
    <key>StandardErrorPath</key>
    <string>$ROOT_XML/results/paper/watchdog.log</string>
</dict>
</plist>
PLIST

plutil -lint "$TMP" >/dev/null
mv "$TMP" "$PLIST"

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl enable "$DOMAIN/$LABEL"

printf 'job       %s\n' "$LABEL"
printf 'plist     %s\n' "$PLIST"
printf 'command   /bin/bash %s --hours %s\n' "$CHECK" "$HOURS"
printf 'checks    daily at 08:15, 14:15 and 20:15 local\n'
printf 'threshold %s hours without a successful run\n' "$HOURS"

if [ "$VERIFY" -eq 1 ]; then
    launchctl kickstart -k "$DOMAIN/$LABEL"
    CODE=""
    for _ in $(seq 1 15); do
        sleep 1
        CODE="$(launchctl print "$DOMAIN/$LABEL" 2>/dev/null \
            | sed -n 's/.*last exit code = \(-*[0-9][0-9]*\).*/\1/p' | head -1)"
        [ -n "$CODE" ] && break
    done
    printf 'verify    ran, last exit code = %s\n' "${CODE:-unknown}"
    tail -1 "$ROOT/results/paper/watchdog.log" 2>/dev/null | sed 's/^/          /' || true
fi

launchctl print "$DOMAIN/$LABEL" 2>/dev/null \
    | grep -E 'state =|last exit code' | sed 's/^[[:space:]]*/          /' || true
