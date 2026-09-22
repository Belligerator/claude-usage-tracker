#!/usr/bin/env bash
#
# Build, install and start the menu bar app in one command.
#
# The app has to be a packaged .app rather than `python3 claude_monitor.py`,
# because rumps notifications only work reliably from a bundle. So every code
# change means a py2app build, a copy into ~/Applications and a LaunchAgent
# restart - done in the wrong order that leaves a half-installed app behind, or
# a running one holding the bundle open. This is those steps, in an order that
# doesn't.
#
#   ./install.sh               build, install and (re)start
#   ./install.sh --statusline  also point Claude Code's status line here
#   ./install.sh --uninstall   stop it and remove the app and the LaunchAgent
#
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_NAME="Claude Usage Tracker.app"
LABEL="com.belligerator.claudeusagetracker"  # must match CFBundleIdentifier in setup.py
INSTALL_DIR="$HOME/Applications"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
AGENT_LOG="/tmp/claude-usage-tracker.launchagent.log"
DOMAIN="gui/$(id -u)"

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mnote:\033[0m %s\n' "$*"; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'USAGE'
Build, install and start the Claude usage menu bar app.

  ./install.sh               build, install and (re)start
  ./install.sh --statusline  also point Claude Code's status line here
  ./install.sh --uninstall   stop it and remove the app and the LaunchAgent
USAGE
}

action=install
statusline=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --statusline) statusline=1 ;;
        --uninstall)  action=uninstall ;;
        -h|--help)    usage; exit 0 ;;
        *)            die "unknown option: $1 (try --help)" ;;
    esac
    shift
done

[[ "$(uname -s)" == Darwin ]] || die "macOS only - this is an AppKit app run by launchd."
command -v python3 >/dev/null || die "python3 not found"

# Booting the job out is asynchronous, and copying over a bundle whose
# executable is still mapped gives you a corrupt app that launches once and
# never again. Wait for the process to actually go away.
stop_app() {
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    local exe="$INSTALL_DIR/$APP_NAME/Contents/MacOS/"
    for _ in $(seq 25); do
        pgrep -f "$exe" >/dev/null 2>&1 || return 0
        sleep 0.2
    done
    pkill -f "$exe" 2>/dev/null || true
    sleep 0.5
}

if [[ "$action" == uninstall ]]; then
    say "Stopping and removing"
    stop_app
    rm -f "$PLIST"
    rm -rf "${INSTALL_DIR:?}/$APP_NAME"
    say "Removed. Your data is untouched:"
    echo "    ~/Library/Application Support/ClaudeUsageTracker/"
    echo "    ~/Library/Logs/claude-usage-tracker.log"
    exit 0
fi

say "Installing Python dependencies"
python3 -m pip install --quiet --requirement "$REPO/requirements.txt" py2app \
    || die "pip install failed - if your python3 is externally managed, install into a venv first"

say "Building the .app"
cd "$REPO"
rm -rf build dist
build_log="$(mktemp -t claude-usage-build)"
if ! python3 setup.py py2app >"$build_log" 2>&1; then
    tail -30 "$build_log" >&2
    die "py2app build failed (full log: $build_log)"
fi
rm -f "$build_log"
[[ -d "dist/$APP_NAME" ]] || die "the build produced no dist/$APP_NAME"

# py2app compiles the project's own modules into Contents/Resources/lib/pythonXY.zip
# by following imports. One it fails to follow is not a build error - it is a
# menu bar icon that never appears, hours later. Check before installing.
say "Checking the bundle carries the project modules"
python3 - "dist/$APP_NAME" <<'PY' || die "the build is incomplete - do not install it"
import glob, sys, zipfile

app = sys.argv[1]
want = {"oauth_usage.pyc", "usage_store.pyc", "claude_ping.pyc"}
found: set[str] = set()
for archive in glob.glob(f"{app}/Contents/Resources/lib/python*.zip"):
    found |= set(zipfile.ZipFile(archive).namelist())
missing = sorted(want - found)
if missing:
    print("missing from the bundle: " + ", ".join(missing), file=sys.stderr)
    raise SystemExit(1)
print("    " + ", ".join(sorted(want)))
PY

say "Stopping the running app"
stop_app

say "Installing into $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
rm -rf "${INSTALL_DIR:?}/$APP_NAME"
cp -R "dist/$APP_NAME" "$INSTALL_DIR/"
# Ad-hoc signed only, so Gatekeeper would otherwise refuse the first launch.
xattr -dr com.apple.quarantine "$INSTALL_DIR/$APP_NAME" 2>/dev/null || true

say "Writing the LaunchAgent"
mkdir -p "$(dirname "$PLIST")"
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$INSTALL_DIR/$APP_NAME/Contents/MacOS/${APP_NAME%.app}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <!-- Interactive, not Background: launchd throttles Background jobs during
         the dark wakes a sleeping Mac runs on, which is exactly when the app
         needs to notice an idle limit window and open one. -->
    <key>ProcessType</key>
    <string>Interactive</string>
    <key>StandardOutPath</key>
    <string>$AGENT_LOG</string>
    <key>StandardErrorPath</key>
    <string>$AGENT_LOG</string>
</dict>
</plist>
PLISTEOF

say "Starting"
launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null \
    || launchctl kickstart -k "$DOMAIN/$LABEL" \
    || die "launchctl refused to start the job - see $AGENT_LOG"

pid=""
for _ in $(seq 25); do
    pid="$(launchctl print "$DOMAIN/$LABEL" 2>/dev/null | awk '/^[[:space:]]*pid = /{print $3}')"
    [[ -n "$pid" ]] && break
    sleep 0.2
done
[[ -n "$pid" ]] || die "the app did not come up - see $AGENT_LOG"

if [[ "$statusline" == 1 ]]; then
    say "Pointing Claude Code's status line at this checkout"
    python3 - "$REPO/claude_statusline.py" <<'PY' || warn "status line left unchanged"
import json, shutil, sys
from pathlib import Path

script = sys.argv[1]
path = Path.home() / ".claude" / "settings.json"
settings = {}
if path.exists():
    try:
        settings = json.loads(path.read_text())
    except ValueError:
        print("~/.claude/settings.json is not valid JSON", file=sys.stderr)
        raise SystemExit(1)

current = settings.get("statusLine")
if isinstance(current, dict) and current.get("command") not in (None, script):
    # Someone else's status line is not ours to replace.
    print(f"    already set to {current['command']} - left alone")
    raise SystemExit(0)

path.parent.mkdir(parents=True, exist_ok=True)
backup = path.with_name("settings.json.bak")
if path.exists():
    shutil.copy2(path, backup)
settings["statusLine"] = {"type": "command", "command": script, "refreshInterval": 30}
path.write_text(json.dumps(settings, indent=2) + "\n")
print(f"    set (previous settings.json saved as {backup})")
PY
fi

say "Running (pid $pid). The icon is in the menu bar."
echo "    log:        ~/Library/Logs/claude-usage-tracker.log"
echo "    launchd:    $AGENT_LOG"
echo "    uninstall:  ./install.sh --uninstall"
