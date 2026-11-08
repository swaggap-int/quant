#!/bin/bash
# Install (or remove) the daily paper-run job with macOS launchd.
#
#   tasks/install_paper_daily_macos.sh              # install, daily at 07:30 local
#   tasks/install_paper_daily_macos.sh 06:45        # install at another local time
#   tasks/install_paper_daily_macos.sh --verify     # install, then run once to prove it works
#   tasks/install_paper_daily_macos.sh --uninstall  # stop and remove the job
#
# This replaces the Windows-only register_paper_daily.ps1. It needs no
# elevation: the job lives in ~/Library/LaunchAgents and runs as you, paper
# only, calling tasks/paper_daily.sh.
#
# macOS protects ~/Documents with TCC. A LaunchAgent's shell cannot read this
# project from there until the job's interpreter is granted Full Disk Access in
# System Settings > Privacy & Security > Full Disk Access. --verify detects
# that and says so. Moving the project out of ~/Documents also avoids it.
set -euo pipefail

LABEL="com.quant.paperdaily"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WRAPPER="$ROOT/tasks/paper_daily.sh"
AGENT_DIR="$HOME/Library/LaunchAgents"
PLIST="$AGENT_DIR/$LABEL.plist"
DOMAIN="gui/$(id -u)"
AT="07:30"   # local time; 1d bars close at 00:00 UTC
VERIFY=0

usage() {
    echo "usage: $(basename "$0") [--uninstall] [--verify] [HH:MM]" >&2
}

uninstall() {
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "removed  $LABEL from $AGENT_DIR"
}

for arg in "$@"; do
    case "$arg" in
        --uninstall) uninstall; exit 0 ;;
        --verify) VERIFY=1 ;;
        -h|--help) usage; exit 0 ;;
        [0-9][0-9]:[0-9][0-9]) AT="$arg" ;;
        *) echo "unexpected argument: $arg" >&2; usage; exit 2 ;;
    esac
done

HOUR=$((10#${AT%%:*}))
MINUTE=$((10#${AT##*:}))
if [ "$HOUR" -gt 23 ] || [ "$MINUTE" -gt 59 ]; then
    echo "time out of range: $AT" >&2
    exit 2
fi

[ -f "$WRAPPER" ] || { echo "wrapper not found: $WRAPPER" >&2; exit 1; }
if [ ! -x "$ROOT/.venv/bin/python" ]; then
    echo "warning: $ROOT/.venv/bin/python is missing; the job will fall back to python3" >&2
    echo "         (the project needs Python 3.10+; see README, Setup on macOS)" >&2
fi

xml_escape() {
    printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'
}
ROOT_XML="$(xml_escape "$ROOT")"
WRAPPER_XML="$(xml_escape "$WRAPPER")"

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
        <string>/bin/sh</string>
        <string>$WRAPPER_XML</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$ROOT_XML</string>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>$HOUR</integer>
        <key>Minute</key>
        <integer>$MINUTE</integer>
    </dict>
    <key>RunAtLoad</key>
    <false/>
    <key>ProcessType</key>
    <string>Background</string>
    <key>StandardOutPath</key>
    <string>$ROOT_XML/results/paper/scheduler.log</string>
    <key>StandardErrorPath</key>
    <string>$ROOT_XML/results/paper/scheduler.log</string>
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
printf 'command   /bin/sh %s\n' "$WRAPPER"
printf 'daily at  %s local\n' "$AT"

if [ "$VERIFY" -eq 1 ]; then
    launchctl kickstart -k "$DOMAIN/$LABEL"
    CODE=""
    for _ in $(seq 1 20); do
        sleep 1
        CODE="$(launchctl print "$DOMAIN/$LABEL" 2>/dev/null \
            | sed -n 's/.*last exit code = \(-\{0,1\}[0-9][0-9]*\).*/\1/p' | head -1)"
        [ -n "$CODE" ] && break
    done
    if [ "$CODE" = "126" ] || grep -q "Operation not permitted" "$ROOT/results/paper/scheduler.log" 2>/dev/null; then
        cat >&2 <<'MSG'

The job ran but macOS blocked it from reading ~/Documents (TCC), exit 126.

Fix it one of these ways, then re-run with --verify:
  1. System Settings > Privacy & Security > Full Disk Access > + , then add
     /bin/sh (press Cmd+Shift+G in the file picker and type /bin/sh).
  2. Move this project out of ~/Documents (for example to ~/quant) and
     reinstall the job from its new location.
MSG
        exit 3
    fi
    printf 'verify    ran, last exit code = %s\n' "${CODE:-unknown}"
fi

launchctl print "$DOMAIN/$LABEL" 2>/dev/null \
    | grep -E 'state =|last exit code|next fire date' | sed 's/^[[:space:]]*/          /' || true
