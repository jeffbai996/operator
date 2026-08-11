"""Cockpit preferences the SERVER has to know about.

Most settings in the hamburger menu are localStorage and never leave the
browser — chat font, keyboard hint, cursor glide all act on the page itself.
The homepage is different: new tabs and the last-tab reset are opened by Python
against the attached Chrome, so the value has to live somewhere that code can
read (the owner 2026-08-07).

Deliberately its own file rather than a key in operator_session: that store is
per-conversation and gets cleared when a chat is deleted, and a browser
preference has nothing to do with which chat is open.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from urllib.parse import urlparse

DEFAULT_HOMEPAGE = "https://www.google.com"

# Long enough for a real deep link, short enough that the prefs file cannot be
# used as storage. Chrome itself stops caring well before this.
MAX_URL = 2048

# Only ever navigated to, never rendered — so the guard is about what Chrome
# will EXECUTE, not about escaping. javascript: runs in whatever document the
# reset lands on; data: and file: read local content into a page the agent can
# then screenshot; chrome:// reaches browser settings.
ALLOWED_SCHEMES = ("http", "https")

_PATH = os.environ.get(
    "OPERATOR_PREFS_PATH",
    os.path.join(os.path.expanduser("~/.cache/computer-use"),
                 "operator-prefs.json")
    + (".demo" if os.environ.get("OPERATOR_DEMO") else ""))
_LOCK = threading.Lock()
_CACHE: dict | None = None


def _cache_clear() -> None:
    """Drop the in-process copy. Tests use it to prove a value persisted."""
    global _CACHE
    _CACHE = None


def _read() -> dict:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    try:
        with open(_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            data = {}
    except Exception:      # noqa: BLE001 — missing or corrupt both mean "no prefs"
        data = {}
    _CACHE = data
    return data


def _write(data: dict) -> None:
    global _CACHE
    os.makedirs(os.path.dirname(_PATH) or ".", exist_ok=True)
    # tmp+rename in the SAME directory, so the replace is atomic and a crash
    # mid-write leaves the old file intact rather than a truncated one.
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(_PATH) or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, _PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _CACHE = data


def normalize_homepage(url: str) -> str:
    """Clean a typed URL, or raise ValueError saying why it was refused.

    An empty value means "back to the default" rather than an error — clearing
    the box is how you undo the setting.
    """
    url = (url or "").strip()
    if not url:
        return DEFAULT_HOMEPAGE
    if len(url) > MAX_URL:
        raise ValueError(f"url is longer than {MAX_URL} characters")
    parsed = urlparse(url)
    if not parsed.scheme:
        # "example.com" — a bare host, which is what people type. Without a
        # scheme Chrome treats it as a relative path against the current page.
        url = "https://" + url
        parsed = urlparse(url)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise ValueError(f"{parsed.scheme}: URLs are not allowed here")
    if not parsed.netloc:
        raise ValueError("that url has no host")
    return url


def homepage() -> str:
    """Where a new tab and the last-tab reset land."""
    with _LOCK:
        value = _read().get("homepage")
    if not isinstance(value, str) or not value.strip():
        return DEFAULT_HOMEPAGE
    try:
        return normalize_homepage(value)
    except ValueError:
        # A value that was valid when stored but is not now (rule tightened)
        # must not strand the cockpit on a URL it refuses to open.
        return DEFAULT_HOMEPAGE


def set_homepage(url: str) -> str:
    """Persist the homepage; returns the stored (normalized) value."""
    clean = normalize_homepage(url)
    with _LOCK:
        data = dict(_read())
        data["homepage"] = clean
        _write(data)
    return clean
