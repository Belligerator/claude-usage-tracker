#!/usr/bin/env python3
"""Claude Code status line: prints usage in the terminal.

Claude Code pipes the session JSON to this script on stdin. The `rate_limits`
block in that JSON is the only per-session view of 5-hour/7-day usage; this
script renders it and caches it to usage.json for its own benefit (so the
next line can compute a burn rate). The menu bar app (claude_monitor.py) does
not read this file - it gets its numbers independently, see oauth_usage.py.

Enable it in ~/.claude/settings.json:

    {
      "statusLine": {
        "type": "command",
        "command": "/absolute/path/to/claude-usage-tracker/claude_statusline.py",
        "refreshInterval": 30
      }
    }

The status line runs locally and consumes no API tokens.
"""
from __future__ import annotations

import json
import sys
import time

from usage_store import (
    append_history,
    bar,
    burn_rate,
    exhaustion_eta,
    exhausts_before_reset,
    format_duration,
    merge_rate_limits,
    read_snapshot,
    snapshot_lock,
    write_snapshot,
)

RESET = "\033[0m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"


def colour(percentage: float | None) -> str:
    """Green below 50 %, yellow below 80 %, red above."""
    if percentage is None:
        return DIM
    if percentage < 50:
        return GREEN
    return YELLOW if percentage < 80 else RED


def percentage_of(window: object) -> float | None:
    if not isinstance(window, dict):
        return None
    value = window.get("used_percentage")
    return float(value) if isinstance(value, (int, float)) else None


def store_snapshot(data: dict, rate_limits: dict, history: list) -> None:
    """Persist the account-wide numbers and the sample series for the menu bar app."""
    write_snapshot(
        {
            "written_at": time.time(),
            "history": history,
            "session_id": data.get("session_id"),
            "model": (data.get("model") or {}).get("display_name"),
            "rate_limits": rate_limits,
            "context_window": data.get("context_window") or {},
            "cost": data.get("cost") or {},
        }
    )


def projection(rate_limits: dict, history: list) -> str:
    """Warn when the 5-hour window is on track to run out before it resets."""
    window = rate_limits.get("five_hour")
    pct = percentage_of(window)
    if pct is None:
        return ""
    resets_at = window.get("resets_at")
    now = time.time()
    eta = exhaustion_eta(pct, burn_rate(history, "five_hour", resets_at, pct, now))
    if not exhausts_before_reset(eta, resets_at, now):
        return ""  # The window resets first - the current pace is sustainable.
    return f" {RED}⚠ 100% in {format_duration(eta)}{RESET}"


def render(data: dict, rate_limits: dict, history: list) -> list[str]:
    """Build the status line rows."""
    model = (data.get("model") or {}).get("display_name") or "Claude"
    context = data.get("context_window") or {}
    context_pct = context.get("used_percentage")
    cost = (data.get("cost") or {}).get("total_cost_usd") or 0.0

    head = f"{DIM}{model}{RESET}"
    if isinstance(context_pct, (int, float)):
        head += f" · {colour(context_pct)}ctx {context_pct:.0f}%{RESET}"
    head += f" · {DIM}${cost:.2f}{RESET}"
    rows = [head]

    segments = []
    for name, label in (("five_hour", "5h"), ("seven_day", "7d"), ("spend_limit", "spend")):
        pct = percentage_of(rate_limits.get(name))
        if pct is None:
            continue
        segments.append(f"{colour(pct)}{label} {bar(pct, 8)} {pct:.0f}%{RESET}")
    if segments:
        rows.append(" · ".join(segments) + projection(rate_limits, history))
    return rows


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (ValueError, OSError):
        print("Claude")
        return

    with snapshot_lock():
        previous = read_snapshot() or {}
        rate_limits = merge_rate_limits(previous.get("rate_limits"), data.get("rate_limits"))
        history = append_history(previous.get("history"), rate_limits, time.time())
        try:
            store_snapshot(data, rate_limits, history)
        except OSError:
            pass  # A status line must never fail because of the cache file.

    for row in render(data, rate_limits, history):
        print(row)


if __name__ == "__main__":
    main()
