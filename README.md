# Claude usage tracker

Shows Claude subscription usage (5h/7d window + spend limit) in **Claude
Code's terminal status line** and in a **macOS menu bar app**.

## Two independent data sources

**The terminal status line** (`claude_statusline.py`) reads `rate_limits`
from the JSON Claude Code pipes to it on stdin - no network call of its own,
but the number is only as fresh as the last real Claude Code response, and
it only exists while an interactive CLI/agentic session is running.

> **This script only feeds the Claude Code CLI status line - the line at the
> bottom of the terminal (or the VS Code "Claude Code" panel).** It has
> nothing to do with the macOS menu bar app; the menu bar reads no file it
> writes and calls no function it defines. If you only care about the menu
> bar icon, you can ignore this script entirely - it exists purely to make
> the terminal/CLI status line itself show live 5h/7d/spend bars.

**The menu bar app** (`claude_monitor.py`) gets its numbers **itself,
directly from Anthropic's API** (`oauth_usage.py`) - independent of whether
or how you're using Claude Code. That means it stays accurate even when
you're going through a lighter interface (e.g. a VS Code chat panel) that
never triggers the status line hook, or when you're not doing anything at
all.

```
claude_statusline.py:  Claude Code ──JSON on stdin──> prints a terminal line

claude_monitor.py:     oauth_usage.py ──live GET, 5 min cache──> api.anthropic.com
                                       ─────────────────────────>  menu bar
```

These are **two separate cache files** - `usage.json` (written only by the
status line, for the terminal) and `live_usage.json` (written only by
`oauth_usage.py`, for the menu bar). Neither reads the other. Every Claude
Code session renders its own status line, so writes to `usage.json` are
serialized with a lock file (`usage.lock`, see `snapshot_lock`) - otherwise
two sessions appending a sample each would clobber one another.

## Files

* `claude_statusline.py` - **CLI-only.** Terminal status line, reads
  `rate_limits` from stdin, caches to `usage.json`. Not used by, and not
  needed for, the menu bar app.
* `claude_monitor.py` - **menu bar app.** Data source is `oauth_usage.py`,
  never `claude_statusline.py`.
* `oauth_usage.py` - the live call to Anthropic's API plus a 5-minute disk
  cache.
* `usage_store.py` - shared paths, atomic writes, formatting, and the
  history/burn-rate/ETA helpers (`burn_rate`, `exhaustion_eta`) used by both
  scripts.
* `setup.py` - `py2app` config to package `claude_monitor.py` into a `.app`.

## How `oauth_usage.py` works

1. It reads the OAuth access token Claude Code already stores locally - from
   the macOS Keychain (service `Claude Code-credentials`), or from
   `~/.claude/.credentials.json` on platforms without a Keychain.
2. It calls `GET https://api.anthropic.com/api/oauth/usage` with
   `Authorization: Bearer <token>` - the same endpoint the "Account & Usage"
   panel in the editor is fed from.
