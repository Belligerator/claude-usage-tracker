"""Open a fresh 5-hour limit window on an otherwise idle machine.

Anthropic's 5-hour window starts with the first request, not with the clock,
so idling leaves it stopped: start work at 09:00, exhaust the window at 11:00,
and the reset is at 14:00 - where a window opened at 07:00 would have had you
waiting only until 12:00. The quota is a bucket, not a rate, so the win is
purely that the wait after a burnout lands earlier.

The menu bar app already fetches the one fact needed to spot an idle window
(`five_hour.resets_at` is null while none is running), so it can send one tiny
request and start the clock. That request goes through the `claude` CLI, which
already owns the subscription credentials - nothing here reads or handles a
token. Measured cost of one ping: below the whole percentage point the usage
API reports, i.e. invisible in the numbers.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

STATE_PATH = Path.home() / "Library/Application Support/ClaudeUsageTracker/ping_state.json"

# The CLI is run from an empty scratch directory of our own. Run from the
# project instead and it loads that tree's CLAUDE.md and context, then answers
# "ping" with an essay about the repository - 24k tokens to start a clock.
WORK_DIR = STATE_PATH.parent / "ping-cwd"

COOLDOWN_SECONDS = 1800
TIMEOUT_SECONDS = 120
PROMPT = "Reply with exactly: pong"

# A packaged .app is started by launchd with no PATH of its own, so it inherits
# only /usr/bin:/bin:/usr/sbin:/sbin - and `claude` installs under the user's
# home, where that will never find it.
SEARCH_PATH = (
    str(Path.home() / ".local/bin"),
    str(Path.home() / ".claude/local"),
    "/opt/homebrew/bin",
    "/usr/local/bin",
)


def binary() -> str | None:
    """The `claude` executable, or None when it isn't anywhere we look.

    Resolved on every call rather than cached once: the installed path is a
    symlink into a versioned directory that every Claude Code update replaces,
    so a remembered target stops existing the first time the CLI updates.
    """
    path = os.pathsep.join((*SEARCH_PATH, os.environ.get("PATH", os.defpath)))
    return shutil.which("claude", path=path)


# -- persisted state -----------------------------------------------------


def _read_state() -> dict:
    try:
        state = json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _write_state(state: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(STATE_PATH)
    except OSError:
        pass  # a toggle that forgets itself is not worth failing a refresh over


def enabled() -> bool:
    """Whether auto-starting is on. On by default, including on a fresh install."""
    return _read_state().get("enabled", True) is not False


def set_enabled(value: bool) -> None:
    _write_state({**_read_state(), "enabled": bool(value)})


def last_ping() -> tuple[float | None, str]:
    """When the last ping was attempted and how it went, for the menu line.

    A successful ping is otherwise completely invisible - it changes nothing a
    user can see - so this line is the only way to tell the feature works.
    """
    state = _read_state()
    at = state.get("at")
    if not isinstance(at, (int, float)):
        return None, ""
    detail = state.get("detail")
    return at, detail if isinstance(detail, str) else ""


# -- the decision --------------------------------------------------------


def can_ping(rate_limits: dict, now: float) -> bool:
    """True when no 5-hour window is running and we are allowed to start one.

    A missing or malformed `five_hour` means "we don't know", which is not the
    same as "no window": were the upstream payload to change shape, treating it
    as an idle window would ping every five minutes forever. The cooldown is
    the backstop for the same failure - a window lasts five hours, so half an
    hour between pings never delays a legitimate one, but it does turn a
    runaway into 48 requests a day instead of 288.
    """
    if not enabled():
        return False

    window = rate_limits.get("five_hour")
    if not isinstance(window, dict) or window.get("resets_at") is not None:
        return False

    weekly = rate_limits.get("seven_day")
    if isinstance(weekly, dict) and isinstance(weekly.get("used_percentage"), (int, float)):
        if weekly["used_percentage"] >= 100:
            return False  # the request would only be rejected, and nothing resets

    at, _ = last_ping()
    return at is None or now - at >= COOLDOWN_SECONDS


# -- the ping ------------------------------------------------------------


def _run() -> tuple[bool, str]:
    """One CLI invocation. Never raises - the caller logs whatever comes back."""
    path = binary()
    if path is None:
        return False, "claude CLI not found"
    try:
        WORK_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"no working directory: {exc}"
    try:
        proc = subprocess.run(
            [path, "-p", PROMPT,
             "--model", "haiku",
             "--restricted",
             "--strict-mcp-config",
             "--no-session-persistence",
             "--output-format", "json"],
            capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
            cwd=str(WORK_DIR),
            # The machine is usually asleep when this runs, and a ping that
            # thinks first pays for tokens that change nothing.
            env={**os.environ, "MAX_THINKING_TOKENS": "0"},
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {TIMEOUT_SECONDS} s"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not run the CLI: {exc}"
    if proc.returncode:
        return False, f"exit {proc.returncode}: {proc.stderr.strip()[:120] or 'no stderr'}"
    return True, "ok"


def send(now: float) -> tuple[bool, str]:
    """Send one ping, recording the attempt. Blocking - keep it off the UI thread.

    The attempt is written before the subprocess starts, so a ping that hangs
    still holds the cooldown down rather than leaving the door open for the
    next tick to send another.
    """
    _write_state({**_read_state(), "at": now, "detail": "running"})
    ok, detail = _run()
    _write_state({**_read_state(), "at": now, "detail": detail})
    return ok, detail
