"""Live 5h/7d/spend usage pulled straight from Anthropic's account API.

The statusline hook only refreshes when an *interactive* Claude Code session
sends a real API request, so it stays frozen while you work through an
interface that never does that (e.g. a lightweight chat surface instead of
an agentic session). This calls the same endpoint the official "Account &
Usage" panel uses, on its own schedule, independent of any Claude Code UI -
so the menu bar app no longer needs Claude Code to be doing anything at all.

Undocumented, unofficial API - if Anthropic changes or removes it, every
function here just returns None/falls back to the disk cache. Never logs or
persists the token itself, only the parsed percentages.
"""
from __future__ import annotations

import json
import ssl
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from usage_store import append_history

CACHE_PATH = Path.home() / "Library/Application Support/ClaudeUsageTracker/live_usage.json"
ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
MIN_REFRESH_SECONDS = 300  # never hit the endpoint more than once per 5 min


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # never follow a redirect carrying the bearer token


def _token() -> str | None:
    """The OAuth access token Claude Code itself already stores locally."""
    try:
        text = (Path.home() / ".claude" / ".credentials.json").read_text()
    except OSError:
        if sys.platform != "darwin":
            return None
        try:
            proc = subprocess.run(
                ["/usr/bin/security", "find-generic-password",
                 "-s", "Claude Code-credentials", "-w"],
                capture_output=True, text=True, timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode:
            return None
        text = proc.stdout
    try:
        return json.loads(text).get("claudeAiOauth", {}).get("accessToken")
    except ValueError:
        return None


def _iso_to_epoch(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _window(payload: dict, key: str) -> dict | None:
    raw = payload.get(key)
    if not isinstance(raw, dict) or not isinstance(raw.get("utilization"), (int, float)):
        return None
    return {"used_percentage": float(raw["utilization"]), "resets_at": _iso_to_epoch(raw.get("resets_at"))}


def _ssl_context() -> ssl.SSLContext:
    """A verifying context built from certifi's CA bundle, read as data.

    Inside the py2app bundle certifi lives in a zip, so `certifi.where()` has
    to extract cacert.pem to a temp file, and it caches that path in a module
    global for the life of the process. macOS eventually purges /var/folders,
    which leaves a long-running menu bar app pointing at a file that no longer
    exists - every later fetch then dies on a FileNotFoundError that no restart
    of the timer can clear. Loading the PEM as `cadata` keeps the certificates
    in memory and never touches the filesystem again.
    """
    try:
        import certifi
        return ssl.create_default_context(cadata=certifi.contents())
    except (ImportError, OSError, ValueError, ssl.SSLError):
        return ssl.create_default_context()  # fall back to the system trust store


def _fetch() -> dict | None:
    """One live HTTP call. None on any failure - callers fall back to the cache."""
    token = _token()
    if not token:
        return None
    req = Request(ENDPOINT, headers={
        "Authorization": "Bearer " + token,
        "anthropic-beta": "oauth-2025-04-20",
    })
    token = None
    try:
        with build_opener(_NoRedirect, HTTPSHandler(context=_ssl_context())).open(req, timeout=5) as resp:
            payload = json.loads(resp.read(1024 * 1024))
    except (HTTPError, URLError, ValueError, OSError):
        return None

    rate_limits = {}
    for key in ("five_hour", "seven_day"):
        window = _window(payload, key)
        if window:
            rate_limits[key] = window
    spend = payload.get("spend")
    if isinstance(spend, dict) and spend.get("enabled") and isinstance(spend.get("percent"), (int, float)):
        rate_limits["spend_limit"] = {"used_percentage": float(spend["percent"]), "resets_at": None}
    return rate_limits or None


def _read_cache() -> dict | None:
    """The cached payload, or None when it is missing, corrupt or malformed.

    Anything that isn't a dict with a numeric `fetched_at` counts as missing,
    so callers can trust that field and a junk file gets replaced by the next
    successful fetch instead of wedging every refresh.
    """
    try:
        cache = json.loads(CACHE_PATH.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(cache, dict) or not isinstance(cache.get("fetched_at"), (int, float)):
        return None
    return cache


def _write_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache))
    tmp.replace(CACHE_PATH)


def cached() -> dict | None:
    """{'rate_limits', 'history', 'fetched_at'} as last fetched - no network."""
    return _read_cache()


def is_stale(cache: dict | None) -> bool:
    """True when `cache` is missing or old enough to warrant a live fetch."""
    if cache is None:
        return True
    return time.time() - cache["fetched_at"] >= MIN_REFRESH_SECONDS


def refresh() -> dict | None:
    """One live fetch plus a cache write. Blocking - keep it off the UI thread.

    A failed fetch (offline, no token, endpoint gone) leaves the cache alone
    and returns whatever was in it, however old - the caller decides whether
    that's too stale to show.
    """
    fresh = _fetch()
    if fresh is None:
        return _read_cache()

    cache = _read_cache()
    now = time.time()
    history = append_history(cache.get("history") if cache else None, fresh, now)
    cache = {"fetched_at": now, "rate_limits": fresh, "history": history}
    try:
        _write_cache(cache)
    except OSError:
        pass  # A fetch that already succeeded is still worth showing.
    return cache