3. It stores the result (`five_hour.utilization`, `seven_day.utilization`,
   `spend.percent`) in
   `~/Library/Application Support/ClaudeUsageTracker/live_usage.json`, along
   with a sample history for the burn-rate/ETA estimate (see "Pace and
   projection" below).
4. The app repaints from that cache every 15 s (`POLL_SECONDS`) via
   `cached()`, which never touches the network. When `is_stale()` says the
   cache is older than 5 minutes (`MIN_REFRESH_SECONDS`), it kicks off
   `refresh()` on a worker thread and repaints when that lands - the fetch
   shells out to the keychain and does an HTTPS round trip, so running it on
   AppKit's main thread would freeze the menu bar for seconds. Attempts are
   throttled, not just successes, so a failing endpoint is retried every
   5 minutes rather than every tick. "Refresh now" bypasses the throttle on
   purpose.

This is an **undocumented, unofficial endpoint** - we found it in
`scoped_usage.py` from
[claude-code-usage-bar](https://github.com/leeguooooo/claude-code-usage-bar),
where it's used for a different, opt-in metric (per-model weekly caps).
Anthropic can change or remove it without notice - if that happens,
`_fetch()` in `oauth_usage.py` just returns `None` and the app keeps showing
the last cache it had, or a "couldn't load" placeholder.

## Security / what the app does with the token

The app reads your OAuth access token and sends it in the `Authorization`
header **only** to `api.anthropic.com` - the same address Claude Code itself
talks to. The token is never sent anywhere else, never written to disk
anywhere (not to the log, not to `live_usage.json` - that file only holds
the parsed result, never the token), and the app never refreshes or
modifies it; refreshing the access token is entirely Claude Code CLI's own
job, done through normal use. If the token expires before Claude Code
refreshes it, `_fetch()` gets a 401 and the app silently falls back to the
last cache instead of trying to fix the token itself.

## Installation

```bash
pip3 install -r requirements.txt
```

The status line is enabled in `~/.claude/settings.json`:

```json
"statusLine": {
  "type": "command",
  "command": "/absolute/path/to/claude-usage-tracker/claude_statusline.py",
  "refreshInterval": 30
}
```

The menu bar app runs as a packaged `.app` (`setup.py`, `py2app`) at
`~/Applications/Claude Usage Tracker.app` and starts itself on login via the
LaunchAgent `~/Library/LaunchAgents/com.belligerator.claudeusagetracker.plist`. A
packaged app is also required because `rumps` notifications don't work
reliably outside a `.app` bundle (see Notifications below).

### Building / updating the app after a code change

```bash
pip3 install py2app   # first time only
rm -rf build dist
python3 setup.py py2app
rm -rf ~/Applications/"Claude Usage Tracker.app"
cp -R dist/"Claude Usage Tracker.app" ~/Applications/
launchctl kickstart -k gui/$(id -u)/com.belligerator.claudeusagetracker
```

`launchctl kickstart -k` restarts the app, so the new build takes effect
immediately instead of waiting for the next login.

### Turning auto-start on/off

It's already on (LaunchAgent with `RunAtLoad`). To turn it off:

```bash
launchctl bootout gui/$(id -u)/com.belligerator.claudeusagetracker
rm ~/Library/LaunchAgents/com.belligerator.claudeusagetracker.plist
```

To turn it back on:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.belligerator.claudeusagetracker.plist
```

(the plist needs to exist on disk again - either don't delete it, or restore
it from git/a backup.)

The app is only ad-hoc signed (no Developer ID), so Gatekeeper may refuse to
launch a freshly built copy the first time you open it from Finder - right-
click → Open, or allow it in System Settings → Privacy & Security, fixes
that. It's not an issue via LaunchAgent/`launchctl`, since the app never
launches through Finder that way.

## Pace and projection

A raw percentage isn't enough: 40% sitting idle and 40% climbing at 50%/h
are very different situations. `live_usage.json` keeps a short sample series
(`history`, capped at 200, a new entry only when the number actually changes
between two live fetches), from which the app computes:

* **pace**, in percent per hour, measured over the last 30 minutes,
* **ETA** - how long until that pace hits 100%,
* **critical state** - when the ETA lands *before* the window resets.

In the critical state the menu bar title switches to `⚠️ 5h 74% → 0 in 26m`,
and a notification fires once per window. When the reset comes first, the
pace is sustainable and nothing warns.

The measurement is anchored to a fixed 30-minute boundary (`RATE_LOOKBACK`),
not to the oldest recent sample - otherwise the pace would swing between 0
and the max during bursty work. Until at least 10 minutes of data exist
(`MIN_RATE_SPAN`), it just shows "measuring…". Samples from an already-reset
window are ignored, so a fresh window starts measuring from zero - and a
window whose `resets_at` didn't parse isn't measured at all, since without a
window boundary there is no way to tell which window a sample belongs to.

## Notifications

Notifications fire at 50 / 75 / 90 / 95%, plus one for a critical
projection (see Pace above). Each fires once per window - a window reset
resets the counter. `rumps` notifications only work reliably from a
packaged app - that's why this runs as a `.app` (see Installation above),
not via `python3 claude_monitor.py` from a terminal.

The first notification triggers a one-time macOS permission prompt
(System Settings → Notifications → Claude Usage Tracker).

## Log

`~/Library/Logs/claude-usage-tracker.log` (rotated at 256 KB × 2, menu bar
app only). "Open log" is in the menu.

## Notes on the limits

* The terminal status line only gets `rate_limits` for Pro/Max/Team sessions,
  and only after the first API response - until then, the terminal shows no
  5h/7d segment at all. The menu bar app isn't bound by that; it asks the API
  directly.
* `oauth_usage.py` calls an undocumented API - Anthropic can change or drop
  it at any time; the app reacts to that by showing the last known value
  instead of crashing.
