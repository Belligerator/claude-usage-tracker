#!/usr/bin/env python3
"""macOS menu bar app showing Claude subscription usage.

The 5-hour/7-day/spend percentages come straight from Anthropic's account
API (see oauth_usage.py) - a live call this app makes itself, on its own
5-minute schedule. It does not depend on Claude Code running anywhere, so it
stays accurate no matter which interface (terminal, VS Code agent panel,
VS Code chat) you actually used it from - or if you didn't use it at all.

Run with:  python3 claude_monitor.py
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from logging.handlers import RotatingFileHandler

import rumps
from PyObjCTools import AppHelper

import oauth_usage
from usage_store import (
    LOG_PATH,
    WINDOW_LABELS,
    bar,
    burn_rate,
    exhaustion_eta,
    exhausts_before_reset,
    format_age,
    format_duration,
    format_reset,
)

POLL_SECONDS = 15
NOTIFY_THRESHOLDS = (95, 90, 75, 50)

log = logging.getLogger("claude-usage")


def setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(LOG_PATH, maxBytes=256_000, backupCount=2)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


class ClaudeUsageApp(rumps.App):
    def __init__(self) -> None:
        super().__init__("Claude ⏳", quit_button="Quit")
        self.notified: dict[tuple[str, object], int] = {}
        self.fetching = False
        self.last_attempt = 0.0
        self.items = {
            key: rumps.MenuItem(title)
            for key, title in (
                ("five_hour", "5h: –"),
                ("seven_day", "7d: –"),
                ("spend_limit", "Spend limit: –"),
                ("rate", "Rate: –"),
                ("age", "Updated: –"),
            )
        }
        self.menu = [
            self.items["five_hour"],
            self.items["seven_day"],
            self.items["spend_limit"],
            None,
            self.items["rate"],
            None,
            self.items["age"],
            None,
            rumps.MenuItem("Refresh now", callback=self.on_refresh),
            rumps.MenuItem("Open log", callback=self.on_open_log),
        ]
        self.timer = rumps.Timer(self.refresh, POLL_SECONDS)
        self.timer.start()
        self.refresh(None)

    # -- actions ---------------------------------------------------------

    def on_refresh(self, _) -> None:
        self.refresh(None, force=True)

    def on_open_log(self, _) -> None:
        subprocess.run(["open", "-t", str(LOG_PATH)], check=False)

    # -- rendering -------------------------------------------------------

    def refresh(self, _, force: bool = False) -> None:
        """Paint from the cache, then fetch on a worker if the cache is old.

        AppKit runs this on the main thread, and a fetch shells out to the
        keychain and does an HTTPS round trip - doing it here would freeze the
        menu bar for seconds at a time.
        """
        try:
            cache = oauth_usage.cached()
            self.update(cache)
            if force or oauth_usage.is_stale(cache):
                self.start_fetch(force)
        except Exception:  # noqa: BLE001 - a timer callback must never die
            log.exception("refresh failed")
            self.title = "Claude ⚠️"

    def start_fetch(self, force: bool) -> None:
        """One fetch at a time, and one attempt per throttle window at most.

        The attempt is throttled rather than the success, so a failing endpoint
        gets retried every 5 minutes instead of on every 15-second tick.
        """
        now = time.time()
        if self.fetching:
            return
        if not force and now - self.last_attempt < oauth_usage.MIN_REFRESH_SECONDS:
            return
        self.fetching = True
        self.last_attempt = now
        threading.Thread(target=self.fetch_worker, daemon=True).start()

    def fetch_worker(self) -> None:
        try:
            oauth_usage.refresh()
        except Exception:  # noqa: BLE001 - a worker must never die unlogged
            log.exception("live fetch failed")
        finally:
            self.fetching = False
        AppHelper.callAfter(self.on_fetched)

    def on_fetched(self) -> None:
        """Repaint once a fetch lands. Never starts another one - no retry loop."""
        try:
            self.update(oauth_usage.cached())
        except Exception:  # noqa: BLE001 - an AppKit callback must never die
            log.exception("repaint failed")
            self.title = "Claude ⚠️"

    def update(self, data: dict | None) -> None:
        if data is None:
            self.title = "Claude ⏳"
            for key in ("five_hour", "seven_day", "spend_limit"):
                self.items[key].title = f"{WINDOW_LABELS[key]}: – (couldn't load, not logged in?)"
            self.items["rate"].title = "Rate: –"
            self.items["age"].title = "Updated: never"
            return

        rate_limits = data.get("rate_limits") or {}
        forecast = self.forecast(rate_limits, data.get("history"))
        self.update_windows(rate_limits)
        self.update_rate(forecast)
        self.items["age"].title = f"Updated: {format_age(data.get('fetched_at'))} ago"
        self.title = self.build_title(rate_limits, forecast)
        if forecast.get("critical"):
            self.notify_forecast(forecast)

    def update_windows(self, rate_limits: dict) -> None:
        for key, label in WINDOW_LABELS.items():
            window = rate_limits.get(key)
            item = self.items[key]
            pct = self.percentage(window)
            if pct is None:
                item.title = f"{label}: – (waiting for first live fetch)"
                continue
            item.title = (
                f"{label}: {bar(pct)} {pct:.1f} %  ·  resets {format_reset(window.get('resets_at'))}"
            )
            self.maybe_notify(key, label, pct, window.get("resets_at"))

    def forecast(self, rate_limits: dict, history: list | None) -> dict:
        """Estimate the 5-hour burn rate and whether it exhausts the window early."""
        window = rate_limits.get("five_hour")
        pct = self.percentage(window)
        if pct is None:
            return {}
        resets_at = window.get("resets_at")
        now = time.time()
        rate = burn_rate(history, "five_hour", resets_at, pct, now)
        eta = exhaustion_eta(pct, rate)
        critical = exhausts_before_reset(eta, resets_at, now)
        return {"rate": rate, "eta": eta, "resets_at": resets_at, "critical": critical}

    def update_rate(self, forecast: dict) -> None:
        rate = forecast.get("rate")
        if rate is None:
            self.items["rate"].title = "Rate: measuring… (need a few samples)"
            return
        if rate <= 0:
            self.items["rate"].title = "Rate: 0 %/h – not burning"
            return
        eta = format_duration(forecast.get("eta"))
        mark = "  ⚠️ before reset" if forecast.get("critical") else "  ✓ reset comes first"
        self.items["rate"].title = f"5h rate: +{rate:.0f} %/h → 100% in {eta}{mark}"

    @staticmethod
    def percentage(window: object) -> float | None:
        if not isinstance(window, dict):
            return None
        value = window.get("used_percentage")
        return float(value) if isinstance(value, (int, float)) else None

    @staticmethod
    def build_title(rate_limits: dict, forecast: dict) -> str:
        """Compact menu bar title - just the 5h window; 7d/spend live in the menu."""
        pct = ClaudeUsageApp.percentage(rate_limits.get("five_hour"))
        if pct is None:
            return "Claude ⏳"
        title = f"{WINDOW_LABELS['five_hour']} {pct:.0f}%"
        if forecast.get("critical"):
            title = f"⚠️ {title} → 0 in {format_duration(forecast.get('eta'))}"
        return title

    # -- notifications ---------------------------------------------------

    def notify_forecast(self, forecast: dict) -> None:
        """Warn once per window that the current pace exhausts it before the reset."""
        window_id = ("five_hour_forecast", forecast.get("resets_at"))
        if window_id in self.notified:
            return
        self.notified[window_id] = 1
        eta = format_duration(forecast.get("eta"))
        log.info(
            "5h window projected to run out in %s at %.0f %%/h",
            eta,
            forecast.get("rate") or 0,
        )
        try:
            rumps.notification(
                title="Claude limit – pace",
                subtitle=f"At +{forecast.get('rate', 0):.0f} %/h you'll exhaust the 5h window in {eta}",
                message=f"Resets {format_reset(forecast.get('resets_at'))}.",
            )
        except Exception:  # noqa: BLE001 - notifications need a bundled app
            log.warning("notification failed (app isn't packaged via py2app)")

    def maybe_notify(self, key: str, label: str, pct: float, resets_at: object) -> None:
        """Notify once per threshold per window; a new window starts over."""
        window_id = (key, resets_at)
        reached = next((t for t in NOTIFY_THRESHOLDS if pct >= t), None)
        if reached is None:
            self.notified.pop(window_id, None)
            return
        if self.notified.get(window_id, 0) >= reached:
            return
        self.notified[window_id] = reached
        log.info("threshold %s%% reached for %s (%.1f%%)", reached, key, pct)
        try:
            rumps.notification(
                title="Claude limit",
                subtitle=f"{label} window at {pct:.0f} %",
                message=f"Resets {format_reset(resets_at)}.",
            )
        except Exception:  # noqa: BLE001 - notifications need a bundled app
            log.warning("notification failed (app isn't packaged via py2app)")


if __name__ == "__main__":
    setup_logging()
    log.info("starting (live API, no statusline dependency)")
    ClaudeUsageApp().run()
