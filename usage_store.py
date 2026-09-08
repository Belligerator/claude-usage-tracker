"""Shared paths, snapshot I/O, and history/forecast/formatting helpers.

`write_snapshot`/`read_snapshot`/`merge_rate_limits` are used only by
claude_statusline.py for its own usage.json. `append_history`, `burn_rate`,
`exhaustion_eta` and the `format_*`/`bar` helpers are network-free and shared
by both claude_statusline.py and oauth_usage.py/claude_monitor.py.
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SNAPSHOT_PATH = Path.home() / "Library/Application Support/ClaudeUsageTracker/usage.json"
SNAPSHOT_LOCK_PATH = SNAPSHOT_PATH.with_suffix(".lock")
LOG_PATH = Path.home() / "Library/Logs/claude-usage-tracker.log"

HISTORY_LIMIT = 200
MIN_RATE_SPAN = 600  # seconds; a shorter span extrapolates one burst into a trend
RATE_LOOKBACK = 1800  # seconds of history the burn rate is measured over
WINDOW_LABELS = {"five_hour": "5h", "seven_day": "7d", "spend_limit": "spend"}


def write_snapshot(data: dict) -> None:
    """Atomically replace the snapshot so a reader never sees a half-written file."""
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(SNAPSHOT_PATH.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle)
        os.replace(tmp, SNAPSHOT_PATH)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_snapshot() -> dict | None:
    """Return the last snapshot, or None when it is missing or corrupt."""
    try:
        with SNAPSHOT_PATH.open() as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


@contextmanager
def snapshot_lock() -> Iterator[None]:
    """Serialize a read-modify-write of the snapshot across concurrent sessions.

    Every Claude Code session renders its own status line, so two of them can
    read the same history, append one sample each and have the slower write
    win - silently dropping samples the burn rate needs. A lock we cannot take
    is not worth failing a status line over, so failures fall through unlocked.
    """
    try:
        SNAPSHOT_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        handle = SNAPSHOT_LOCK_PATH.open("w")
    except OSError:
        yield
        return
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
        except OSError:
            pass
        yield
    finally:
        handle.close()  # closing the descriptor releases the flock


def merge_rate_limits(previous: dict | None, current: dict | None) -> dict:
    """Keep a window the fresh payload omits, as long as it has not reset yet.

    Claude Code omits `rate_limits` until the first API response of a session and
    drops each window once its `resets_at` passes. Carrying a still-valid window
    over avoids the menu bar going blank right after a new session starts.
    """
    merged = dict(current or {})
    now = time.time()
    for name, window in (previous or {}).items():
        if name in merged or not isinstance(window, dict):
            continue
        resets_at = window.get("resets_at")
        if isinstance(resets_at, (int, float)) and resets_at > now:
            merged[name] = window
    return merged


def bar(percentage: float | None, width: int = 10) -> str:
    """Render a percentage as a fixed-width block bar."""
    if percentage is None:
        return "─" * width
    filled = max(0, min(width, round(percentage / 100 * width)))
    return "▓" * filled + "░" * (width - filled)


def format_reset(resets_at: float | None) -> str:
    """Format a reset timestamp as local clock time plus a countdown."""
    if not isinstance(resets_at, (int, float)):
        return "?"
    remaining = int(resets_at - time.time())
    clock = time.strftime("%H:%M", time.localtime(resets_at))
    if remaining <= 0:
        return f"{clock} (just now)"
    hours, minutes = divmod(remaining // 60, 60)
    if hours >= 24:
        clock = time.strftime("%a %H:%M", time.localtime(resets_at))
        return f"{clock} (in {hours // 24} d {hours % 24} h)"
    countdown = f"{hours} h {minutes} m" if hours else f"{minutes} m"
    return f"{clock} (in {countdown})"


def format_age(written_at: float | None) -> str:
    """Describe how old the snapshot is."""
    if not isinstance(written_at, (int, float)):
        return "unknown"
    seconds = int(max(0, time.time() - written_at))
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 86400:
        return f"{seconds // 3600} h {seconds % 3600 // 60} min"
    return f"{seconds // 86400} d"


def append_history(previous: list | None, rate_limits: dict, now: float) -> list:
    """Keep a bounded series of usage samples for the burn-rate estimate.

    A sample is stored only when a percentage actually moves, so the series stays
    tiny - a flat stretch is implied by the gap between two samples.
    """
    history = [s for s in (previous or []) if isinstance(s, dict)]
    sample: dict = {"t": now}
    for key in ("five_hour", "seven_day"):
        window = rate_limits.get(key)
        if isinstance(window, dict) and isinstance(window.get("used_percentage"), (int, float)):
            sample[key] = float(window["used_percentage"])
            sample[f"{key}_reset"] = window.get("resets_at")
    if len(sample) == 1:
        return history[-HISTORY_LIMIT:]
    last = history[-1] if history else None
    if last and all(last.get(k) == v for k, v in sample.items() if k != "t"):
        return history[-HISTORY_LIMIT:]
    history.append(sample)
    return history[-HISTORY_LIMIT:]


def burn_rate(
    history: list | None, key: str, resets_at: object, current_pct: float, now: float
) -> float | None:
    """Percent per hour for one window, or None when there is too little data.

    Samples from an already-reset window are ignored, so a fresh window starts
    measuring from scratch instead of inheriting the previous one's slope. That
    attribution is what `resets_at` is for, so an unknown one means "no idea
    which window these samples belong to" - measuring anyway would anchor the
    new window's rate on the old window's percentages.
    """
    if not isinstance(resets_at, (int, float)):
        return None

    samples = sorted(
        (
            s
            for s in (history or [])
            if s.get(f"{key}_reset") == resets_at
            and isinstance(s.get(key), (int, float))
            and isinstance(s.get("t"), (int, float))
        ),
        key=lambda s: s["t"],
    )
    if not samples:
        return None

    # Usage only grows inside a window, so the newest sample older than the
    # 30-minute lookback still held its value at that boundary. Anchoring there
    # averages the whole half hour instead of only the latest burst, which keeps
    # the estimate steady when work comes in bursts.
    boundary = now - RATE_LOOKBACK
    before = [s for s in samples if s["t"] <= boundary]
    if before:
        anchor_pct, anchor_t = before[-1][key], boundary
    else:
        anchor_pct, anchor_t = samples[0][key], samples[0]["t"]

    span = now - anchor_t
    if span < MIN_RATE_SPAN:
        return None
    return (current_pct - anchor_pct) / span * 3600


def exhaustion_eta(current_pct: float, rate_per_hour: float | None) -> float | None:
    """Seconds until the window would hit 100 %, or None when it will not."""
    if not rate_per_hour or rate_per_hour <= 0:
        return None
    remaining = 100.0 - current_pct
    return 0.0 if remaining <= 0 else remaining / rate_per_hour * 3600


def exhausts_before_reset(eta: float | None, resets_at: object, now: float) -> bool:
    """True when the projected 100 % lands before the window resets.

    Both the status line and the menu bar warn off this, so it lives here -
    two copies of the predicate drifted apart on what an unknown `resets_at`
    means. It means "don't warn": without a reset time there is nothing to
    beat.
    """
    if eta is None or not isinstance(resets_at, (int, float)):
        return False
    return now + eta < resets_at


def format_duration(seconds: float | None) -> str:
    """Format a span of seconds as a short human string."""
    if seconds is None:
        return "?"
    total = int(max(0, seconds))
    hours, minutes = divmod(total // 60, 60)
    if hours >= 24:
        return f"{hours // 24} d {hours % 24} h"
    if hours:
        return f"{hours} h {minutes} m"
    return f"{minutes} m" if minutes else "<1 m"
