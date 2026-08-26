"""Browser operator — live view + full remote control of the logged-in Chrome.

One self-contained surface (full-screen on an iPad over Tailscale) that shows the
real Chrome the squad's computer-use drives and lets you take the wheel live —
click, type, navigate — interleaving freely with whatever a bot is doing in the
same browser (shared mouse; last action wins). "See it, steer it." (the owner
2026-06-25; refined for click/keyboard control + more controls 2026-06-26.)

Zero new deps — playwright + aiohttp are already in the host-app venv:
  - VIEW: a background thread holds a Playwright connect_over_cdp() attach to the
    Windows Chrome on :9222 and grabs JPEG frames of the active page into a buffer. The
    Flask route streams that as multipart/x-mixed-replace (MJPEG) → an <img>.
  - CONTROL: POST actions run on the SAME attached page. Coordinate clicks come in
    normalized (0..1) so the frontend needn't know the viewport; we scale to the
    live viewport size (also reported to the frontend for letterbox mapping).
"""
from __future__ import annotations

import asyncio
import re
import threading
import time
from collections import deque as _deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from flask import (Blueprint, Response, jsonify, render_template, request,
                   send_file, has_request_context)
import operator_agent  # the headless-claude agent runner (option 1)
import operator_prefs  # server-side cockpit settings (the landing page)
import operator_tasks as operator_tasks_store  # saved-task store (#30)

import os as _os_cfg

# THE release version. It used to be typed into the template three times and
# pinned in the tests a fourth, so a bump meant four edits and a red suite when
# you forgot one (2026-08-05: two hand-bumps in a row did exactly that). One
# constant now, injected into every template this blueprint renders — including
# the generated public demo, whose label appends " demo" to whatever it finds.
# The README's version ladder must carry a row for this version; a test asserts
# it, so the changelog cannot silently fall behind the number on screen.
OP_VERSION = "1.1.0"
# DEMO isolation (the public demo): a second instance runs with OPERATOR_DEMO=1 and
# its own isolated, NOT-logged-in Chrome on a separate CDP port. These env vars are
# unset for the owner's live cockpit (-> no behavior change); set only by demo_server.py.
DEMO = _os_cfg.environ.get("OPERATOR_DEMO") == "1"
DEMO_INTERACTIVE = (
    DEMO
    and _os_cfg.environ.get("OPERATOR_UNSAFE_DEMO_INTERACTIVE") == "1"
)
# Standalone full-function instances (operator-fam): the cockpit is the whole
# app — the template hides the host-app site chrome and takes the viewport.
# Unset for the host-app-mounted cockpit (no behavior change).
STANDALONE = _os_cfg.environ.get("OPERATOR_STANDALONE") == "1"
# Match the remote browser to the visible stage by default. The desktop CSS
# floor below prevents responsive mobile layouts while matching the aspect
# removes the four-edge letterbox gutter. Set OPERATOR_VIEWPORT_FOLLOW=0 only
# as an explicit recovery switch.
_VIEW_FOLLOW = _os_cfg.environ.get("OPERATOR_VIEWPORT_FOLLOW", "1") != "0"
# both the live _Streamer and the agent MCP attach here in demo mode (isolated
# Chrome), never :9222 (the logged-in browser). The unguessable path gate is the
# WSGI url-prefix mounted by demo_server.py (APPLICATION_ROOT=/<slug>/<hash>).
CDP_URL = _os_cfg.environ.get("OPERATOR_DEMO_CDP") or "http://127.0.0.1:9222"
if DEMO:
    # the demo may view/drive the SANDBOX surface, but never the owner's container —
    # scope it to its own (sandbox_container.py reads this at load).
    _os_cfg.environ.setdefault("OPERATOR_SANDBOX_CONTAINER", "operator-sandbox-demo")
FRAME_INTERVAL = 0.066     # ~15fps (the owner's pick)
IDLE_FRAME_INTERVAL = 0.35  # ~3fps after the pixels have stayed quiet
MOTION_HOLD_S = 1.4         # keep full cadence through short UI animations
JPEG_QUALITY = 60
IDLE_STOP_AFTER = 90.0
# A browser-surface run is allowed to start only after the same Playwright/CDP
# path that paints the cockpit has produced a real frame. HTTP /json can stay
# healthy while one restored renderer is wedged; connect_over_cdp then waits
# forever on that target and the agent used to start against no usable browser.
BROWSER_ATTACH_TIMEOUT = 10.0
BROWSER_READY_TIMEOUT = 12.0
# F1 adaptive frame tier — ?tier=lo (narrow viewport / Save-Data clients) gets
# lean frames. Browser lo frames are downscaled PER-CAPTURE via CDP clip+scale
# (never Emulation.setDeviceMetricsOverride, which would resize the SHARED page
# under the agent) and compressed harder; a Retina tablet otherwise pulls the
# full device-resolution JPEG every frame. Sandbox lo lowers the ffmpeg rate
# and raises -q:v (2-31, higher = smaller frames; hi keeps the 10fps/q8 default).
TIER_LO_QUALITY = 35
TIER_LO_MAX_W = 900
TIER_LO_SANDBOX_FPS, TIER_LO_SANDBOX_Q = 6, 12
MIN_VIEWPORT_W, MIN_VIEWPORT_H = 320, 240
DESKTOP_LAYOUT_MIN_W = 1280
DESKTOP_CSS_MIN_W = 1024
# A vertical scrollbar shaves ~15 device px off cssLayoutViewport (which _grab
# reads), so a scrollable page under the forced 1280 width reports ~1012 CSS —
# 12 px under the floor. Without this allowance _matches_view_metrics failed
# every frame on any scrollable page and _force_desktop_page's clear+apply
# reflowed the REAL window at frame rate (the 2026-07-21 GUI strobe).
# 24→40 (2026-07-22): a page with BOTH scrollbars (a wide table forcing the
# horizontal bar, e.g. theyshootpictures' 1000-films table) reads down to 988
# at 125% display scale — 12px under the old allowance — so the gate failed,
# the repair reflow toggled the scrollbars, and the strobe was back on that
# page class.
# 40→64 (2026-07-22, same night): STILL strobing on the films table. Live
# probes showed the full mechanism: each repair snaps the viewport to 1024,
# then the page RELAXES back to its 964 equilibrium in 12px scrollbar-quantum
# steps over ~20s (1012→1000→988→976→964), and mid-walk transients dip under
# whatever exact floor is set — re-arming the next repair. Chasing scrollbar
# arithmetic is whack-a-mole; split the actual clusters instead: desktop with
# any scrollbar combo reads ~950-1024, genuinely collapsed mobile emulation
# reads ≤700. Floor 960 (allowance 64) sits in the dead zone. Paired with the
# REPAIR_AFTER_MISSES persistence gate below, which ignores 1-2-frame reflow
# transients entirely.
SCROLLBAR_CSS_ALLOW = 64
# A metrics-gate failure must persist this many CONSECUTIVE grab frames before
# a repair fires (2026-07-22). A genuinely collapsed/stale emulation fails
# forever, so repair still lands within ~a second; a scrollbar-reflow transient
# (the strobe fuel) lasts 1-2 frames and now never triggers the clear+apply.
REPAIR_AFTER_MISSES = 4

# Repairs (clear+apply, a REAL full-page reflow — the visible "zooms in then
# back out" pulse) only fire below this: the collapsed-mobile-emulation band
# they exist to fix reads ~650-720. Between here and the 960 gate floor lives
# the walked/shaved zone (2026-07-23: a page width WALKING down in 12px
# scrollbar quanta every ~4s bottomed at 928 — layout fine, capture fine, and
# the 30s repair snap was the only thing the user could SEE). Gate misses in
# that zone log (vp-walk) instead of repairing.
REPAIR_HARD_FLOOR_W = 800
# Collapsed-band scoring (2026-07-29). REPAIR_AFTER_MISSES is a CONSECUTIVE
# counter, zeroed by any healthy read — right for the ambiguous scrollbar zone,
# wrong below the hard floor where the reading is unambiguous. The recorder
# caught the failure it lets through: ten `vp-walk 1024->651` events in one
# session, the layout viewport FLIPPING between healthy and collapsed rather
# than walking down. Alternating reads reset the counter every other frame, so
# the repair for the collapsed band never fired and the captured frame kept
# changing size under the viewer (the owner: "the viewport went super narrow, like a
# strip maybe 1/3 of the total viewport height"). Scoring instead of counting: a
# collapsed read outweighs a healthy one, so a flip-flop accumulates while a
# lone navigation transient still decays to nothing.
COLLAPSE_HIT = 2
COLLAPSE_DECAY = 1
COLLAPSE_REPAIR_AT = 4
COLLAPSE_SCORE_CAP = 8


def collapse_score(prev: int, *, gate_ok: bool, css_w: float,
                   floor: float = REPAIR_HARD_FLOOR_W) -> int:
    """Next collapsed-viewport score. Pure, so the flip-flop case is testable
    without driving a frame loop.

    Only reads BELOW `floor` score — between there and the gate floor lives the
    scrollbar-quantum zone, which self-documents via vp-walk and must never
    trigger a reflow.
    """
    if not gate_ok and css_w and css_w < floor:
        return min(COLLAPSE_SCORE_CAP, prev + COLLAPSE_HIT)
    return max(0, prev - COLLAPSE_DECAY)


def _fit_clip_to_view_aspect(width: float, height: float,
                             view_w: float, view_h: float) -> tuple[float, float]:
    """Expand a zoom-split capture clip to the requested viewport aspect.

    Windows Chrome profiles can carry a default page zoom (125% here). CDP then
    reports a CSS viewport smaller than the device-metrics canvas. Some pages
    still lay their body out at the full canvas width, so the existing extent
    guard widened the clip without growing its height: a requested 1280x760
    viewport became a 1280x608 frame and object-fit letterboxed it. Both source
    dimensions are already capped to the requested canvas; expanding only the
    deficient axis therefore restores the aspect without cropping content.
    """
    if width <= 0 or height <= 0 or view_w <= 0 or view_h <= 0:
        return width, height
    target = view_w / view_h
    current = width / height
    if current > target:
        height = min(view_h, width / target)
    elif current < target:
        width = min(view_w, height * target)
    return width, height


def _clamp_stage(w: float, h: float) -> tuple[int, int]:
    """A viewer's stage size → the remote viewport target it may ask for.

    Scales a narrow stage up to the desktop layout floor without changing its
    shape (the captured frame has to fill that stage), then bounds the result
    so no client can ask the shared browser for something absurd. Returns
    (0, 0) for a degenerate reading — a stage measured mid-layout reports zero,
    and a zero target is the letterbox.
    """
    try:
        w, h = int(float(w)), int(float(h))
    except (TypeError, ValueError):
        return 0, 0
    if w <= 0 or h <= 0:
        return 0, 0
    if w < DESKTOP_LAYOUT_MIN_W:
        h = round(h * DESKTOP_LAYOUT_MIN_W / max(1, w))
        w = DESKTOP_LAYOUT_MIN_W
    w = min(1600, w)
    h = max(480, min(1300, h))
    if w * h > 1_900_000:
        h = 1_900_000 // w
    return w - w % 2, h - h % 2


def _emu_clip_box(*, css_w: float, css_h: float, dev_w: float, dev_h: float,
                  body_w: float, body_h: float, page_x: float, page_y: float,
                  view_w: float, view_h: float) -> tuple[float, float]:
    """Capture clip for the emulated regime, in DEVICE pixels.

    Page.captureScreenshot reads `clip` in device pixels, but every page
    reading available here — cssLayoutViewport, document.body's scroll box,
    the scroll offset — is in CSS pixels. On a display-scaled Chrome the two
    differ by the scale factor, so feeding CSS numbers straight into a
    device-space clip captured only the top-left 1/scale of the canvas: the
    right column and the footer were never in the frame at all. Measured live
    on google.com 2026-08-14 — css 1036x894 against a 1295x1118 canvas, so
    the clip came out 1036x905 and lost a fifth of the page. Worse, the
    aspect fitter below then matched the stage exactly, so it presented as a
    narrow page rather than as the crop it was. Convert first, then clip.

    The floor is the device layout viewport, which IS the painted canvas — it
    can neither crop real content nor pad a blank band. The body scroll box
    only ever grows the clip, and only as far as the requested view target.
    """
    scale = (dev_w / css_w) if css_w else 1.0
    bw, bh = dev_w, dev_h
    if body_w:
        bw = max(dev_w, min(view_w, (body_w - page_x) * scale))
    if body_h and view_h:
        bh = max(dev_h, min(view_h, (body_h - page_y) * scale))
    if view_h:
        bw, bh = _fit_clip_to_view_aspect(bw, bh, view_w, view_h)
    return bw, bh
# Frames after a run starts during which we re-assert view metrics if the
# agent's CDP attach knocked them off. The loop paces 0.45s while busy, so 5
# covers roughly the first ~2.2s — long enough for a slow attach, short enough
# that a run which resizes on purpose is left alone.
RUN_START_FIXUPS = 5

# Viewport ownership (2026-07-22, the "random zoom" fix). The stage_size beacon
# fires on every cockpit LOAD (plus resize / rail-drag-end), and applying it
# unconditionally let ANY second client — a phone tab resuming in the
# background, a reconnect, a headless probe — re-aspect the SHARED remote
# browser under the active viewer. On a desktop stage (wider than the frame)
# the height change alters the object-fit:contain scale: the "zooms in and out
# for a second" pulse. Proven live 13:06:21 2026-07-22 — a headless cockpit's
# load beacon reflowed the real browser 1280x1014 → 1280x858 mid-session.
# Ownership: the viewer whose beacon last applied owns the aspect while it
# keeps pulling frames; others are refused (owned:true → slow client retry)
# until the owner has been quiet this long.
# 15 → 2.5 (2026-07-26): with per-tab cids a live watcher pulls sub-second and
# a closed/backgrounded tab stops pulling within one pump frame, so 2.5s
# cleanly separates "watching" from "gone". At 15s, re-entering the cockpit
# left the DEAD previous tab owning the aspect — the fresh tab's load beacon
# was refused and the stage sat letterboxed until a manual rail drag re-fired
# it (the owner: "letterboxed until I move the resize bar").
VP_OWNER_IDLE_S = 2.5

bp = Blueprint("operator", __name__,
                template_folder="templates", static_folder="static",
                static_url_path="/operator-static")


def _origin_tuple(url: str) -> tuple[str, str, int | None] | None:
    """Canonical origin for browser CSRF checks, including default ports."""
    try:
        parsed = urlsplit(url)
        if not parsed.scheme or not parsed.hostname:
            return None
        port = parsed.port
    except ValueError:
        return None
    if port is None:
        port = {"http": 80, "https": 443}.get(parsed.scheme.lower())
    return parsed.scheme.lower(), parsed.hostname.lower(), port


@bp.before_request
def _reject_cross_origin_mutations():
    """Keep hostile pages from driving a tailnet user's Operator session.

    Tailnet remains the identity boundary. Browser-supplied Origin, Referer,
    and Fetch Metadata provide the narrower CSRF boundary; headerless CLI and
    internal callers remain compatible because they are already inside that
    trusted perimeter.
    """
    if DEMO and not DEMO_INTERACTIVE and request.endpoint not in {
            "operator.operator_page",
            "operator.operator_demo_page",
            "operator._cockpit_redirect",
            "operator.operator_models",
            "operator.static",
    }:
        return jsonify(ok=False, error="the public Operator demo is read-only"), 403
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None

    expected = _origin_tuple(request.host_url)
    supplied = request.headers.get("Origin")
    fetch_site = (request.headers.get("Sec-Fetch-Site") or "").lower()
    referer = request.referrer

    # Fetch Metadata's ``same-site`` is broader than same-origin. Safari and
    # installed PWAs can report it even for a request whose explicit Origin is
    # exactly this Operator. Trust that exact origin (or referer) match; keep
    # rejecting same-site requests that provide no origin evidence at all.
    if supplied is not None:
        foreign = (
            _origin_tuple(supplied) != expected
            or fetch_site == "cross-site"
        )
    elif referer is not None:
        foreign = (
            _origin_tuple(referer) != expected
            or fetch_site == "cross-site"
        )
    else:
        foreign = fetch_site in {"cross-site", "same-site"}
    if foreign:
        return jsonify(
            ok=False,
            error="cross-origin Operator mutation refused",
        ), 403
    return None


@bp.after_request
def _revisioned_static_delivery(resp):
    """Make Operator's explicit revision query a real immutable cache key.

    Flask serves blueprint static files through a direct-passthrough response,
    which also caused host-app's generic gzip hook to skip the 326KB JS and
    246KB CSS. Revisioned text assets are safe to materialise for compression
    and cache for a year; unrevisioned assets retain Flask's conservative
    policy so a forgotten revision bump cannot pin stale bytes indefinitely.
    """
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
    if DEMO and not DEMO_INTERACTIVE:
        resp.headers["X-Operator-Demo"] = "read-only"
    if request.endpoint == "operator.static" and request.args.get("rev"):
        if 200 <= resp.status_code < 300:
            ctype = (resp.headers.get("Content-Type") or "").split(";", 1)[0].strip()
            if ctype in ("text/css", "application/javascript", "text/javascript"):
                # The app-level compression hook runs after this blueprint hook.
                resp.direct_passthrough = False
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp

import base64 as _b64ph

# chrome://new-tab-page renders BLANK under --headless=new + --disable-gpu (the demo's
# launch flags) — Chromium's WebUI new-tab surface needs a GPU compositing path that
# isn't there, so the "reset" silently produces an empty page instead of an error.
# A local data: URL bypasses Chrome's internal NTP entirely — no navigation/rendering-
# path quirks — so it always paints. Loaded via RAW CDP Page.navigate, not Playwright's
# page.set_content()/page.goto() — those wait on the page's own lifecycle-event
# machinery, which (like page.mouse/page.evaluate elsewhere in this file) can hang
# indefinitely on a desynced connect_over_cdp page handle. Page.navigate fire-and-forget
# bounded by asyncio.wait_for never blocks the grab loop.
# Default landing page for tab open/close/new-tab/home. the owner wanted the branded
# custom NTP (templates/newtab.html) gone in favor of google.com; chrome://new-tab-page
# renders blank under headless+no-GPU (see comment above), so google.com is the
# option that actually paints. Still navigated via raw CDP Page.navigate.
# google.com is now only the FALLBACK: the landing page is a setting (the owner
# 2026-08-07, "add that to the hamburger menu settings"). operator_prefs owns
# the stored value and the scheme guard; this constant is what you get when
# nothing is stored.
_NEWTAB_DATA_URL = operator_prefs.DEFAULT_HOMEPAGE


def _op_error(e: Exception) -> str:
    """A message the cockpit can show.

    concurrent.futures.TimeoutError stringifies to the EMPTY STRING, so a tab
    operation that ran out of time reported `{"ok": false, "error": ""}` — a
    failure banner with no reason in it (measured 2026-08-07). Name the class
    when the instance has nothing to say.
    """
    import concurrent.futures as _cf
    if isinstance(e, (_cf.TimeoutError, asyncio.TimeoutError, TimeoutError)):
        return f"timed out after {TAB_OP_TIMEOUT:g}s"
    return str(e) or type(e).__name__


def _landing_url() -> str:
    """Where a new tab and the last-tab reset go. Read per call, so changing
    the setting takes effect on the next tab rather than the next restart."""
    try:
        return operator_prefs.homepage()
    except Exception:  # noqa: BLE001 — a broken prefs file must not block a tab
        return _NEWTAB_DATA_URL


# Tab open/close budget. The 8s that used to be here was SHORTER than what the
# body could legitimately spend (a CDP navigate at 4s plus four bounded
# Emulation sends at 3s each), so a slow-but-successful op came back ok:false
# with an empty error — measured 2026-08-07 at exactly 8.19s per operation
# while the tab opened and closed correctly the whole time. The outer bound
# must exceed the inner one or it reports success as failure.
TAB_INNER_BUDGET = 16.0
TAB_OP_TIMEOUT = 20.0
_DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)

# Color scheme forced onto every attached target (see _force_desktop_page).
# The host browser/OS run dark; CDP attach flips pages to light, so we pin it.
_COLOR_SCHEME = _os_cfg.environ.get("OPERATOR_COLOR_SCHEME") or "dark"


# tiny dark placeholder frame (matches --bg) so the MJPEG stream always has
# valid data and the <img> never shows the broken-image glyph before/between
# real captures.
_PLACEHOLDER_JPEG = _b64ph.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAoHBwgHBgoICAgLCgoLDhgQDg0NDh0VFhEYIx8lJCIfIiEmKzcvJik0KSEiMEExNDk7Pj4+JS5ESUM8SDc9Pjv/2wBDAQoLCw4NDhwQEBw7KCIoOzs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozv/wAARCAGQAoADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDx2iiiqEFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAf/2Q=="
)


@dataclass
class _Streamer:
    frame: bytes | None = None
    frame_id: int = 0               # advances only when encoded pixels change
    frame_ts: float = 0.0
    last_view: float = 0.0
    status: str = "idle"          # idle | connecting | live | error
    detail: str = ""
    vw: int = 0                   # live viewport size (for click coord scaling)
    vh: int = 0
    cur_url: str = ""             # URL cached by the async grab loop; read by sync status route
    last_click: tuple = (0.0, 0.0, 0.0)   # (norm_x, norm_y, monotonic_ts) — agent cursor
    # zoom is CSS document zoom — fine-tuning ON TOP of the layout width below.
    # It does NOT change innerWidth (proven live 2026-07-16), so it can never
    # make a desktop-width layout fit a phone; view_w is the lever that reflows.
    # 0.8 = two 0.1 notches under neutral (the owner 2026-07-19). The old 0.5 was a
    # band-aid that shrank content INSIDE the giant pre-view_w canvas.
    zoom: float = 0.8
    # view_w — the CSS layout width Operator forces via CDP. Keep it at or above
    # a desktop responsive breakpoint; narrower values make surviving tabs
    # silently flip into a site's mobile layout after tab/viewport changes.
    # height 0 = auto (keep the real window height); 0 disables the override.
    view_w: int = DESKTOP_LAYOUT_MIN_W
    # view_h — 0 = auto (native window height). Set alongside view_w by the
    # stage_size steer (smart viewport follow): the viewer's stage aspect
    # becomes the remote viewport aspect, so the frame fills the stage with no
    # letterbox on any device.
    view_h: int = 0
    _thread: threading.Thread | None = None
    _loop: asyncio.AbstractEventLoop | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _running: bool = False
    _page = None
    _pw = None
    _browser = None
    _cdp = None
    _metric_sessions: dict = field(default_factory=dict)
    _target_ids: dict = field(default_factory=dict)   # page -> CDP target id (stable per page)
    _crashed_pages: set = field(default_factory=set)
    _io_lock = None      # asyncio.Lock — serialize grab vs actions on the CDP page
    _user_closed = False  # True when Chrome was closed manually → don't auto-relaunch (the owner)
    _key_repeat = None   # dict[key -> asyncio.Task] — held-key auto-repeat loops
    # F1: frame tier, set by the feed routes (last-viewer-wins on the shared
    # frame buffer — single-user cockpit; per-viewer buffers are a 1.0.10 idea)
    tier: str = "hi"
    _eager_evt = None    # asyncio.Event — an input action wakes the grab loop (F2)
    # relaunch pacing (2026-07-11): with CDP unreachable, every /frame poll used
    # to relaunch the thread — a fresh driver process per second, and any driver
    # not stopped on the failure path leaked (15 orphans in one night). Error
    # exits now arm a growing backoff that ensure_running honors; a good frame
    # resets it so recovery after Chrome comes back is fast again.
    _fail_streak: int = 0
    _backoff_until: float = 0.0
    _was_busy: bool = False   # agent-run edge detector (sweep emulation on end)
    _motion_until: float = 0.0
    # viewport ownership + flight recorder (see VP_OWNER_IDLE_S above)
    _vp_owner: str = ""                    # cid of the viewer whose aspect is applied
    _vp_seen: dict = field(default_factory=dict)    # cid -> last frame-pull monotonic ts
    _vp_events: object = field(default_factory=lambda: _deque(maxlen=48))

    # ---- viewport ownership ---------------------------------------------
    def vp_note_pull(self, cid: str) -> None:
        """Stamp viewer liveness from the feed routes (hot path — keep cheap)."""
        if not cid:
            return
        self._vp_seen[cid] = time.monotonic()
        if len(self._vp_seen) > 16:
            stale = min(self._vp_seen, key=self._vp_seen.get)
            if stale != cid:
                self._vp_seen.pop(stale, None)

    def vp_beacon_allowed(self, cid: str) -> bool:
        """May this client's stage_size reflow the shared browser? Yes when it
        IS the owner, there is no owner, or the owner stopped pulling frames
        (left / backgrounded) past the idle window."""
        owner = self._vp_owner
        if not owner or cid == owner:
            return True
        ts = self._vp_seen.get(owner)
        return ts is None or time.monotonic() - ts > VP_OWNER_IDLE_S

    def seed_view_from_stage(self, w: float, h: float) -> bool:
        """Pre-load the viewport target from a returning viewer's stage size.

        The cockpit could only tell the server its stage AFTER its script ran
        and its layout settled, so every session opened on whatever the last
        viewer left — or on the WIDTHx0 default, whose auto height object-fit
        renders as a letterbox — until a real resize fired a beacon. That is
        the "wrong size until I drag it, then it snaps" report (the owner
        2026-08-15). The stage is knowable earlier than that: the browser
        sends it on the document request, so the target can be in place before
        the streamer has attached, and the FIRST captured frame is already
        right.

        Only a hint, deliberately: it never touches CDP and never overrides a
        viewer who is actually watching (same arbiter the beacon obeys). The
        client's own beacon remains the authority and corrects a stale hint.
        """
        if not _VIEW_FOLLOW:
            return False
        w, h = _clamp_stage(w, h)
        if not w or not h or (w, h) == (self.view_w, self.view_h):
            return False
        if not self.vp_beacon_allowed(""):
            return False
        self.view_w, self.view_h = w, h
        self._vp_log("seed", f"{w}x{h}")
        return True

    def _vp_log(self, kind: str, detail: str = "") -> None:
        """Flight recorder: every path that can reflow the remote page leaves a
        trace (the strobe/zoom hunts took 5 rounds because nothing recorded the
        writers). Read via /operator/debug/viewport."""
        self._vp_events.append({"t": time.time(), "kind": kind, "detail": detail})

    def _publish_frame(self, data: bytes) -> bool:
        """Publish captured pixels and return whether they actually changed.

        `frame_ts` is capture health, so it advances for identical screenshots.
        `frame_id` is network identity, so it advances only when the encoded
        JPEG changes. Keeping those clocks separate lets status stay live while
        pull clients skip retransmitting the same 30KB frame ten times a second.
        """
        now = time.monotonic()
        changed = data != self.frame
        if changed:
            self.frame = data
            self.frame_id += 1
            self._motion_until = now + MOTION_HOLD_S
        self.frame_ts = now
        return changed

    def _capture_interval(self, *, now: float, busy: bool) -> float:
        """Preserve the agent-sharing limit; otherwise idle at roughly 3fps."""
        if busy:
            return 0.45
        if now < self._motion_until:
            return FRAME_INTERVAL
        return IDLE_FRAME_INTERVAL

    # ---- lifecycle -------------------------------------------------------
    def has_attached_page(self) -> bool:
        """Whether input and capture currently have a usable renderer target."""
        page = self._page
        if page is None or page in self._crashed_pages:
            return False
        try:
            return not page.is_closed()
        except Exception:
            return False

    def _mark_page_crashed(self, page) -> None:
        """Invalidate a crashed target immediately, even if Playwright leaves
        its page object open and addressable for a while."""
        self._crashed_pages.add(page)
        if page is self._page:
            self._page = None
            self._cdp = None
            self._cdp_for = None
            self.frame = None
            self.frame_ts = 0.0
            self.status = "connecting"
            self.detail = "browser tab crashed — reconnecting"

    def _track_page(self, page) -> None:
        try:
            page.on("crash", lambda _page=None: self._mark_page_crashed(page))
        except Exception:
            pass

    def _live_pages(self, ctx) -> list:
        pages = []
        for page in ctx.pages:
            if page in self._crashed_pages:
                continue
            try:
                if not page.is_closed():
                    pages.append(page)
            except Exception:
                continue
        return pages

    def ensure_running(self) -> None:
        with self._lock:
            self.last_view = time.monotonic()
            # restart if flagged running but the thread actually died (stale flag)
            alive = self._thread is not None and self._thread.is_alive()
            if self._running and alive:
                return
            if time.monotonic() < self._backoff_until:
                return   # recent abnormal death — don't thrash the relaunch
            self._running = False  # reset a stale flag so we cleanly relaunch
            self._running = True
            self.status = "connecting"
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="operator-streamer")
            self._thread.start()

    def require_ready(self, timeout: float = BROWSER_READY_TIMEOUT) -> str | None:
        """Synchronously prove the cockpit browser before dispatch.

        This is deliberately stronger than _cdp_alive(): Chrome can keep its
        HTTP target list alive while Playwright cannot attach to one of the
        renderers. A fresh frame proves the exact control/capture path the
        agent and the user are about to share. Returns a user-facing error;
        it never lets a browser task fail open into runner.start().
        """
        try:
            self._user_closed = False
            self._ensure_chrome_alive()
            self.ensure_running()
        except Exception as e:  # noqa: BLE001 — dispatch must fail closed
            return self.detail or str(e) or "browser could not start"

        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            now = time.monotonic()
            if (self.status == "live" and self.frame
                    and now - self.frame_ts <= 3.0):
                return None
            if self.status == "error":
                return self.detail or "browser connection failed"
            thread = self._thread
            if thread is not None and not thread.is_alive():
                return self.detail or "browser connection stopped"
            time.sleep(0.05)
        return self.detail or "browser connection timed out"

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        # asyncio.Lock/Task bind to the loop they're created on. A reattach spins a
        # fresh loop here, so DROP any primitives cached against the previous loop —
        # else they raise "bound to a different event loop" on the next action and the
        # status card flashes "Failed" for every click/keystroke. Rebuilt lazily.
        self._io_lock = None
        self._eager_evt = None  # asyncio.Event bound to the dead loop -> rebuilt lazily on this loop
        self._key_repeat = {}   # drop any Tasks bound to the dead old loop
        try:
            self._loop.run_until_complete(self._grab_loop())
        except Exception as e:  # noqa: BLE001
            self.status, self.detail = "error", str(e)
        finally:
            self._running = False
            with self._lock:
                if self.status == "error":
                    # abnormal death (attach failure / wedge) → pace the relaunch
                    self._fail_streak = min(self._fail_streak + 1, 8)
                    self._backoff_until = time.monotonic() + min(
                        10.0, 2.0 ** self._fail_streak)
                elif self.status == "live":
                    self.status = "idle"

    @staticmethod
    def _chrome_attach_script() -> str:
        """Path to the (re)launcher for the active mode — the demo's isolated
        headless Chrome under DEMO, the Windows Chrome otherwise.

        OPERATOR_CHROME_LAUNCHER overrides both: operator-fam's CDP lives on
        :9333 (a dedicated persistent-profile Windows Chrome launched by
        opfam-chrome.sh), but generic
        chrome-attach.sh defaults to :9222/browse-automation-chrome with no way
        to know it should target :9333 instead. Each standalone instance points
        this at its own launcher via env; unset falls back to the primary
        Windows launcher."""
        import os
        override = _os_cfg.environ.get("OPERATOR_CHROME_LAUNCHER")
        if override:
            return os.path.expanduser(override)
        if DEMO:
            return os.path.expanduser("~/local-projects/operator-demo/op-demo-chrome.sh")
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "browse", "chrome-attach.sh")

    def _cdp_alive(self) -> bool:
        """True when Chrome can serve its target list, not merely the cheap
        version endpoint (which can stay alive while the renderer is wedged)."""
        import json as _json
        import urllib.request
        try:
            raw = urllib.request.urlopen(CDP_URL + "/json", timeout=3).read()
            _json.loads(raw)
            return True
        except Exception:  # noqa: BLE001 — liveness probe
            return False

    def _active_target_id(self) -> "str | None":
        """The REAL foreground tab's CDP target id, or None if unavailable.

        Chrome orders /json/list most-recently-ACTIVATED first, so entry [0] is
        the visible tab. This is the only foreground signal that actually works
        on the operator's Chrome: `document.visibilityState` reports 'visible'
        for EVERY tab here (verified 2026-07-29 — 4 tabs, all 'visible', all
        hasFocus()==true), because a CDP-driven window that isn't OS-focused
        never computes per-tab occlusion. requestAnimationFrame is equally
        useless (fires in all tabs — renderers stay unthrottled under CDP).
        Ordering held across three consecutive activations AND a URL-stable
        poke, which is the case the url-diff heuristic structurally cannot see.

        Blocking urllib on the streamer thread, matching _cdp_alive(); the
        1.5s timeout keeps a wedged Chrome from stalling the grab loop."""
        import json as _json
        import urllib.request
        try:
            raw = urllib.request.urlopen(CDP_URL + "/json/list", timeout=1.5).read()
            for t in _json.loads(raw):
                if t.get("type") == "page":
                    return t.get("id")
            return None
        except Exception:  # noqa: BLE001 — best-effort foreground probe
            return None

    async def _page_target_id(self, pg) -> "str | None":
        """CDP target id for a Playwright page, memoized per page.

        Resolving it costs a throwaway CDP session, so cache by page object —
        a target id is stable for the page's whole life. The session is always
        detached (a leaked one is per-target state Chrome holds until browser
        disconnect — the same leak class as the 2026-07-23 width walker)."""
        cache = self._target_ids
        if pg in cache:
            return cache[pg]
        sess = None
        try:
            sess = await pg.context.new_cdp_session(pg)
            r = await asyncio.wait_for(sess.send("Target.getTargetInfo"), timeout=0.5)
            tid = (r.get("targetInfo") or {}).get("targetId")
        except Exception:  # noqa: BLE001 — best-effort
            tid = None
        finally:
            if sess is not None:
                try:
                    await sess.detach()
                except Exception:  # noqa: BLE001
                    pass
        if tid:
            cache[pg] = tid
        return tid

    def _launch_chrome(self) -> None:
        """Run the existing idempotent launcher from the streamer thread.

        chrome-attach.sh owns a host-wide flock and re-checks CDP after taking
        it, so simultaneous first-viewer requests cannot stampede Chrome."""
        import os
        import subprocess
        attach = self._chrome_attach_script()
        if not os.path.exists(attach):
            raise FileNotFoundError(f"browser launcher not found: {attach}")
        subprocess.run(
            ["bash", attach],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=45,
            check=False,
        )

    def _ensure_chrome_alive(self) -> None:
        """Demand-start Chrome when an Operator viewer actually needs it.

        There is intentionally no server-boot or background-watchdog launch:
        closing Bot Chrome leaves it closed until the first frame/stream request
        starts this streamer. The launcher itself serializes concurrent demand."""
        if self._cdp_alive():
            self._user_closed = False
            return
        self.status, self.detail = "connecting", "starting browser…"
        try:
            self._launch_chrome()
        except Exception as e:  # noqa: BLE001 — surface launcher failures
            self._user_closed = True
            self.status, self.detail = "error", f"browser could not start: {e}"
            raise ConnectionError(self.detail) from e
        if not self._cdp_alive():
            self._user_closed = True
            self.status, self.detail = "error", "browser could not start"
            raise ConnectionError(self.detail)
        self._user_closed = False

    async def _attach(self) -> None:
        from playwright.async_api import async_playwright
        self._ensure_chrome_alive()
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.connect_over_cdp(CDP_URL)
        ctx = self._browser.contexts[0] if self._browser.contexts else \
            await self._browser.new_context()
        self._crashed_pages.clear()
        pages = self._live_pages(ctx)
        for page in pages:
            self._track_page(page)
        if pages:
            for page in pages:
                await self._force_desktop_page(page)
            # Headless Chromium may restore the real session AND manufacture a
            # synthetic about:blank launcher target. Target ordering then calls
            # the blank "foreground", so the cockpit streams a perfectly healthy
            # black rectangle while the user's real tabs sit behind it. Prefer
            # the first restored page with content; only use a blank when there
            # is nothing else to show.
            real_pages = [p for p in pages if p.url not in ("", "about:blank")]
            self._page = real_pages[0] if real_pages else pages[0]
            # The sweep left the session cache bound to the LAST page swept —
            # drop it so nothing reuses a session for a page we didn't select.
            self._cdp = None
            self._cdp_for = None
            # A freshly launched Chrome already has ONE tab — its own default
            # new-tab page — so `pages` is never actually empty on a cold
            # start, and the no-pages fallback below never fires. That
            # existing page sat on about:blank forever with nothing to
            # navigate it (the owner 2026-08-04, reproduced live after auto-heal
            # relaunched Chrome: "right now it lands in about:blank"). Treat
            # a blank EXISTING page exactly like having no page: land it on
            # the same URL the true no-pages case already used below. Only
            # the selected page, and only when it's genuinely blank — a real
            # page must never be yanked out from under the user on attach.
            if self._page.url in ("", "about:blank"):
                try:
                    await self._cdp_navigate(self._page, _NEWTAB_DATA_URL)
                except Exception:  # noqa: BLE001 — landing nav is best-effort
                    pass
            try:
                await asyncio.wait_for(self._page.bring_to_front(), timeout=2)
            except Exception:  # noqa: BLE001 — foregrounding is best-effort
                pass
        else:
            # fallback page: navigate it to the landing URL — a bare new_page()
            # sits on about:blank forever ("new tab doesn't load the home page")
            self._page = await ctx.new_page()
            self._track_page(self._page)
            self._cdp = None
            try:
                await self._force_desktop_page(self._page)
                await self._cdp_navigate(self._page, _NEWTAB_DATA_URL)
            except Exception:  # noqa: BLE001 — landing nav is best-effort
                pass
        try:
            def _on_page(page):
                self._track_page(page)
                asyncio.create_task(self._force_desktop_page(page))
            ctx.on("page", _on_page)
        except Exception:
            pass
        try:
            await ctx.add_init_script("""
                (function(){
                  if (window.__opClickHooked) return; window.__opClickHooked = true;
                  function rec(e){ try {
                    var w = window.innerWidth || 1, h = window.innerHeight || 1;
                    window.__opClick = { x: e.clientX / w, y: e.clientY / h, t: Date.now() };
                  } catch(_){} }
                  window.addEventListener('pointerdown', rec, true);
                  window.addEventListener('click', rec, true);
                })();
            """)
        except Exception:
            pass
        # re-apply the chosen zoom on every navigation
        try:
            if self.zoom and self.zoom != 1.0:
                await ctx.add_init_script(
                    f"document.addEventListener('DOMContentLoaded',()=>{{document.documentElement.style.zoom='{self.zoom}';}});")
        except Exception:
            pass
        # Init scripts only cover future documents, so apply the persisted zoom
        # to the currently open target as well.
        try:
            if self.zoom and self.zoom != 1.0:
                await asyncio.wait_for(self._page.evaluate(
                    "zoom => { document.documentElement.style.zoom = String(zoom); }",
                    self.zoom,
                ), timeout=2.5)
        except Exception:
            pass
        # Also install click tracking on the CURRENTLY-open page.
        # WRAP IN A TIMEOUT: evaluate() on a privileged page (chrome://new-tab-page) or a
        # busy/heavy page (e.g. Bloomberg mid-load) can BLOCK FOREVER with no built-in
        # timeout, wedging _attach before it ever sets status="live" — the streamer then
        # sits in "connecting" indefinitely and the browser pane never paints. Bounding it
        # means a hostile current page degrades gracefully (no click-hook on it) instead of
        # taking the whole streamer down.
        try:
            await asyncio.wait_for(self._page.evaluate("""
                (function(){
                  if (window.__opClickHooked) return; window.__opClickHooked = true;
                  function rec(e){ try {
                    var w = window.innerWidth || 1, h = window.innerHeight || 1;
                    window.__opClick = { x: e.clientX / w, y: e.clientY / h, t: Date.now() };
                  } catch(_){} }
                  window.addEventListener('pointerdown', rec, true);
                  window.addEventListener('click', rec, true);
                })();
            """), timeout=2.5)
        except Exception:
            pass
        self._update_viewport()
        self.status, self.detail = "live", ""

    def _update_viewport(self) -> None:
        try:
            vp = self._page.viewport_size
            if vp and self._accept_viewport(vp.get("width"), vp.get("height")):
                return
        except Exception:  # noqa: BLE001
            pass

    def _accept_viewport(self, width, height) -> bool:
        """Cache only usable browser viewports; transient single-digit widths
        otherwise poison frame clipping and every later click mapping."""
        try:
            width, height = int(width), int(height)
        except (TypeError, ValueError):
            return False
        if width < MIN_VIEWPORT_W or height < MIN_VIEWPORT_H:
            return False
        self.vw, self.vh = width, height
        return True

    def _usable_click_basis(self, width, height) -> bool:
        """A viewport good enough to MAP CLICKS against.

        Deliberately stricter than _accept_viewport, whose 320px floor exists
        to keep single-digit garbage out of the frame clip. A collapsed-but-
        plausible read sails through that floor and then silently rescales
        every click: the flight recorder caught `vp-walk 1024->651` twice on
        2026-07-31, which lands a pointer at 64% of where it was aimed —
        "clicks arent landing in the right place" (the owner, same evening).

        The frame path already refuses to trust a width under
        REPAIR_HARD_FLOOR_W. The click path has to agree with it, or the
        picture and the pointer are working off two different viewports.

        The floor check runs BEFORE _accept_viewport on purpose: that method
        caches into self.vw/self.vh as a side effect, so letting a collapsed
        read reach it also poisons the last-resort fallback below.
        """
        try:
            w = float(width)
        except (TypeError, ValueError):
            return False
        if self.view_w:
            floor = min(float(REPAIR_HARD_FLOOR_W), float(self.view_w))
            if w < floor:
                return False
        return self._accept_viewport(width, height)

    def _matches_view_metrics(self, width, height) -> bool:
        """True only when a usable viewport matches Operator's current target."""
        try:
            width, height = int(width), int(height)
        except (TypeError, ValueError):
            return False
        if width < MIN_VIEWPORT_W or height < MIN_VIEWPORT_H:
            return False
        # Chrome's 125% host display scale turns a 1280 device-metrics width
        # into 1024 CSS px. Responsive layout follows CSS pixels, so compare
        # the observed viewport to the desktop CSS floor, not to view_w — and
        # give a scrollbar allowance: cssLayoutViewport excludes the ~15px
        # scrollbar, so scrollable pages legitimately read a few px under the
        # floor. A hard floor here strobed the real window (see SCROLLBAR_CSS_ALLOW).
        if width < DESKTOP_CSS_MIN_W - SCROLLBAR_CSS_ALLOW:
            return False
        return True

    def _iolock(self):
        if self._io_lock is None:
            self._io_lock = asyncio.Lock()
        return self._io_lock

    async def _grab_loop(self) -> None:
        # try/finally so the driver is stopped on EVERY exit path — including an
        # _attach() that raises (2026-07-11: a failed connect_over_cdp left the
        # freshly-started driver orphaned; combined with per-poll relaunch this
        # leaked one node process per second all night).
        try:
            await asyncio.wait_for(self._attach(), timeout=BROWSER_ATTACH_TIMEOUT)
            await self._grab_loop_inner()
        finally:
            self.frame = None      # stopping → no stale 'live' with no frames
            await self._teardown()

    async def _grab_loop_inner(self) -> None:
        _misses = 0
        while self._running:
            if time.monotonic() - self.last_view > IDLE_STOP_AFTER:
                break
            try:
                await self._refresh_active_page()
                await self._follow_active_tab()
                async with self._iolock():
                    png = await self._grab(self._page)
                if png:
                    self._publish_frame(png)
                    if self.status == "connecting":
                        self.status, self.detail = "live", ""
                    try: self.cur_url = self._page.url or ""
                    except Exception: pass
                    _misses = 0
                    if self._fail_streak:
                        # frames flowing again → recovery is proven; relaunches
                        # go back to instant for the next incident
                        with self._lock:
                            self._fail_streak = 0
                            self._backoff_until = 0.0
                else:
                    _misses += 1
                    if _misses >= 4:
                        # wedged Chrome (alive but screenshots hang/fail). No more
                        # auto-relaunch here (2026-07-05 — see _ensure_chrome_alive):
                        # stop cleanly and surface it instead of shelling out to
                        # chrome-attach.sh, which could race a concurrent dispatch's
                        # own relaunch attempt. Manual relaunch via the bot-chrome
                        # script is the expected recovery now; this is rare enough
                        # (per the owner) that it doesn't need to self-heal.
                        _misses = 0
                        self.status, self.detail = "error", "Chrome wedged — relaunch it via the bot-chrome script"
                        break
                if not self.vw:
                    # CDP-attached pages have NO Playwright viewport_size (it's None for
                    # connect_over_cdp), so the sync helper leaves vw/vh=0 → manual click
                    # mapping breaks. Read the REAL viewport via JS innerWidth/innerHeight.
                    # BOUND IT: page.evaluate() has no built-in timeout and can block
                    # FOREVER on a page whose JS world isn't responsive (observed: a
                    # connect_over_cdp page reporting url=='' yet still screenshottable).
                    # Unbounded, this froze the grab loop after the first frame — the
                    # stream delivered one buffered burst then went silent (status "live",
                    # vw stuck at 0). Bound + CDP-layout fallback so the loop never stalls.
                    try:
                        _vp = await asyncio.wait_for(self._page.evaluate(
                            "({w: window.innerWidth, h: window.innerHeight})"), timeout=1.0)
                        if _vp and self._accept_viewport(_vp.get("w"), _vp.get("h")):
                            pass
                        else:
                            await self._force_desktop_page(self._page)
                    except Exception:
                        # JS world slow/unavailable → CDP layout metrics, then sync helper.
                        try:
                            sess = await self._cdp_session(self._page)
                            m = await asyncio.wait_for(sess.send("Page.getLayoutMetrics"), timeout=1.0)
                            vv = (m or {}).get("visualViewport") or {}
                            cw, ch = int(vv.get("clientWidth") or 0), int(vv.get("clientHeight") or 0)
                            if self._accept_viewport(cw, ch):
                                pass
                            else:
                                await self._force_desktop_page(self._page)
                        except Exception:
                            self._update_viewport()
            except Exception as e:  # noqa: BLE001
                self.detail = str(e)
                # A single transient capture error is normal during navigation —
                # don't thrash the page/session for it. Only escalate to a reattach
                # after several consecutive failures.
                _misses += 1
                if _misses < 4:
                    await asyncio.sleep(FRAME_INTERVAL)
                    continue
                _misses = 0
                # page-level hiccup → soft swap; whole-browser drop → hard re-attach
                ok = await self._reattach_soft()
                if not ok:
                    try:
                        await self._teardown()
                        self.status = "connecting"   # after teardown (which idles)
                        await self._attach()
                    except Exception as e2:  # noqa: BLE001
                        # browser connection is GONE — exit and let ensure_running
                        # relaunch under the backoff, instead of starting a fresh
                        # driver process inside the loop every ~1.3s (2026-07-11)
                        self.status, self.detail = "error", str(e2)
                        break
            # ease off while an agent drives (shares CDP with the agent's MCP);
            # _pace (not sleep) so a cockpit action wakes the loop instantly (F2)
            try:
                busy = operator_agent.runner.is_running()
            except Exception:
                busy = False
            if busy and not self._was_busy:
                # Run just STARTED. The agent's Playwright MCP attaches to the
                # SAME Chrome over CDP, and that attach pushes Playwright's own
                # emulation defaults — dropping our setDeviceMetricsOverride,
                # so the canvas snaps from view_w back to the window's native
                # width the moment a task begins (the owner 2026-07-29 "the Operator
                # browser suddenly resizes"). Same mechanism as the
                # prefers-color-scheme flip handled in _force_desktop_page.
                #
                # The repair loop does NOT cover this: its gate only fires when
                # the viewport is UNDER REPAIR_HARD_FLOOR_W (800), and an
                # attach makes the page WIDER (~1349), not narrower — so
                # nothing ever corrected it.
                #
                # Armed as a COUNTDOWN, not a single shot, because the attach
                # lands somewhere AFTER is_running() flips: re-asserting once
                # on the transition would just get clobbered by an attach a
                # few hundred ms later.
                self._run_start_fixups = RUN_START_FIXUPS
            if busy and getattr(self, "_run_start_fixups", 0) > 0:
                self._run_start_fixups -= 1
                # _gate_misses is recomputed every frame in _grab and is >0
                # exactly when the live reading no longer matches view_w, so it
                # doubles as "the attach clobbered us". Only re-assert then —
                # a matching viewport needs nothing, and staying quiet keeps us
                # off the back of a run that resizes deliberately.
                if getattr(self, "_gate_misses", 0) > 0:
                    try:
                        self._vp_log("run-start-reassert",
                                     f"{self._run_start_fixups} left")
                        await self._force_desktop_page(self._page, force=True)
                    except Exception:  # noqa: BLE001
                        pass
            if self._was_busy and not busy:
                # run just ended → sweep the emulation it may have left on the
                # shared browser (never mid-run: a run may emulate deliberately)
                try:
                    self._vp_log("run-end-sweep")
                    await self._clear_emulation()
                except Exception:  # noqa: BLE001
                    pass
            self._was_busy = busy
            await self._pace(self._capture_interval(
                now=time.monotonic(), busy=busy))

    async def _grab(self, page):
        """Raw JPEG frame via CDP Page.captureScreenshot — no font-loading wait
        (page.screenshot() font-waits and hung 30s on heavy pages). Falls back to
        a short-timeout page.screenshot if CDP isn't available."""
        import base64 as _b64
        try:
            # Identity-checked session (not a bare self._cdp read): the attach
            # sweep / any page swap can leave the cache bound to a DIFFERENT
            # page, streaming one tab while input targets another (Codex P1).
            sess = await self._cdp_session(page)
            lo = self.tier == "lo"
            args = {"format": "jpeg",
                    "quality": TIER_LO_QUALITY if lo else JPEG_QUALITY}
            # FULL-COVERAGE, DEVICE-RES frames (2026-07-12, rev 2). On a Chrome
            # whose device scale ≠ 1 (Windows display scaling — here 1.25),
            # captureScreenshot's clip is interpreted in DEVICE pixels. We clip
            # the FULL device viewport so coverage is complete (no right/bottom
            # crop — the owner's "right edge cut off").
            #
            # OUTPUT AT DEVICE RESOLUTION (scale=1.0), not CSS width. Click
            # accuracy does NOT depend on frame size — the frontend sends
            # NORMALIZED (0..1) coords that _viewport_css maps to CSS pixels for
            # Input.dispatchMouseEvent, so frame dimensions are irrelevant to
            # where a tap lands. The earlier "normalize to CSS width" clip threw
            # away ~20% of resolution for nothing (690 vs 863 px on a 1.25x
            # display) → frames upscaled soft on the phone ("pixelated as shit").
            # WORSE: only the CDP path downscaled; the except-branch fallback
            # (page.screenshot) captures at DEVICE res, so a transient CDP-grab
            # failure during a nav flipped the served frame 690<->863 every few
            # frames — the phone's <img> rescaled each swap ("spasming between
            # small and big at constant frequency"). Same device-res on both
            # paths = one stable size, no rescale pulse, full sharpness.
            # lo tier still downscales (bandwidth) but by a FIXED cap, so its
            # size is stable too.
            try:
                _m = await asyncio.wait_for(sess.send("Page.getLayoutMetrics"),
                                            timeout=1.0)
                _css = ((_m or {}).get("cssLayoutViewport")
                        or (_m or {}).get("layoutViewport") or {})
                _dev = ((_m or {}).get("layoutViewport")
                        or (_m or {}).get("cssLayoutViewport") or {})
                _cw = float(_css.get("clientWidth") or 0)
                _ch = float(_css.get("clientHeight") or 0)
                _gate_ok = (self._accept_viewport(_cw, _ch)
                            and self._matches_view_metrics(_cw, _ch))
                self._gate_misses = 0 if _gate_ok else (
                    getattr(self, "_gate_misses", 0) + 1)
                # Collapsed band gets its own score, not the consecutive
                # counter — see collapse_score. A healthy read decays it
                # instead of erasing it, so an alternating reading still
                # converges on a repair.
                self._collapse_score = collapse_score(
                    getattr(self, "_collapse_score", 0),
                    gate_ok=_gate_ok, css_w=_cw)
                # dormancy wake conditions: the gate recovering on its own, or
                # ANY navigation/tab-change (url flip) — both mean the world
                # the dud repairs gave up on is gone, so repairs re-arm.
                try:
                    _u = page.url or ""
                except Exception:  # noqa: BLE001
                    _u = ""
                if _gate_ok or _u != getattr(self, "_gate_url", _u):
                    if getattr(self, "_repair_dormant", False):
                        self._vp_log("repair-rearm",
                                     "gate ok" if _gate_ok else "nav")
                    self._repair_dormant = False
                    self._repair_duds = 0
                self._gate_url = _u
                # walker instrumentation: consecutive shrinking reads on a
                # static viewport self-document the next occurrence (the
                # 2026-07-23 hunt burned an hour without this trail)
                _pw_ = getattr(self, "_gate_prev_w", 0.0)
                if not _gate_ok and _cw and _pw_ and _cw < _pw_:
                    self._vp_log("vp-walk", f"{_pw_:.0f}->{_cw:.0f}")
                self._gate_prev_w = _cw
                if (not _gate_ok
                        and _cw < REPAIR_HARD_FLOOR_W
                        and not getattr(self, "_repair_dormant", False)
                        and self._collapse_score >= COLLAPSE_REPAIR_AT
                        and time.monotonic() - getattr(self, "_repair_ts", 0.0)
                        > getattr(self, "_repair_backoff", 5.0)):
                    # A switched target can inherit a collapsed/stale emulation
                    # viewport. Repair it on a persistent metrics session, then
                    # re-read before clipping this frame. THROTTLED + BACKED OFF
                    # + PERSISTENCE-GATED: if a repair doesn't change the reading
                    # (scrollbar under the floor, a foreign-session override we
                    # can't beat), retrying just clear+apply-reflows the REAL
                    # window — the visible GUI strobe. Worse, on scrollbar-heavy
                    # pages the repair reflow ITSELF perturbs the reading
                    # (films table 2026-07-22: the snap-to-1024 relaxes back to
                    # 964 in 12px steps over ~20s, and mid-walk transients dip
                    # under the floor → next repair → self-sustaining pulse).
                    # So: a failure must persist REPAIR_AFTER_MISSES consecutive
                    # frames (reflow transients never do; real collapsed
                    # emulation always does), a repair that leaves the gate
                    # failing DOUBLES the wait up to 60s, one that lands resets
                    # to the 5s base; attach/tab-switch repairs stay immediate
                    # (one-shot events).
                    self._gate_misses = 0
                    self._collapse_score = 0   # a fired repair re-earns its score
                    self._repair_ts = time.monotonic()
                    self._vp_log("repair", f"css {_cw:.0f}x{_ch:.0f} under floor")
                    await self._force_desktop_page(page, force=True)
                    _m = await asyncio.wait_for(
                        sess.send("Page.getLayoutMetrics"), timeout=1.0)
                    _css = ((_m or {}).get("cssLayoutViewport")
                            or (_m or {}).get("layoutViewport") or {})
                    _dev = ((_m or {}).get("layoutViewport")
                            or (_m or {}).get("cssLayoutViewport") or {})
                    _cw = float(_css.get("clientWidth") or 0)
                    _ch = float(_css.get("clientHeight") or 0)
                    if (self._accept_viewport(_cw, _ch)
                            and self._matches_view_metrics(_cw, _ch)):
                        self._repair_backoff = 5.0
                        self._repair_duds = 0
                    else:
                        self._repair_backoff = min(
                            getattr(self, "_repair_backoff", 5.0) * 2, 60.0)
                        # Ineffective repair → the cached metric session is the
                        # prime suspect: a session bound to a PRE-NAVIGATION
                        # (bfcache/frozen) target reads the old doc's geometry
                        # and its overrides never reach the live page — clear+
                        # apply then just perturbs the real page visibly every
                        # backoff period: the "zooms in then back out, viewport
                        # static" pulse (the owner 2026-07-23, live-diagnosed: the
                        # streamer's session read 708x634 while a fresh session
                        # on the same target read the true 1012x891). Drop the
                        # session so the next attempt rebuilds against the
                        # CURRENT target; after 3 dud repairs go DORMANT until
                        # a nav/tab-switch/attach resets the gate — a forever
                        # 60s pulse is itself the bug being chased.
                        self._repair_duds = getattr(self, "_repair_duds", 0) + 1
                        stale = self._metric_sessions.pop(page, None)
                        if stale is not None:
                            self._vp_log("repair-drop-session",
                                         f"dud #{self._repair_duds}")
                            try:
                                await stale.detach()
                            except Exception:  # noqa: BLE001
                                pass
                            if self._cdp is stale:
                                self._cdp = None
                        if self._repair_duds >= 3:
                            self._vp_log("repair-dormant",
                                         f"css {_cw:.0f}x{_ch:.0f} persists")
                            self._repair_dormant = True
                # REGIME-AWARE CLIP (2026-07-26 rev 2 — the "chin", then the
                # reset-view crop). Two capture regimes coexist on a
                # display-scaled Chrome (host WSLg 125%):
                #  * OUR OVERRIDE ACTIVE — the page lays out at
                #    cssLayoutViewport (override/1.25) and captureScreenshot
                #    renders it 1:1 CSS onto an override-sized canvas. The
                #    device clip pads a white right+bottom band (the letterbox
                #    chin); the CSS clip is exact (live-proven on chatgpt.com:
                #    device clip content-to (990,815) on a 1280x1020 canvas,
                #    CSS clip 1024x816 full-bleed).
                #  * NATIVE (no override — a PDF tab, or a page "Fix stuck
                #    zoom" swept and the live-page re-apply missed) — capture
                #    renders at device scale, so the CSS clip zooms+crops
                #    (the owner's google.com "urgh browser issues") and the device
                #    clip is the correct one.
                # Which regime? Our override is active exactly when the DEVICE
                # layout viewport equals the view target (that is what
                # setDeviceMetricsOverride pins); a native page reads the real
                # window size instead. Height only gates when view_h is
                # explicit. Click mapping is safe either way — the frontend
                # sends normalized (0..1) coords mapped to CSS px.
                _dw = float(_dev.get("clientWidth") or 0)
                _dh = float(_dev.get("clientHeight") or 0)
                # compare against what _apply_view_metrics ACTUALLY applied —
                # the scrollbar-deficit compensation can overshoot view_w
                _aw = float(getattr(self, "_applied_view_w", 0) or self.view_w)
                _emu = ((abs(_dw - self.view_w) <= 2 or abs(_dw - _aw) <= 2)
                        and (not self.view_h or abs(_dh - self.view_h) <= 2))
                if _emu:
                    # PAGE-ZOOM SPLIT (2026-07-26 rev 3 — google.com "cuts
                    # off"): the bot Chrome profile is display-scaled, so the
                    # css viewport reads view/scale while the capture canvas
                    # stays the full device size. Every reading below is CSS
                    # px and the clip is device px — _emu_clip_box owns that
                    # conversion, and the arithmetic is unit-tested there
                    # rather than inline against a live browser.
                    _bv = [0, 0]
                    try:
                        import json as _json
                        _ext = await asyncio.wait_for(
                            sess.send("Runtime.evaluate", {
                                "expression":
                                    "JSON.stringify(document.body?"
                                    "[document.body.scrollWidth,"
                                    "document.body.scrollHeight]:[0,0])",
                                "returnByValue": True}),
                            timeout=0.6)
                        _parsed = _json.loads(
                            (_ext.get("result") or {}).get("value") or "[0,0]")
                        # normalise INSIDE the guard: a page with no body
                        # answers `null`, and a subscript on that outside the
                        # try escapes to the outer handler, which drops the
                        # clip entirely and serves an unclipped frame
                        if isinstance(_parsed, list) and len(_parsed) >= 2:
                            _bv = _parsed
                    except Exception:  # noqa: BLE001 — extent read is best-effort
                        pass
                    _bw, _bh = _emu_clip_box(
                        css_w=_cw, css_h=_ch, dev_w=_dw, dev_h=_dh,
                        body_w=float(_bv[0] or 0), body_h=float(_bv[1] or 0),
                        page_x=float(_css.get("pageX") or 0),
                        page_y=float(_css.get("pageY") or 0),
                        view_w=float(self.view_w), view_h=float(self.view_h))
                    # device-space clip → device-space origin (same convention
                    # as the native branch below); a css origin here would
                    # under-scroll the capture by the display scale.
                    _src = _dev
                else:
                    _src, _bw, _bh = _dev, _dw, _dh
                if self._accept_viewport(_cw, _ch) and _bw and _bh:
                    _scale = min(1.0, TIER_LO_MAX_W / _bw) if lo else 1.0
                    # clip uses document coordinates, so its origin must follow
                    # the live viewport. At y=0 a scrolled page captures the
                    # offscreen region above it (a giant blank band on Yahoo).
                    args["clip"] = {"x": float(_src.get("pageX") or 0),
                                    "y": float(_src.get("pageY") or 0),
                                    "width": _bw, "height": _bh, "scale": _scale}
            except Exception:
                pass   # metrics unavailable → unclipped frame (full coverage on stock scale)
            res = await asyncio.wait_for(
                sess.send("Page.captureScreenshot", args),
                timeout=2.5)
            try:
                cr = await asyncio.wait_for(sess.send("Runtime.evaluate", {
                    "expression": "JSON.stringify(window.__opClick||null)",
                    "returnByValue": True}), timeout=0.6)
                val = (cr.get("result") or {}).get("value")
                if val and val != "null":
                    import json as _json
                    d = _json.loads(val)
                    if isinstance(d, dict) and "x" in d:
                        self.last_click = (float(d["x"]), float(d["y"]), time.monotonic())
            except Exception:
                pass
            return _b64.b64decode(res["data"])
        except Exception:
            self._cdp = None  # session may be stale (page nav) — rebuild next time
            try:
                return await asyncio.wait_for(
                    page.screenshot(
                        type="jpeg",
                        quality=TIER_LO_QUALITY if self.tier == "lo" else JPEG_QUALITY,
                        animations="disabled"),
                    timeout=2.5)
            except Exception:
                return None

    async def _pace(self, interval: float) -> None:
        """Sleep the capture interval, but wake IMMEDIATELY when an input action
        lands (F2): the interesting pixels appear in the first ~100ms after a
        click/keypress, and a fixed cadence could sit out a full interval before
        showing them. Idle cadence is untouched — the event only fires on actions."""
        if self._eager_evt is None:
            self._eager_evt = asyncio.Event()
        try:
            await asyncio.wait_for(self._eager_evt.wait(), timeout=interval)
        except asyncio.TimeoutError:
            return
        self._eager_evt.clear()

    async def _refresh_active_page(self) -> None:
        try:
            ctx = self._browser.contexts[0]
            live = self._live_pages(ctx)
            if not live:
                return
            switch_to = None
            # 1. current page gone → must switch
            if self._page is None or self._page.is_closed():
                switch_to = live[-1]
            else:
                # 2. follow the agent's ACTIVE tab: when a new tab appeared (the live
                # count grew) the agent almost certainly just opened+moved to it, so
                # stream that one. bounded by a count check so we don't churn per-frame.
                n = len(live)
                if n != getattr(self, "_live_n", n) and self._page is not live[-1]:
                    switch_to = live[-1]
                self._live_n = n
            if switch_to is not None and switch_to is not self._page:
                self._page = switch_to
                self._cdp = None
                self._update_viewport()
                await self._force_desktop_page(self._page)
        except Exception:  # noqa: BLE001
            pass

    async def _follow_active_tab(self) -> None:
        """Stream whichever tab the AGENT (or user) actually has in the FOREGROUND —
        not just the newest one. _refresh_active_page only switches when the tab COUNT
        changes (and always to the last tab), so an agent that flips between already-
        open tabs (clicks a link that activates an existing tab, or switches back to
        tab 1) left the view frozen on the stale tab (the owner 2026-06-30).

        Foreground is decided by the CDP target list (_active_target_id), NOT by
        document.visibilityState: this docstring used to claim "only the
        foreground tab reports 'visible'", which is simply false on the
        operator's Chrome — all tabs report 'visible', so the probe could never
        distinguish them. Believing it is why this bug survived three rounds of
        patching (2026-07-08 / -22 / -27). The visibility probe is kept only as
        a fallback for when the target list can't be read.

        Throttled (every ~0.8s) + bounded per check so it never stalls the grab
        loop, and only does work when there's >1 tab."""
        try:
            now = time.monotonic()
            if now - getattr(self, "_tab_check_ts", 0.0) < 0.8:
                return
            self._tab_check_ts = now
            ctx = self._browser.contexts[0]
            live = self._live_pages(ctx)
            try:
                _busy = operator_agent.runner.is_running()
            except Exception:  # noqa: BLE001
                _busy = False
            # AUTO-mode focus enforcement (the owner 2026-07-22): while a run is
            # live, the bot browser must SHOW the streamed (= agent's) tab at
            # all times. bring_to_front of an already-front tab is a cheap
            # activation no-op (no emulation, no reflow), so re-asserting it
            # snaps the GUI back within seconds if a stray click flipped the
            # real window to another tab mid-run — which otherwise also taught
            # the visibility-based follow below the WRONG foreground tab.
            if _busy and self._page is not None and not self._page.is_closed():
                if now - getattr(self, "_front_ts", 0.0) > 2.5:
                    self._front_ts = now
                    try:
                        await asyncio.wait_for(
                            self._page.bring_to_front(), timeout=0.5)
                    except Exception:  # noqa: BLE001 — best-effort
                        pass
            if len(live) < 2:
                return  # single tab → nothing to follow
            # ACTIVITY BEATS VISIBILITY (the owner 2026-07-08: "the view doesn't track
            # the tab the bot is using — some bots, not others"): agents drive
            # pages over CDP, which never foregrounds them — the MCP picks its
            # current tab at connect independent of Chrome's focus, and navigate/
            # click never activate a target (only the explicit tab tools do). So
            # a tab whose URL changed since the last poll is the one being DRIVEN;
            # follow it and bring it to front (which also keeps its renderer from
            # being background-throttled and makes later visibility polls agree).
            # ONLY while an agent run is live: outside a run, "URL activity" is
            # SPA churn in idle tabs (Google Travel pushStates on its own), and
            # yanking focus then kills in-page popups the USER is working with
            # (1Password's inline menu dies on blur) — and in manual mode a view
            # switch would re-aim the user's steer clicks at the wrong page.
            urls = {pg: pg.url for pg in live}
            prev = getattr(self, "_tab_urls", {})
            self._tab_urls = urls
            # drop target-id cache entries for closed tabs — bounds the dict to
            # the live tab count instead of every page the session ever opened
            if len(self._target_ids) > len(live):
                _liveset = set(live)
                for _dead in [k for k in self._target_ids if k not in _liveset]:
                    self._target_ids.pop(_dead, None)
            # A tab CREATED AND NAVIGATED between two polls has no prev entry,
            # so the url-diff never saw it — the agent's fresh MCP tab could
            # stream-shadow behind a stale one indefinitely (the owner 2026-07-27
            # "isn't displaying the tab the agent is working on at ALL
            # times"). While busy, a brand-new non-blank tab counts as a
            # mover too.
            moved = ([pg for pg in live
                      if (pg in prev and prev[pg] != urls[pg])
                      or (pg not in prev
                          and urls[pg] not in ("", "about:blank"))]
                     if _busy else [])
            if moved:
                pg = moved[-1]                       # most recently registered mover
                if pg is not self._page:
                    self._page = pg
                    self._cdp = None
                    self._update_viewport()
                    await self._force_desktop_page(self._page)
                    self._vp_log("tab-follow", (urls.get(pg) or "")[:48])
                try:
                    await asyncio.wait_for(pg.bring_to_front(), timeout=0.5)
                except Exception:  # noqa: BLE001 — foregrounding is best-effort
                    pass
                return
            # current page already visible? then don't churn.
            async def _vis(pg):
                try:
                    s = await asyncio.wait_for(
                        pg.evaluate("document.visibilityState"), timeout=0.5)
                    return s
                except Exception:
                    # JS world unavailable → fall back to CDP visibility metric.
                    # DETACH the throwaway session: this runs per pump
                    # iteration, and each leaked session is per-target CDP
                    # state Chrome keeps until browser disconnect (found while
                    # hunting the 2026-07-23 width walker — hundreds leaked on
                    # a busy 2-tab night).
                    sess = None
                    try:
                        sess = await pg.context.new_cdp_session(pg)
                        r = await asyncio.wait_for(
                            sess.send("Runtime.evaluate", {
                                "expression": "document.visibilityState",
                                "returnByValue": True}), timeout=0.5)
                        return (r.get("result") or {}).get("value")
                    except Exception:
                        return None
                    finally:
                        if sess is not None:
                            try:
                                await sess.detach()
                            except Exception:  # noqa: BLE001
                                pass
            # AUTHORITATIVE foreground check (the owner 2026-07-29: "the operator
            # browser doesn't focus on the tab the bot is working on is STILL
            # present"). The visibilityState probes below cannot answer this on
            # our Chrome — every tab reports 'visible' (see _active_target_id),
            # so the old `cur_vis == "visible"` early-return matched ALWAYS and
            # froze the view on whatever tab it happened to hold. That left the
            # url-diff heuristic above as the only mover, which by construction
            # misses a same-URL workload (clicking/typing/reading one SPA) —
            # precisely the trace the owner screenshotted.
            act = self._active_target_id()
            if act:
                cur_tid = await self._page_target_id(self._page) \
                    if self._page in live else None
                if cur_tid == act:
                    return                       # already on the real front tab
                for pg in live:
                    if pg is self._page:
                        continue
                    if await self._page_target_id(pg) == act:
                        self._page = pg
                        self._cdp = None
                        self._update_viewport()
                        await self._force_desktop_page(self._page)
                        self._vp_log("tab-follow-active",
                                     (urls.get(pg) or "")[:48])
                        return
                return   # front tab known but not ours to stream (devtools etc.)
            # CDP target list unreachable → fall back to the visibility probe.
            # Harmless where it works (a real focused window) and a no-op where
            # every tab claims visible, which is the pre-existing behavior.
            if self._page in live:
                cur_vis = await _vis(self._page)
                if cur_vis == "visible":
                    return
            for pg in reversed(live):   # prefer the newest visible one
                if pg is self._page:
                    continue
                if await _vis(pg) == "visible":
                    self._page = pg
                    self._cdp = None
                    self._update_viewport()
                    await self._force_desktop_page(self._page)
                    return
        except Exception:  # noqa: BLE001
            pass

    async def _reattach_soft(self) -> bool:
        """Swap to a live page in the SAME browser. Returns False if the browser
        connection itself is gone (caller then does a hard re-attach)."""
        try:
            ctx = self._browser.contexts[0]
            live = self._live_pages(ctx)
            if live:
                self._page = live[-1]
                self._cdp = None   # session was bound to the dead page — never dispatch input into it
                await self._force_desktop_page(self._page)
                return True
            return False
        except Exception:  # noqa: BLE001 — browser/context dropped
            return False

    async def _teardown(self) -> None:
        for closer in (lambda: self._browser and self._browser.close(),
                       lambda: self._pw and self._pw.stop()):
            try:
                r = closer()
                if asyncio.iscoroutine(r):
                    await r
            except Exception:  # noqa: BLE001
                pass
        self._page = self._browser = self._pw = None
        self._crashed_pages.clear()
        self._metric_sessions.clear()
        self._target_ids.clear()   # ids are per-browser — never reuse across attach
        # an error status (wedge, attach failure) must SURVIVE teardown — it
        # carries the user-facing message and keys the relaunch backoff
        if self.status != "error":
            self.status = "idle"

    async def _clear_emulation(self) -> dict:
        """Strip device-metrics + touch emulation overrides from EVERY page of
        the attached browser. Agent MCP sessions (and one-off browser_resize
        calls) leave CDP emulation on the real Chrome, and it OUTLIVES the
        client that set it: pages stay reflowed to the emulated size (the
        2026-07-10 "zoom spaz") and touch emulation kills wheel scrolling.
        Per-page best-effort; a page with no override is a harmless no-op."""
        cleared, failed = 0, 0
        try:
            ctx = self._browser.contexts[0]
            pages = self._live_pages(ctx)
        except Exception as e:  # noqa: BLE001 — browser gone/never attached
            return {"ok": False, "error": str(e), "cleared": 0, "failed": 0}
        for pg in pages:
            try:
                sess = await self._metric_session(pg)
                await asyncio.wait_for(
                    sess.send("Emulation.clearDeviceMetricsOverride"), timeout=1.5)
                # un-hide the scrollbars our apply hid (paired with
                # setScrollbarsHidden in _apply_view_metrics)
                try:
                    await asyncio.wait_for(sess.send(
                        "Emulation.setScrollbarsHidden", {"hidden": False}),
                        timeout=1.5)
                except Exception:  # noqa: BLE001
                    pass
                # Re-assert OUR phone-legible width — clearing alone drops the
                # page back to the native ~1349px canvas (the miniscule bug).
                # Only for the live page; other tabs just get de-emulated.
                if pg is self._page:
                    await self._apply_view_metrics(pg, sess)
                await asyncio.wait_for(
                    sess.send("Emulation.setTouchEmulationEnabled",
                              {"enabled": False}), timeout=1.5)
                cleared += 1
            except Exception:  # noqa: BLE001 — dead/privileged page, keep sweeping
                failed += 1
        return {"ok": True, "cleared": cleared, "failed": failed}

    # ---- tabs ------------------------------------------------------------
    def list_tabs(self) -> list:
        """Snapshot of open tabs (title/url/active). Runs on the loop thread."""
        if not self._running or self._loop is None:
            self.ensure_running()
            return []
        try:
            fut = asyncio.run_coroutine_threadsafe(self._list_tabs(), self._loop)
            return fut.result(timeout=6)
        except Exception:
            return []

    async def _list_tabs(self) -> list:
        try:
            ctx = self._browser.contexts[0]
            tabs = []
            for i, pg in enumerate(self._live_pages(ctx)):
                try:
                    title = await asyncio.wait_for(pg.title(), timeout=2)
                except Exception:
                    title = ""
                tabs.append({"i": i, "title": (title or pg.url or "tab")[:48],
                             "url": pg.url, "active": pg is self._page})
            return tabs
        except Exception:
            return []

    def switch_tab(self, idx: int) -> dict:
        if self._loop is None:
            return {"ok": False, "error": "not running"}
        try:
            fut = asyncio.run_coroutine_threadsafe(self._switch_tab(idx), self._loop)
            return fut.result(timeout=TAB_OP_TIMEOUT)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": _op_error(e)}

    async def _switch_tab(self, idx: int) -> dict:
        lock = self._iolock()
        await lock.acquire()
        try:
            return await self._switch_tab_locked(idx)
        finally:
            lock.release()

    async def _switch_tab_locked(self, idx: int) -> dict:
        try:
            ctx = self._browser.contexts[0]
            pages = self._live_pages(ctx)
            if 0 <= idx < len(pages):
                self._page = pages[idx]
                self._cdp = None
                await self._page.bring_to_front()
                self._update_viewport()
                await self._force_desktop_page(self._page)
                fresh = await self._grab(self._page)
                if fresh:
                    self._publish_frame(fresh)
                return {"ok": True}
            return {"ok": False, "error": "bad tab index"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    def close_tab(self, idx: int) -> dict:
        if self._loop is None: return {"ok": False, "error": "not running"}
        try:
            return asyncio.run_coroutine_threadsafe(self._close_tab(idx), self._loop).result(timeout=TAB_OP_TIMEOUT)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": _op_error(e)}

    async def _close_tab(self, idx: int) -> dict:
        lock = self._iolock()
        await lock.acquire()
        try:
            return await self._close_tab_locked(idx)
        finally:
            lock.release()

    async def _close_tab_locked(self, idx: int) -> dict:
        try:
            ctx = self._browser.contexts[0]
            pages = self._live_pages(ctx)
            if 0 <= idx < len(pages):
                closing = pages[idx]
                # never close the LAST tab — that kills the browser / leaves the
                # viewer with nothing + no way to reopen. Navigate it to Google.
                if len(pages) <= 1:
                    # last tab: don't close it (that kills the browser) — send it
                    # home instead. chrome://new-tab-page renders blank under
                    # headless+no-GPU (see _NEWTAB_HTML comment), so this is the
                    # configured landing page, navigated over raw CDP.
                    self._cdp = None
                    try:
                        await self._cdp_navigate(closing, _landing_url())
                    except Exception:  # noqa: BLE001
                        pass
                    self._page = closing; self._cdp = None; self._update_viewport()
                    await self._force_desktop_page(self._page)
                    return {"ok": True, "reset": True}
                # Raw CDP first — Playwright's page.close() is the other half of
                # the 8.19s hang measured 2026-08-07.
                if not await self._cdp_close_tab(closing):
                    await closing.close()
                live = self._live_pages(ctx)
                if not live:
                    # safety net: never leave zero tabs (that closes the browser) —
                    # open a fresh one so the demo/cockpit always has a live page.
                    try:
                        newp = await self._cdp_open_tab(_landing_url())
                        if newp is None:
                            newp = await ctx.new_page()
                            self._cdp = None
                            await self._cdp_navigate(newp, _landing_url())
                        live = [newp]
                    except Exception:  # noqa: BLE001
                        live = []
                if live:
                    self._page = live[-1]; self._cdp = None; self._update_viewport()
                    await self._page.bring_to_front()
                    await self._force_desktop_page(self._page)
                    fresh = await self._grab(self._page)
                    if fresh:
                        self._publish_frame(fresh)
                return {"ok": True}
            return {"ok": False, "error": "bad tab index"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    def new_tab(self) -> dict:
        if self._loop is None: return {"ok": False, "error": "not running"}
        try:
            return asyncio.run_coroutine_threadsafe(self._new_tab(), self._loop).result(timeout=TAB_OP_TIMEOUT)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": _op_error(e)}

    async def _new_tab(self) -> dict:
        lock = self._iolock()
        await lock.acquire()
        try:
            return await self._new_tab_locked()
        finally:
            lock.release()

    async def _new_tab_locked(self) -> dict:
        try:
            ctx = self._browser.contexts[0]
            url = _landing_url()
            # Fast path: raw CDP creates the target ALREADY on the landing
            # page, so there is no second navigate to wait on.
            pg = await self._cdp_open_tab(url)
            if pg is None:
                pg = await ctx.new_page()
                self._cdp = None
                await self._cdp_navigate(pg, url)
            self._page = pg; self._cdp = None; self._update_viewport()
            await self._force_desktop_page(self._page)
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    # ---- actions ---------------------------------------------------------
    # Driver-death fingerprints. When the Playwright node driver dies (e.g. the
    # uncaught CRPage frame-detach race, the owner 2026-08-10 01:33 on operator-fam),
    # every pending/future call fails with one of these. They mean the whole
    # attach is gone — only a full reattach helps, so they must never sit out
    # the abnormal-death backoff returning dead clicks.
    _DRIVER_DEAD_RE = re.compile(
        r"has been closed|connection closed|frame has been detached|"
        r"pipe closed|browser closed|not connected", re.IGNORECASE)

    def _force_reattach(self) -> None:
        """Driver confirmed dead: clear the backoff and relaunch NOW."""
        with self._lock:
            self._backoff_until = 0.0
            self._running = False
        self.ensure_running()

    def run_action(self, action: dict) -> dict:
        if not self._running or self._loop is None:
            self.ensure_running()
            time.sleep(0.5)
        loop = self._loop
        # A dead loop object survives an abnormal streamer death (_run never
        # nulls it), so actions used to be posted onto a stopped loop — the
        # future never resolved, every click burned the full 30s timeout, and
        # the cockpit read "browser disconnected" until a manual refresh
        # (the owner 2026-08-10). Refuse fast and relaunch instead.
        if loop is None or not loop.is_running():
            self._force_reattach()
            return {"ok": False, "error": "browser link lost — reconnecting"}
        fut = asyncio.run_coroutine_threadsafe(self._do_action(action), loop)
        try:
            result = fut.result(timeout=30)
        except Exception as e:  # noqa: BLE001
            if self._DRIVER_DEAD_RE.search(str(e) or type(e).__name__):
                self._force_reattach()
                return {"ok": False, "error": "browser link lost — reconnecting"}
            return {"ok": False, "error": str(e)}
        if (isinstance(result, dict) and not result.get("ok")
                and self._DRIVER_DEAD_RE.search(str(result.get("error", "")))):
            self._force_reattach()
            return {"ok": False, "error": "browser link lost — reconnecting"}
        return result

    def _safe_url(self, p) -> str:
        """p.url is a sync property but on a desynced connect_over_cdp page it can
        return '' (handle out of sync). Never raises; returns '' on trouble."""
        try:
            return p.url or ""
        except Exception:
            return ""

    async def _cdp_session(self, p):
        """Reusable CDP session for raw input/screenshot ops. Rebuilt if missing
        OR if the cache belongs to a DIFFERENT page than the one requested: a
        session is bound to one target, so after a page swap a stale cache
        dispatched clicks/keys into the old (background/closed) tab while the
        capture showed the new one — taps rippled, steers returned ok, and the
        visible page never reacted (the owner 2026-07-12, after a tab close). Most
        _page-swap sites null the cache manually; this identity check is the
        backstop so no future swap site can reintroduce the class."""
        sess = getattr(self, "_cdp", None)
        bound = getattr(self, "_cdp_for", None)
        if sess is None or bound is not p:
            # A send failure clears _cdp but leaves _cdp_for as the identity of
            # the failed target. Evict that one stale persistent session before
            # rebuilding; tab switches keep their already-healthy map entries.
            if sess is None and bound is p:
                self._metric_sessions.pop(p, None)
            sess = await self._metric_session(p)
            self._cdp = sess
            self._cdp_for = p
        return sess

    async def _metric_session(self, p):
        """Persistent per-target CDP session for emulation state.

        Device metrics are session-scoped: applying them on a temporary session
        and detaching immediately reverts the page. Keep one session alive per
        target and reuse that exact session for capture/input when the tab is
        active; a separate screenshot session would still see collapsed pixels.
        """
        sess = self._metric_sessions.get(p)
        if sess is None:
            sess = await p.context.new_cdp_session(p)
            self._metric_sessions[p] = sess
        return sess

    async def _force_desktop_page(self, p, force=False) -> None:
        """Clear stale mobile emulation and advertise a desktop browser.

        Operator is a persistent shared Chrome, so an earlier computer-use run
        can leave a target with mobile metrics/touch enabled. Apply this per
        target (including new tabs) so both responsive layout and HTTP UA stay
        desktop without changing the real window geometry or screenshot scale.
        """
        try:
            _u = ""
            try:
                _u = (p.url or "")[-48:]
            except Exception:  # noqa: BLE001
                pass
            self._vp_log("force-desktop", _u)
            sess = await self._metric_session(p)
            # Overwrite any STALE metrics an agent run / stray browser_resize
            # left with OUR deliberate phone-legible width (view_w).
            # setDeviceMetricsOverride REPLACES a prior override wholesale, so
            # no clear-first is needed when we're applying — and the old
            # unconditional clear+apply pair was the strobe: the clear reverts
            # the canvas to the window's native ~1349px for a frame before the
            # apply re-squeezes it, one visible size pulse per call, and this
            # runs on every attach/nav/tab-switch (a search→redirect chain =
            # 4 pulses in 2s, the 2026-07-26 "resize up and down" report).
            # Clear ONLY when view_w is falsy (native mode) — there apply
            # no-ops and the clear is the whole job.
            if self.view_w:
                await self._apply_view_metrics(p, sess, force=force)
            else:
                await asyncio.wait_for(
                    sess.send("Emulation.clearDeviceMetricsOverride"), timeout=3)
            await asyncio.wait_for(
                sess.send("Emulation.setTouchEmulationEnabled", {"enabled": False}), timeout=3)
            # Attaching over CDP flips every page to `prefers-color-scheme:
            # light` even though this Chrome (and Windows) run dark, so sites
            # rendered their light theme for the whole session. The browser is
            # fine — verified 2026-07-28 that a raw CDP eval reports dark
            # before a Playwright attach and light during it; Playwright pushes
            # its own emulation defaults. Clearing the override tested
            # unreliable, so force the value. Per-target and reset by re-attach,
            # which is exactly why it belongs here (runs on attach, new tab,
            # and nav) rather than once at startup.
            await asyncio.wait_for(sess.send("Emulation.setEmulatedMedia", {
                "features": [{"name": "prefers-color-scheme",
                              "value": _COLOR_SCHEME}]}), timeout=3)
            await asyncio.wait_for(sess.send("Emulation.setUserAgentOverride", {
                "userAgent": _DESKTOP_USER_AGENT,
                "acceptLanguage": "en-US,en;q=0.9",
                "platform": "Win32",
                "userAgentMetadata": {
                    "brands": [
                        {"brand": "Chromium", "version": "150"},
                        {"brand": "Google Chrome", "version": "150"},
                        {"brand": "Not_A Brand", "version": "99"},
                    ],
                    "fullVersionList": [
                        {"brand": "Chromium", "version": "150.0.0.0"},
                        {"brand": "Google Chrome", "version": "150.0.0.0"},
                        {"brand": "Not_A Brand", "version": "99.0.0.0"},
                    ],
                    "platform": "Windows",
                    "platformVersion": "10.0.0",
                    "architecture": "x86",
                    "model": "",
                    "mobile": False,
                    "bitness": "64",
                    "wow64": False,
                },
            }), timeout=3)
        except Exception:
            pass

    async def _apply_view_metrics(self, p, sess=None, force=False) -> None:
        """Force the layout viewport to view_w CSS px so a desktop page reflows
        to a phone-legible width. mobile=False keeps DESKTOP layouts (we want the
        full site, just narrower — not the mobile skin). height/deviceScaleFactor
        0 = auto (keep the real window height and display density). No-op when
        view_w is falsy (native width).

        PDF tabs ("auto-resize not working", the owner 2026-07-22, DS-11): Chrome's
        PDF viewer is a plugin surface — setDeviceMetricsOverride applies
        without error but neither the layout nor the captured frame changes
        (live-proven: apply logged 1280x1074, frame stayed 1024x859). The only
        thing that reflows a PDF is the REAL window, so for .pdf pages resize
        the window via Browser.setWindowBounds instead. Web tabs still get
        emulation (which overrides window size for them), so the bounds change
        is invisible everywhere except the PDF itself."""
        if not self.view_w:
            return
        url = ""
        try:
            url = (p.url or "").split("?", 1)[0].lower()
        except Exception:  # noqa: BLE001
            pass
        try:
            sess = sess or await self._metric_session(p)
            if url.endswith(".pdf"):
                win = await asyncio.wait_for(
                    sess.send("Browser.getWindowForTarget"), timeout=3)
                await asyncio.wait_for(sess.send("Browser.setWindowBounds", {
                    "windowId": win["windowId"],
                    "bounds": {"width": int(self.view_w),
                               # + chrome above the viewport (tab strip/urlbar):
                               # bounds are the OUTER window, the viewport runs
                               # ~90px shorter. Close enough for aspect-fit.
                               "height": int((self.view_h or 800)) + 90,
                               "windowState": "normal"},
                }), timeout=3)
                self._vp_log("apply-pdf-window",
                             f"{int(self.view_w)}x{int(self.view_h or 0)}")
                return
            # APPLY STORM GUARD (the owner 2026-07-27 "window still randomly
            # resizes, esp when I navigate away"): SPA-heavy pages fire
            # several frameNavigated events per nav, and each force-desktop
            # re-applied the override — with the scrollbar compensation's
            # second apply on top, one navigation produced a visible burst of
            # resize pulses. Skip if the SAME target hit the SAME page within
            # the last 1.5s.
            _now = time.monotonic()
            _key = (id(p), int(self.view_w), int(self.view_h or 0))
            if (not force
                    and getattr(self, "_apply_seen", None) == _key
                    and _now - getattr(self, "_apply_seen_ts", 0.0) < 1.5):
                return
            self._apply_seen = _key
            self._apply_seen_ts = _now
            # apply the CACHED compensated width in ONE shot when known — the
            # measure-then-reapply dance below is for first contact only, so
            # routine navs get exactly one silent apply.
            _w_apply = int(getattr(self, "_sb_comp", {}).get(
                int(self.view_w), self.view_w))
            await asyncio.wait_for(sess.send("Emulation.setDeviceMetricsOverride", {
                "width": _w_apply, "height": int(self.view_h or 0),
                "deviceScaleFactor": 0, "mobile": False,
                "screenWidth": _w_apply,
                "screenHeight": int(self.view_h or 1400),
            }), timeout=3)
            self._applied_view_w = _w_apply
            # SCROLLBAR DEFICIT (the owner 2026-07-27 "black bars left/right"): a
            # visible vertical scrollbar shaves ~15px off cssLayoutViewport, so
            # frames came back a sliver narrower than the beaconed stage and
            # object-fit pillarboxed them. setScrollbarsHidden is a no-op on a
            # GUI Chrome (live-tested — the classic scrollbar keeps its layout
            # gutter), so instead: measure the shave and OVERSHOOT the override
            # proportionally so the css viewport nets the full target. The
            # adjusted width is remembered in _applied_view_w — _grab's regime
            # detection must compare the device viewport against what we
            # actually applied, not the stage target.
            try:
                # first contact for this view target only — once the deficit is
                # cached, navs skip the measure entirely (no second apply)
                if int(self.view_w) in getattr(self, "_sb_comp", {}):
                    return
                # let the override's reflow land — measured immediately, the
                # css viewport still reads the pre-scrollbar width and the
                # deficit shows as 0 (live-caught 2026-07-27)
                await asyncio.sleep(0.15)
                _m = await asyncio.wait_for(
                    sess.send("Page.getLayoutMetrics"), timeout=2)
                # DEVICE layout viewport, not css: the scrollbar shaves its
                # ~15px in device units BEFORE the page zoom divides into css
                # (override 1280 → device 1265 → css 1265/zoom). Measuring css
                # mixed the zoom into the deficit and blew past the gate.
                _dw = float((_m.get("layoutViewport") or {})
                            .get("clientWidth") or 0)
                _deficit = self.view_w - _dw
                if 2 < _deficit <= 40 and _dw > 0:
                    _w2 = int(self.view_w + _deficit)
                    await asyncio.wait_for(
                        sess.send("Emulation.setDeviceMetricsOverride", {
                            "width": _w2, "height": int(self.view_h or 0),
                            "deviceScaleFactor": 0, "mobile": False,
                            "screenWidth": _w2,
                            "screenHeight": int(self.view_h or 1400),
                        }), timeout=3)
                    self._applied_view_w = _w2
                    if not hasattr(self, "_sb_comp"):
                        self._sb_comp = {}
                    self._sb_comp[int(self.view_w)] = _w2
                    self._vp_log("apply-sb-comp",
                                 f"css {_cw:.0f} deficit {_deficit:.0f} -> {_w2}")
            except Exception:  # noqa: BLE001 — compensation is best-effort
                pass
            # logged AFTER the send: the recorder previously logged intent
            # before a swallowed failure, which read as "applied fine" during
            # the 2026-07-23 stale-session hunt and pointed the wrong way.
            self._vp_log("apply", f"{int(self.view_w)}x{int(self.view_h or 0)}")
        except Exception as e:  # noqa: BLE001
            self._vp_log("apply-failed", str(e)[:60])

    async def _cdp_click(self, p, x: float, y: float, button: str = "left",
                         clicks: int = 1, ramp: bool = True) -> None:
        """Click at CSS-px (x,y) via raw CDP Input.dispatchMouseEvent, bypassing
        Playwright's high-level page.mouse (which blocks indefinitely on a desynced
        connect_over_cdp handle). Each op is timeout-bounded so a wedged page can
        never hold _io_lock and freeze the grab loop. Also stamps last_click so the
        UI cursor overlay shows even if the page's own __opClick JS hook is slow."""
        sess = await self._cdp_session(p)
        async def _send(typ, **extra):
            args = {"type": typ, "x": float(x), "y": float(y)}
            args.update(extra)
            await asyncio.wait_for(sess.send("Input.dispatchMouseEvent", args), timeout=4)
        # glide a couple of moves in so it isn't a zero-movement instant click
        await _send("mouseMoved")
        await asyncio.sleep(0.02)
        if ramp:
            # programmatic multi-click (agent dblclick): synthesize the FULL
            # sequence — press/release 1, press/release 2, … up to `clicks`.
            for n in range(1, clicks + 1):
                await _send("mousePressed", button=button, clickCount=n)
                await asyncio.sleep(0.03)
                await _send("mouseReleased", button=button, clickCount=n)
        else:
            # incremental user multi-click: each physical click of a burst arrives
            # as its own steer with the native detail count. The earlier clicks in
            # the burst were already dispatched (clickCount 1, 2, …), so send ONLY
            # the nth press/release — Chrome's input pipeline turns clickCount=2/3
            # into the page's dblclick / word-select / paragraph-select behavior.
            await _send("mousePressed", button=button, clickCount=clicks)
            await asyncio.sleep(0.03)
            await _send("mouseReleased", button=button, clickCount=clicks)
        # stamp the cursor overlay from the normalized coords we were handed
        try:
            d = await self._viewport_dims(p)
            if d.get("w") and d.get("h"):
                self.last_click = (x / d["w"], y / d["h"], time.monotonic())
        except Exception:
            pass

    # in-page overlay JS: if the element at (x,y) is a native <select>, replace
    # its unreachable OS popup with a DOM list the CDP click CAN hit. Toggles:
    # a second click on the same select closes it. Returns true iff it acted on
    # a select. Self-contained; leaves no trace on non-selects.
    _SELECT_SHIM_JS = r"""
    (function(px, py){
      var OVID = '__opSelectOverlay';
      var old = document.getElementById(OVID);
      var el = document.elementFromPoint(px, py);
      // a click ON the overlay is handled by its own listeners — report handled
      if (el && el.closest && el.closest('#'+OVID)) return true;
      // close any open overlay when clicking elsewhere / re-clicking the select
      if (old) { old.remove(); }
      var sel = el && el.closest ? el.closest('select') : null;
      if (!sel || sel.disabled || sel.multiple) return false;
      var r = sel.getBoundingClientRect();
      var ov = document.createElement('div');
      ov.id = OVID;
      ov.style.cssText = 'position:fixed;z-index:2147483647;left:'+r.left+'px;top:'+
        (r.bottom)+'px;min-width:'+r.width+'px;max-height:60vh;overflow:auto;'+
        'background:#fff;color:#111;border:1px solid rgba(0,0,0,.25);'+
        'border-radius:6px;box-shadow:0 6px 24px -6px rgba(0,0,0,.5);'+
        'font:14px/1.4 system-ui,sans-serif;padding:4px 0;';
      // keep it on-screen if the select is low in the viewport
      if (r.bottom + 200 > window.innerHeight && r.top > window.innerHeight/2) {
        ov.style.top = ''; ov.style.bottom = (window.innerHeight - r.top)+'px';
      }
      Array.prototype.forEach.call(sel.options, function(opt, i){
        var row = document.createElement('div');
        row.textContent = opt.textContent;
        row.style.cssText = 'padding:6px 14px;cursor:pointer;white-space:nowrap;'+
          (i === sel.selectedIndex ? 'background:#e8f0fe;font-weight:600;' : '');
        row.addEventListener('mouseenter', function(){ row.style.background = '#eef1f5'; });
        row.addEventListener('mouseleave', function(){ row.style.background = (i===sel.selectedIndex?'#e8f0fe':''); });
        row.addEventListener('mousedown', function(ev){
          ev.preventDefault(); ev.stopPropagation();
          if (sel.selectedIndex !== i) {
            sel.selectedIndex = i;
            sel.dispatchEvent(new Event('input', {bubbles:true}));
            sel.dispatchEvent(new Event('change', {bubbles:true}));
          }
          ov.remove();
        }, true);
        ov.appendChild(row);
      });
      document.documentElement.appendChild(ov);
      return true;
    })
    """

    async def _maybe_open_select(self, p, x: float, y: float) -> bool:
        """If (x,y) is over a native <select>, show an in-page clickable option
        overlay (the OS popup is unreachable over CDP) and return True. Returns
        False for anything else so the normal click path runs."""
        try:
            sess = await self._cdp_session(p)
            res = await asyncio.wait_for(sess.send("Runtime.evaluate", {
                "expression": f"({self._SELECT_SHIM_JS})({float(x)},{float(y)})",
                "returnByValue": True}), timeout=2.0)
            return bool((res.get("result") or {}).get("value"))
        except Exception:
            return False

    _PRIVILEGED_ACTIVATE_JS = r"""
    (function(px, py){
      var hits = [];
      function walk(root, depth){
        var controls = [];
        try {
          controls = root.querySelectorAll(
            'a[href],button,[role="button"],[role="link"]');
        } catch (_) {}
        for (var i = 0; i < controls.length; i++) {
          var el = controls[i], r = el.getBoundingClientRect();
          if (r.width > 0 && r.height > 0 && px >= r.left && px <= r.right &&
              py >= r.top && py <= r.bottom) {
            hits.push({el: el, depth: depth, area: r.width * r.height});
          }
        }
        var all = [];
        try { all = root.querySelectorAll('*'); } catch (_) {}
        for (var j = 0; j < all.length; j++) {
          if (all[j].shadowRoot) walk(all[j].shadowRoot, depth + 1);
        }
      }
      walk(document, 0);
      if (!hits.length) return false;
      hits.sort(function(a, b){
        return (b.depth - a.depth) || (a.area - b.area);
      });
      hits[0].el.click();
      return true;
    })
    """

    async def _maybe_activate_privileged_control(
            self, p, x: float, y: float) -> bool:
        """Activate controls in Chrome's privileged NTP shadow tree.

        Chrome accepts CDP mouse events on this page but does not run shortcut
        activation (the click merely selects its label). Ordinary web pages
        stay on the trusted raw-input path.
        """
        url = self._safe_url(p)
        if not (url.startswith("chrome://new-tab-page/")
                or url.startswith("chrome://newtab/")):
            return False
        try:
            sess = await self._cdp_session(p)
            res = await asyncio.wait_for(sess.send("Runtime.evaluate", {
                "expression": (
                    f"({self._PRIVILEGED_ACTIVATE_JS})"
                    f"({float(x)},{float(y)})"),
                "returnByValue": True,
            }), timeout=2.0)
            return bool((res.get("result") or {}).get("value"))
        except Exception:
            return False

    async def _cdp_scroll(self, p, dx: float, dy: float) -> None:
        """Scroll via raw CDP Input.dispatchMouseEvent(type=mouseWheel), NOT
        Playwright's page.mouse.wheel(). Same reason clicks use raw CDP: on a
        connect_over_cdp handle the high-level page.mouse.wheel() SILENTLY
        NO-OPS — it returns without error but dispatches nothing, so the page
        never moves ("scroll up/down randomly broke", the owner 2026-07-12; verified:
        p.mouse.wheel left scrollY at 0, a raw mouseWheel at the same delta
        moved it). Wheel events need a position → dispatch at the viewport
        centre, which is where a real trackpad/wheel gesture over the page
        lands. Timeout-bounded so a wedged page can't hold _io_lock."""
        sess = await self._cdp_session(p)
        try:
            d = await self._viewport_dims(p)
            cx = float(d.get("w") or 800) / 2.0
            cy = float(d.get("h") or 600) / 2.0
        except Exception:
            cx, cy = 400.0, 300.0
        await asyncio.wait_for(sess.send("Input.dispatchMouseEvent", {
            "type": "mouseWheel", "x": cx, "y": cy,
            "deltaX": float(dx or 0), "deltaY": float(dy or 0)}), timeout=4)

    async def _browser_cdp(self):
        """Browser-level CDP session, for the Target.* domain.

        Page sessions cannot create or close targets — those live on the
        browser. Cached like _cdp and dropped on any failure, since a session
        that has gone bad stays bad.
        """
        sess = getattr(self, "_browser_sess", None)
        if sess is None:
            sess = await asyncio.wait_for(
                self._browser.new_browser_cdp_session(), timeout=4)
            self._browser_sess = sess
        return sess

    async def _target_id(self, p) -> str:
        """CDP targetId for a Playwright page, asked of the page's own session."""
        sess = await self._cdp_session(p)
        info = await asyncio.wait_for(sess.send("Target.getTargetInfo"), timeout=3)
        return ((info or {}).get("targetInfo") or {}).get("targetId") or ""

    async def _cdp_open_tab(self, url: str):
        """Open a tab over raw CDP and hand back the Playwright page for it.

        Playwright's ctx.new_page() is the call that measured 8.19s on this
        streamer's aged connection (0.078s on a fresh one, 0.022s over raw
        CDP) — the same page-lifecycle desync this file routes clicks, keys and
        navigation around. Returns None if CDP refuses, so the caller can fall
        back rather than leaving the user with no tab.
        """
        before = set(self._browser.contexts[0].pages)
        try:
            sess = await self._browser_cdp()
            res = await asyncio.wait_for(
                sess.send("Target.createTarget", {"url": url}), timeout=5)
        except Exception:  # noqa: BLE001
            self._browser_sess = None
            return None
        if not (res or {}).get("targetId"):
            return None
        # Playwright learns about the target on its own event stream, which
        # lands a beat after the command returns. Take the page that APPEARED
        # rather than asking each one its targetId: that would be a CDP
        # round-trip per open tab, and this runs under the io lock so no other
        # operator-initiated open can be interleaving with it.
        for _ in range(40):
            for pg in self._browser.contexts[0].pages:
                if pg not in before and not pg.is_closed():
                    return pg
            await asyncio.sleep(0.05)
        return None

    async def _cdp_close_tab(self, p) -> bool:
        """Close a tab over raw CDP. False means the caller should fall back."""
        try:
            tid = await self._target_id(p)
            if not tid:
                return False
            sess = await self._browser_cdp()
            await asyncio.wait_for(
                sess.send("Target.closeTarget", {"targetId": tid}), timeout=5)
        except Exception:  # noqa: BLE001
            self._browser_sess = None
            return False
        # The page object goes closed on Playwright's event stream, not on the
        # command's return; give it a moment so the caller's live-page scan
        # does not still see it.
        for _ in range(20):
            if p.is_closed():
                break
            await asyncio.sleep(0.05)
        return True

    async def _cdp_navigate(self, p, url: str, timeout: float = 4) -> None:
        """Navigate via raw CDP Page.navigate, bypassing Playwright's
        page.goto()/set_content() (which wait on lifecycle events that can hang
        indefinitely on a desynced connect_over_cdp handle — same bug class as
        page.mouse/page.evaluate, see _cdp_click). Caller must null self._cdp
        first if p differs from the page the cached session is bound to."""
        sess = await self._cdp_session(p)
        await asyncio.wait_for(sess.send("Page.navigate", {"url": url}), timeout=timeout)

    async def _viewport_dims(self, p):
        """CSS-pixel viewport {w,h} for mapping normalized click coords. Uses CDP
        getLayoutMetrics (immune to page eval-blocking, e.g. Amex CSP). Falls back
        to page.evaluate, then to the cached streamer dims."""
        # 1) CDP — works even when the page disables eval()
        try:
            sess = await self._cdp_session(p)
            for attempt in range(2):
                m = await asyncio.wait_for(sess.send("Page.getLayoutMetrics"), timeout=3)
                vp = m.get("cssLayoutViewport") or m.get("layoutViewport") or {}
                w = vp.get("clientWidth"); h = vp.get("clientHeight")
                if self._usable_click_basis(w, h):
                    return {"w": float(w), "h": float(h)}
                if attempt == 0:
                    await self._force_desktop_page(p)
        except Exception:
            self._cdp = None
        # 2) page eval (works on normal sites)
        try:
            d = await p.evaluate("({w: window.innerWidth, h: window.innerHeight})")
            if self._usable_click_basis(d.get("w"), d.get("h")):
                return d
        except Exception:
            pass
        # 3) last resort: the dims the screenshot frame was captured at
        if self.vw and self.vh:
            return {"w": self.vw, "h": self.vh}
        return {"w": 1280, "h": 800}

    async def _do_action(self, action: dict) -> dict:
        if self._page is None:
            return {"ok": False, "error": "no page attached"}
        kind = action.get("kind")
        val = action.get("value", "")
        p = self._page
        _lk = self._iolock()
        await _lk.acquire()
        try:
            if kind == "goto":
                url = val if "://" in val else f"https://{val}"
                await p.goto(url, wait_until="domcontentloaded", timeout=20000)
            elif kind == "click":                       # click by visible text
                await p.get_by_text(val, exact=False).first.click(timeout=8000)
            elif kind in ("click_at", "dblclick_at"):   # (double-)click at normalized x,y
                # CDP-attached Chrome reports viewport_size=None, so mouse.click
                # must scale against the LIVE CSS-pixel viewport (window.inner*),
                # which is also exactly what the screenshot frame covers.
                dims = await self._viewport_dims(p)
                x = float(action.get("x", 0)) * dims["w"]
                y = float(action.get("y", 0)) * dims["h"]
                # Drive the click via RAW CDP Input.dispatchMouseEvent, not
                # p.mouse.*. With Playwright 1.60 + headless Chrome the connect_over_cdp
                # page wrapper intermittently desyncs (url=='' , JS world dead) — its
                # high-level mouse/evaluate/title calls then BLOCK with no timeout,
                # holding _io_lock and freezing the grab loop (the "click crashes the
                # feed, no cursor" bug). Raw CDP bypasses the broken page model — it's
                # the same layer _grab uses for screenshots, which never broke.
                # `count` = native multi-click detail from the cockpit (1 single,
                # 2 double, 3 triple → sentence/paragraph select). Sent per physical
                # click, so dispatch it incrementally (ramp=False). The agent's
                # dblclick_at carries no count and keeps the full ramped sequence.
                # NATIVE <select> shim (the owner 2026-07-21): a real click on a
                # <select> opens an OS-drawn popup that raw CDP mouse events
                # can't reach — the option list isn't in the page, so the
                # follow-up option-click hits nothing and the value never
                # changes. (Regressed when clicks moved from Playwright's
                # high-level page.mouse — which drove selects via the a11y tree
                # — to raw Input.dispatchMouseEvent for the connect_over_cdp
                # wedge fix.) On-demand only: if THIS click lands on a select,
                # render an in-page, CDP-clickable option overlay instead of
                # firing the dead native popup. Non-selects are untouched.
                if kind == "click_at" and await self._maybe_open_select(p, x, y):
                    _u = self._safe_url(p); self.cur_url = _u or self.cur_url
                    return {"ok": True, "url": _u, "px": [round(x), round(y)],
                            "select": True}
                if (kind == "click_at"
                        and await self._maybe_activate_privileged_control(p, x, y)):
                    _u = self._safe_url(p); self.cur_url = _u or self.cur_url
                    return {"ok": True, "url": _u,
                            "px": [round(x), round(y)], "activated": True}
                cnt = action.get("count")
                try:
                    cnt = max(1, min(4, int(cnt))) if cnt is not None else None
                except (TypeError, ValueError):
                    cnt = None
                if cnt is not None:
                    await self._cdp_click(p, x, y, button="left",
                                          clicks=cnt, ramp=False)
                else:
                    clicks = 2 if kind == "dblclick_at" else 1
                    await self._cdp_click(p, x, y, button="left", clicks=clicks)
                _u = self._safe_url(p); self.cur_url = _u or self.cur_url
                return {"ok": True, "url": _u, "px": [round(x), round(y)]}
            elif kind == "rclick_at":              # right-click at normalized x,y (context menu)
                dims = await self._viewport_dims(p)
                x = float(action.get("x", 0)) * dims["w"]
                y = float(action.get("y", 0)) * dims["h"]
                await self._cdp_click(p, x, y, button="right", clicks=1)
            elif kind == "move":                   # hover at normalized x,y (menus, tooltips)
                dims = await self._viewport_dims(p)
                x = float(action.get("x", 0)) * dims["w"]
                y = float(action.get("y", 0)) * dims["h"]
                sess = await self._cdp_session(p)
                await asyncio.wait_for(
                    sess.send("Input.dispatchMouseEvent",
                              {"type": "mouseMoved", "x": x, "y": y}), timeout=4)
            elif kind == "type":
                await p.keyboard.type(val, delay=35)
            elif kind == "key":
                await p.keyboard.press(val or "Enter")
            elif kind == "key_down":
                # HELD-KEY AUTO-REPEAT. Playwright's keyboard.down() fires ONE keydown and
                # does NOT auto-repeat like a physically-held key — so a held arrow scrolled
                # once, not continuously. Instead we simulate OS key-repeat: press once now,
                # then a background task re-presses every ~45ms until key_up. Each press is
                # a real down+up so the page scrolls/navigates each tick.
                key = val or "Enter"
                if self._key_repeat is None:
                    self._key_repeat = {}
                old = self._key_repeat.pop(key, None)
                if old:
                    old.cancel()
                await p.keyboard.press(key)                 # immediate first tick
                self._key_repeat[key] = asyncio.ensure_future(self._repeat_key(key))
            elif kind == "key_up":            # stop the held-key repeat
                key = val or "Enter"
                if self._key_repeat:
                    t = self._key_repeat.pop(key, None)
                    if t:
                        t.cancel()
            elif kind == "scroll":
                # numeric dy/dx → precise user wheel/touch scroll; else keyword amounts.
                # Raw CDP mouseWheel (see _cdp_scroll) — p.mouse.wheel() no-ops
                # on connect_over_cdp handles.
                dx = action.get("dx"); dy = action.get("dy")
                if isinstance(dy, (int, float)) or isinstance(dx, (int, float)):
                    await self._cdp_scroll(p, float(dx or 0), float(dy or 0))
                else:
                    amt = {"up": -600, "down": 600, "top": -100000,
                           "bottom": 100000}.get(val, 600)
                    await self._cdp_scroll(p, 0, amt)
            elif kind == "back":
                # wait_until="commit" returns as soon as the navigation COMMITS (not
                # full load), so we don't hold the io-lock for up to 15s while the page
                # loads — that lock starves the grab loop and froze/broke the feed on
                # back/forward (the owner). The feed then streams the new page as it loads.
                await p.go_back(wait_until="commit", timeout=8000)
            elif kind == "forward":
                await p.go_forward(wait_until="commit", timeout=8000)
            elif kind == "reload":
                await p.reload(wait_until="commit", timeout=8000)
            elif kind in ("mousedown_at", "mouseup_at"):  # press-and-hold (captchas)
                dims = await self._viewport_dims(p)
                x = float(action.get("x", 0)) * dims["w"]
                y = float(action.get("y", 0)) * dims["h"]
                if kind == "mousedown_at":
                    await p.mouse.move(x, y, steps=6)
                    await p.mouse.down()
                else:
                    await p.mouse.up()
                _u = self._safe_url(p); self.cur_url = _u or self.cur_url
                return {"ok": True, "url": _u, "px": [round(x), round(y)]}
            elif kind == "drag":          # atomic click-drag: down at (x0,y0) → up at (x1,y1)
                dims = await self._viewport_dims(p)
                x0 = float(action.get("x0", 0)) * dims["w"]
                y0 = float(action.get("y0", 0)) * dims["h"]
                x1 = float(action.get("x1", 0)) * dims["w"]
                y1 = float(action.get("y1", 0)) * dims["h"]
                await p.mouse.move(x0, y0, steps=4)
                await p.mouse.down()
                await asyncio.sleep(0.05)
                await p.mouse.move(x1, y1, steps=20)   # glide so the page sees a drag
                await asyncio.sleep(0.05)
                await p.mouse.up()
                _u = self._safe_url(p); self.cur_url = _u or self.cur_url
                return {"ok": True, "url": _u, "px": [round(x1), round(y1)]}
            elif kind == "find":          # ⌘F find-on-page
                await p.keyboard.press("Control+f")
            elif kind == "select_all":
                await p.keyboard.press("Control+a")
            elif kind == "zoom":                        # browser zoom in/out/reset
                # synthetic Ctrl+/- didn't visibly zoom a CDP-driven Chrome, so apply
                # a CSS zoom on the document instead (reliable, captured by the feed,
                # re-applied after each navigation via the init script below).
                if val == "in":
                    self.zoom = min(3.0, round(self.zoom + 0.1, 2))
                elif val == "out":
                    self.zoom = max(0.3, round(self.zoom - 0.1, 2))
                else:
                    self.zoom = 1.0
                try:
                    await p.evaluate(f"document.documentElement.style.zoom = '{self.zoom}'")
                except Exception:
                    pass
            elif kind == "stage_size":
                # Smart viewport follow: the viewer's stage size (CSS px, sent as
                # "WxH" in value) becomes the remote layout viewport, clamped —
                # the captured frame then matches the stage aspect exactly (no
                # letterbox on any device). Applied immediately when idle; during
                # a live run only STORED (mid-run frame size stays stable for the
                # agent) — the run-end emulation sweep re-asserts the latest via
                # _apply_view_metrics. Gated: demo always, prod via env.
                if not _VIEW_FOLLOW:
                    return {"ok": False, "ignored": "viewport follow disabled"}
                try:
                    w_s, h_s = str(val).lower().split("x", 1)
                    w, h = int(float(w_s)), int(float(h_s))
                except Exception:
                    return {"ok": False, "error": "stage_size wants value=WxH"}
                # Preserve a desktop-class responsive layout even when the
                # viewer is narrow. Scale height with the width floor so the
                # captured frame keeps the stage aspect on tablets. Shared
                # with the page-load seed so a hint and a beacon can never
                # disagree about what the same stage means.
                w, h = _clamp_stage(w, h)
                if not w or not h:
                    return {"ok": False, "error": "stage_size wants value=WxH"}
                cid = str(action.get("cid") or "")
                if not self.vp_beacon_allowed(cid):
                    # another viewer OWNS the aspect and is actively pulling
                    # frames — refuse, or a background tab resuming (or any
                    # probe) zooms the shared browser under the real viewer.
                    self._vp_log("beacon-refused", f"{cid or 'anon'} wanted {w}x{h}")
                    return {"ok": True, "view": [self.view_w, self.view_h],
                            "applied": False, "owned": True}
                self._vp_owner = cid or "anon"   # anon can't hold it (no liveness)
                self.vp_note_pull(cid)
                force = action.get("force") is True
                same_target = (w, h) == (self.view_w, self.view_h)
                if same_target and not force:
                    return {"ok": True, "view": [w, h], "applied": False}
                self._vp_log("beacon-force" if same_target else "beacon",
                             f"{cid or 'anon'} {val} -> {w}x{h}")
                self.view_w, self.view_h = w, h
                busy = False
                try:
                    busy = operator_agent.runner.is_running()
                except Exception:
                    pass
                # IMMEDIATE APPLY (restored 2026-07-22). The dead-band +
                # rate-limit machinery that lived here treated symptoms of a
                # CLIENT-side feedback loop: 4f09e2b put a ResizeObserver on
                # the stage, so layout shifts the cockpit caused ITSELF
                # (scrollbar toggles, rail animation, frame swaps) beaconed
                # back as "resizes" and re-reflowed the remote page — the
                # strobe. The observer is gone (beacons are user-driven only:
                # window resize / rail-drag end, 600ms debounce), so a beacon
                # here is a real resize again and applying it immediately is
                # exactly the responsive follow the feature shipped with.
                if not busy:
                    await self._apply_view_metrics(p)
                    # Prime the buffer under the same I/O lock before the action
                    # returns. The pump therefore swaps old-layout pixels
                    # directly for the final viewport instead of catching an
                    # intermediate responsive/mobile frame.
                    fresh = await self._grab(p)
                    if fresh:
                        self._publish_frame(fresh)
                return {"ok": True, "view": [w, h], "applied": not busy}
            elif kind == "reset_view":
                # strip emulation overrides an agent run (or a stray
                # browser_resize) left on the shared browser — the "stuck
                # phone-zoom + dead scrolling" recovery, one tap from the menu
                res = await self._clear_emulation()
                return res
            elif kind == "extensions":
                await self._cdp_navigate(p, "chrome://extensions/")
            elif kind == "hard_reload":
                await p.reload(timeout=20000)
                await p.keyboard.press("Control+Shift+r")
            elif kind == "home":
                await self._cdp_navigate(p, _NEWTAB_DATA_URL)
            elif kind == "tab_next":                    # cycle to the next tab
                ctx = self._browser.contexts[0]
                live = [pg for pg in ctx.pages if not pg.is_closed()]
                if len(live) > 1 and self._page in live:
                    nxt = live[(live.index(self._page) + 1) % len(live)]
                    await nxt.bring_to_front()
                    self._page = nxt
                    self._cdp = None
                    self._update_viewport()
                    await self._force_desktop_page(self._page)
            else:
                return {"ok": False, "error": f"unknown action '{kind}'"}
            _u = self._safe_url(p); self.cur_url = _u or self.cur_url
            return {"ok": True, "url": _u}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
        finally:
            try: _lk.release()
            except Exception: pass
            # F2: whatever the action did, paint its result on the next grab —
            # running on the streamer loop, so touching the Event here is safe.
            if self._eager_evt is None:
                self._eager_evt = asyncio.Event()
            self._motion_until = max(
                self._motion_until, time.monotonic() + MOTION_HOLD_S)
            self._eager_evt.set()

    async def _repeat_key(self, key: str) -> None:
        """Simulate OS key auto-repeat for a held key: re-press every ~45ms until
        cancelled (key_up). Each press is a real down+up so the page keeps scrolling/
        navigating. Acquires the io-lock per tick so it doesn't race the grab loop.
        Self-terminates if the page goes away. Cancellation is the normal exit."""
        try:
            await asyncio.sleep(0.28)   # honor the OS repeat-delay before the first repeat
            lk = self._iolock()
            while True:
                p = self._page
                if p is None:
                    break
                await lk.acquire()
                try:
                    await p.keyboard.press(key)
                except Exception:
                    break
                finally:
                    try: lk.release()
                    except Exception: pass
                await asyncio.sleep(0.045)   # ~22 presses/sec → smooth continuous scroll
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            pass
        finally:
            if self._key_repeat:
                self._key_repeat.pop(key, None)


_streamer = _Streamer()


# ── Is the BROWSER up, independent of the feed? ──────────────────────────────
# /operator/status reports the STREAMER's state, which on the launchpad rests at
# 'idle' whether Chrome is healthy or stone dead — nothing has tried to connect
# yet. So the launchpad mark settled into its "connected" pose over a browser
# that wasn't there (the owner 2026-08-02, after an evening of exactly that).
#
# Probed OFF the request path on purpose: _cdp_alive() is a blocking urllib call
# and a WEDGED Chrome is precisely the case where it burns its full timeout —
# doing that inline would stall every status poll for 3s each. A background
# refresh keeps the endpoint free and the answer at most _CDP_TTL seconds stale,
# which is far better than the status quo of never knowing at all.
_CDP_TTL = 10.0
_cdp_probe: dict = {"up": None, "ts": 0.0, "running": False}
_cdp_probe_lock = threading.Lock()


def _cdp_up_cached() -> "bool | None":
    """Last known Chrome reachability. Never blocks; None until the first probe."""
    now = time.monotonic()
    with _cdp_probe_lock:
        fresh = (now - _cdp_probe["ts"]) < _CDP_TTL
        if fresh or _cdp_probe["running"]:
            return _cdp_probe["up"]
        _cdp_probe["running"] = True
        # Snapshot UNDER the lock and return that, not a post-spawn re-read: a
        # fast probe can land between spawn and return, so re-reading makes the
        # value non-deterministic for the same call. Callers get "what we knew
        # when you asked"; the refresh lands on the next poll.
        prev = _cdp_probe["up"]

    def _run() -> None:
        try:
            up = _streamer._cdp_alive()
        except Exception:  # noqa: BLE001 — a probe must never raise into the app
            up = False
        with _cdp_probe_lock:
            _cdp_probe.update(up=up, ts=time.monotonic(), running=False)

    threading.Thread(target=_run, name="operator-cdp-probe", daemon=True).start()
    return prev


# ── Track C: surfaces (browser / desktop-sandbox / desktop-real) ─────────────
# The active surface decides the live-feed source and what the dispatched agent
# drives. Module-level state, reset to browser on server restart (safe default).
import shutil as _shutil
import subprocess as _fsp

_CU_DIR = str(Path(__file__).resolve().parent.parent / "computer-use")

_SURFACE_DEFS = [
    {"key": "browser", "label": "Browser",
     "hint": "Controls a Chrome browser."},
    {"key": "desktop-sandbox", "label": "Sandbox",
     "hint": "Controls an isolated virtual desktop."},
    {"key": "desktop-real", "label": "Computer", "gated": True,
     "hint": "Controls this machine directly. Requires confirmation."},
]
_active_surface = {"name": "browser"}


def _surface_available(key: str) -> bool:
    if key == "browser":
        return True
    if key == "desktop-sandbox":
        # the sandbox is a Docker container now (sandbox_container.py), not the
        # old host-Xvfb path — Xvfb/scrot live INSIDE the image.
        return bool(_shutil.which("docker"))
    if key == "desktop-real":
        # bare which() fails under the systemd --user unit (interop dirs not on
        # its PATH) — accept the canonical WSL interop path too, matching the
        # resolution win_backend.py does.
        _ps = (_shutil.which("powershell.exe")
               or ("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
                   if _os_cfg.path.exists(
                       "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
                   else None))
        return bool(_ps and _os_cfg.path.exists(
            _os_cfg.path.join(_CU_DIR, "win_capture.ps1")))
    return False


_CU_CACHE: dict = {}


def _load_cu(fname: str):
    """Import a computer-use module by path (dir name has a dash). CACHED —
    sandbox_container holds live pipe state (the persistent xdotool shell), and
    a fresh exec_module per steer action re-created its docker exec every time:
    ~130ms/action instead of ~10ms through the warm pipe."""
    if fname not in _CU_CACHE:
        import importlib.util
        p = _os_cfg.path.join(_CU_DIR, fname)
        spec = importlib.util.spec_from_file_location("cu_feed_" + fname[:-3], p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _CU_CACHE[fname] = mod
    return _CU_CACHE[fname]


class _DesktopFeed:
    """Live-feed source for the desktop surfaces — the desktop counterpart of
    _Streamer. Captures via the same computer-use backends the agent drives
    (scrot on the Xvfb display / win_capture.ps1 on the real desktop), at a
    gentler cadence (full-desktop captures are heavier than CDP frames).
    Serves PNG or JPEG parts; the stream generator reads .mime per frame."""

    def __init__(self) -> None:
        self.frame: bytes | None = None
        self.frame_id: int = 0
        self.frame_ts: float = 0.0
        self.mime: str = "image/jpeg"
        self.last_view: float = 0.0
        self.detail: str = ""
        self.surface: str = "desktop-sandbox"
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._mods: dict = {}
        self._stream_dead_until = 0.0   # cooldown after an ffmpeg-stream failure
        self.tier: str = "hi"           # F1: set by the feed routes (last-viewer-wins)
        self._wake = threading.Event()  # F2: a steer poke wakes the capture loop

    def _publish_frame(self, data: bytes, mime: str) -> bool:
        """Refresh capture health without re-identifying duplicate pixels."""
        now = time.monotonic()
        changed = data != self.frame or mime != self.mime
        if changed:
            self.frame = data
            self.mime = mime
            self.frame_id += 1
        self.frame_ts = now
        return changed

    def _pace(self, interval: float) -> None:
        """Sleep between captures, but wake immediately on a steer poke (F2).
        Consume-once so the idle cadence stays untouched afterward."""
        if self._wake.wait(timeout=interval):
            self._wake.clear()

    def ensure_running(self, surface: str) -> None:
        with self._lock:
            self.last_view = time.monotonic()
            self.surface = surface
            alive = self._thread is not None and self._thread.is_alive()
            if self._running and alive:
                return
            self._running = True
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="operator-desktop-feed")
            self._thread.start()

    def _run(self) -> None:
        while self._running:
            if time.monotonic() - self.last_view > IDLE_STOP_AFTER:
                break
            # sandbox: prefer the persistent MJPEG stream (~8fps, one exec for
            # its whole life) over per-frame scrot execs (~1fps, laggy)
            if (self.surface == "desktop-sandbox"
                    and time.monotonic() >= self._stream_dead_until
                    and self._stream()):
                continue      # stream ended (switch/pipe death) — re-decide
            try:
                path = self._capture()
                if path:
                    with open(path, "rb") as f:
                        data = f.read()
                    # scrot writes PNG regardless of suffix; sniff the magic
                    mime = ("image/png" if data[:8] == b"\x89PNG\r\n\x1a\n"
                            else "image/jpeg")
                    self._publish_frame(data, mime)
                    self.detail = ""
            except Exception as e:  # noqa: BLE001 — feed must idle, not die
                self.detail = str(e)
            self._pace(1.2 if self.surface == "desktop-real" else 0.6)
        self.frame = None
        self._running = False

    # §feed-decay (2026-07-09): a long-lived x11grab stream DECAYS on this Xvfb —
    # young streams deliver 10fps with a 20-80ms action→visible latency; by ~8min
    # of age the same pipeline was measured ~2s stale (the "works great after a
    # surface flip, shits the bed a minute later" cockpit lag). Rather than chase
    # ffmpeg's internals, the reader self-heals: count frames actually received
    # per window and CYCLE the stream (kill + immediate respawn, last frame kept,
    # ~0.5s blip) whenever the rate sags below the floor. MJPEG emits ~10 frames/s
    # even on a static screen (identical bytes are still frames), so a sagging
    # receive rate means pipeline decay, not a quiet desktop.
    _HEALTH_WINDOW_S = 5.0    # measure received-fps over this window
    _HEALTH_MIN_FPS = 4.0     # below this → cycle (configured rate is 10)
    _HEALTH_GRACE_S = 15.0    # never judge a freshly-spawned stream

    @classmethod
    def _stream_decayed(cls, n_frames: int, window_s: float, age_s: float) -> bool:
        """Pure decision: has the stream's delivery rate sagged enough to cycle?"""
        if age_s < cls._HEALTH_GRACE_S or window_s < cls._HEALTH_WINDOW_S:
            return False
        return (n_frames / window_s) < cls._HEALTH_MIN_FPS

    def _stream(self) -> bool:
        """Read the sandbox's long-lived ffmpeg MJPEG pipe until the surface
        changes, the viewer idles out, the pipe dies, or delivery decays (see
        _stream_decayed above). Returns True if the stream ran; False → fall
        back to scrot polling (with a cooldown, so an image without ffmpeg
        doesn't pay the spawn cost on every frame)."""
        if "sandbox" not in self._mods:
            self._mods["sandbox"] = _load_cu("sandbox_container.py")
        sb = self._mods["sandbox"]
        # F1: the tier picks the ffmpeg rate/quality at spawn; a mid-stream tier
        # change breaks the read loop below so the outer loop respawns with the
        # new params (~0.5s blip, same path as the decay cycle).
        spawn_tier = self.tier
        fps, q = ((TIER_LO_SANDBOX_FPS, TIER_LO_SANDBOX_Q)
                  if spawn_tier == "lo" else (10, 8))
        try:
            proc = sb.open_stream(fps=fps, quality=q)
        except Exception as e:  # noqa: BLE001 — ffmpeg missing / container down
            self.detail = f"starting sandbox desktop… ({e})"
            self._stream_dead_until = time.monotonic() + 45
            return False
        tail = b""
        born = win_t = time.monotonic()
        win_n = 0
        try:
            while (self._running and self.surface == "desktop-sandbox"
                   and self.tier == spawn_tier
                   and time.monotonic() - self.last_view <= IDLE_STOP_AFTER):
                # read1: return as soon as ANY bytes arrive. A plain read(64KB)
                # blocks until the full 64KB accumulates (~2 frames at q8), so
                # it surfaced only every other frame and added ~400ms of
                # chunk-accumulation latency (halved the configured 10fps).
                chunk = proc.stdout.read1(65536)
                if not chunk:
                    break                    # container gone — outer loop re-decides
                frames, tail = sb.split_jpegs(tail + chunk)
                if frames:
                    win_n += len(frames)
                    self._publish_frame(frames[-1], "image/jpeg")
                    self.detail = ""
                now = time.monotonic()
                if now - win_t >= self._HEALTH_WINDOW_S:
                    if self._stream_decayed(win_n, now - win_t, now - born):
                        break   # cycle: finally reaps, outer loop respawns fresh
                    win_t, win_n = now, 0
        finally:
            # stop_stream kills BOTH the host exec client AND the container-side
            # ffmpeg — a bare proc.kill() leaves the in-container ffmpeg orphaned,
            # still grabbing X11; those stacked up and made the feed lag worse the
            # longer a session ran (2026-07-09).
            try:
                sb.stop_stream(proc)
            except Exception:  # noqa: BLE001
                pass
        return True

    def _capture(self) -> str | None:
        if self.surface == "desktop-real":
            if "win" not in self._mods:
                self._mods["win"] = _load_cu("win_backend.py")
            return self._mods["win"].screenshot("windows-primary", _SHOT_DIR)
        # sandbox: a REAL isolated Docker desktop. Bring the container up (once)
        # and capture it via docker exec. ensure() is idempotent + persistent —
        # the container survives across switches; it is never torn down here.
        if "sandbox" not in self._mods:
            self._mods["sandbox"] = _load_cu("sandbox_container.py")
        try:
            self._mods["sandbox"].ensure()
        except Exception as e:  # noqa: BLE001 — surface the reason, keep idling
            self.detail = f"starting sandbox desktop… ({e})"
            return None
        return self._mods["sandbox"].screenshot(_SHOT_DIR)


_desktop_feed = _DesktopFeed()


# ── routes ────────────────────────────────────────────────────────────────
@bp.context_processor
def _inject_version():
    """OP_VERSION reaches every template rendered from this blueprint, in both
    mounts (host-app and the standalone fam/demo servers), without each
    render call having to remember to pass it."""
    return {"OP_VERSION": OP_VERSION}


@bp.route("/operator")
def operator_page():
    from flask import make_response
    # demo: serve the standalone, de-PII'd template (no squad chrome/nav, no owner
    # refs, bot picker collapsed). Regenerate with gen_demo_template.py.
    _tmpl = "operator_demo.html" if DEMO else "operator.html"
    # Seed the viewport BEFORE the page paints. The cockpit writes its stage
    # size to op_stage, so a returning viewer's geometry rides up on the
    # document request and the streamer's first attach already targets it —
    # no opening frame at the last viewer's aspect, no resize-to-snap.
    _stage = (request.cookies.get("op_stage") or "").lower()
    if "x" in _stage:
        _w, _, _h = _stage.partition("x")
        _streamer.seed_view_from_stage(_w, _h)
    resp = make_response(render_template(
        _tmpl, standalone=STANDALONE, demo_interactive=DEMO_INTERACTIVE))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@bp.route("/demo")
def operator_demo_page():
    """Demo entry path alias (the public demo URL ends in /demo, not /operator —
    version-agnostic). Serves the same page; only meaningful when DEMO."""
    return operator_page()


@bp.route("/cockpit")
def _cockpit_redirect():  # legacy path → operator
    from flask import redirect, url_for
    return redirect(url_for("operator.operator_page"))


# Per-client tier + liveness. The tier used to be last-viewer-wins on the
# shared buffer, which the old docstring called "fine for a single-user
# cockpit". It is not a single-user cockpit — iPad and desktop watch together,
# and the viewport recorder routinely shows three pullers. With one client on
# lo and one on hi, EVERY frame request restamped the shared field, so
# consecutive captures alternated between the 900px lo cap and full device
# resolution and the <img> rescaled on each swap. That is the flicker: the same
# "spasming between small and big at constant frequency" the capture path
# already documents for the 2026-07-12 CDP-fallback version of this bug.
#
# Serve the HIGHEST tier any live viewer wants. A lo client can downscale what
# it receives; a hi client cannot invent the resolution back. Liveness reuses
# the viewport owner's window — a closed tab stops polling within one frame.
TIER_IDLE_S = VP_OWNER_IDLE_S
_TIER_SEEN: dict = {}


def effective_tier(seen: dict, now: float) -> str:
    """Highest tier among clients that have polled inside the idle window."""
    live = [t for t, ts in seen.values() if now - ts <= TIER_IDLE_S]
    if not live:
        return "hi"
    return "hi" if "hi" in live else "lo"


def _apply_feed_tier() -> None:
    """F1: read ?tier=lo|hi and ?cid off the request and stamp both feeds."""
    tier = "lo" if request.args.get("tier") == "lo" else "hi"
    cid = request.args.get("cid") or "anon"
    now = time.monotonic()
    _TIER_SEEN[cid] = (tier, now)
    for k, (_t, ts) in list(_TIER_SEEN.items()):
        if now - ts > TIER_IDLE_S:
            _TIER_SEEN.pop(k, None)
    resolved = effective_tier(_TIER_SEEN, now)
    _streamer.tier = resolved
    _desktop_feed.tier = resolved


def _frame_source():
    """Return (surface, source, mime), starting only the selected feed."""
    surface = _active_surface["name"]
    if surface == "browser":
        _streamer.ensure_running()
        return surface, _streamer, "image/jpeg"
    _desktop_feed.ensure_running(surface)
    return surface, _desktop_feed, _desktop_feed.mime


def _frame_token(surface: str, src) -> str:
    """Per-surface identity prevents equal counters colliding on a switch."""
    return f"{surface}:{getattr(src, 'frame_id', 0)}"


@bp.route("/operator/stream")
def operator_stream():
    """MJPEG multipart stream — renders into an <img>. Survives frame gaps.
    Source-switched per frame by the active surface: browser → the CDP
    _Streamer; desktop surfaces → the _DesktopFeed. Switching surfaces mid-
    stream just swaps the source, no reconnect needed."""
    _apply_feed_tier()
    if _active_surface["name"] == "browser":
        _streamer.ensure_running()
    else:
        _desktop_feed.ensure_running(_active_surface["name"])
    # captured OUTSIDE gen() — no request context inside a streaming generator
    _cid = request.args.get("cid") or ""

    def _part(data, mime=b"image/jpeg"):
        return (b"--frame\r\n"
                b"Content-Type: " + mime + b"\r\n"
                b"Content-Length: " + str(len(data)).encode() + b"\r\n\r\n"
                + data + b"\r\n")

    def gen():
        # Emit a placeholder frame IMMEDIATELY so the <img> always has valid
        # multipart data and never shows the broken-image glyph, even before the
        # first real capture (cold start / mid-reattach). We then keep the
        # connection open forever, swapping in real frames as they arrive.
        yield _part(_PLACEHOLDER_JPEG)
        last_sent = -1.0
        last_push = 0.0
        # Poll the frame buffer MUCH faster than the capture cadence so a fresh
        # frame is pushed within a few ms of being grabbed (snappy feed) — we only
        # actually yield when the frame is NEW, so the fast poll adds no bandwidth.
        POLL = 0.02
        while True:
            if _active_surface["name"] == "browser":
                src = _streamer
                _streamer.ensure_running()
                mime = b"image/jpeg"
            else:
                src = _desktop_feed
                _desktop_feed.ensure_running(_active_surface["name"])
                mime = src.mime.encode()
            src.last_view = time.monotonic()
            _streamer.vp_note_pull(_cid)
            f = src.frame
            ts = src.frame_ts
            now = time.monotonic()
            if f and ts != last_sent:
                last_sent = ts; last_push = now      # push a new frame immediately
                yield _part(f, mime)
            elif f and (now - last_push) > 1.0:
                last_push = now                       # ~1s heartbeat of last frame
                yield _part(f, mime)
            elif not f and (now - last_push) > 1.0:
                last_push = now                       # placeholder heartbeat (no frame yet)
                yield _part(_PLACEHOLDER_JPEG)
            time.sleep(POLL)

    resp = Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.route("/operator/frame")
def operator_frame():
    """Single newest frame — the pull half of the feed. The MJPEG push stream
    has no backpressure: a client that decodes slower than the feed produces
    (iPad Safari) buffers the excess and drifts PROGRESSIVELY behind live —
    the 'works great after a reconnect, shits the bed a minute later' lag
    (2026-07-09). The cockpit now self-clocks instead: fetch a frame, render
    it, only then fetch the next — latency is bounded at ~1 frame in flight
    by construction, on any device or link. Fast clients still get ~10fps."""
    _apply_feed_tier()
    cid = request.args.get("cid") or ""
    since = request.args.get("since") or ""
    try:
        wait_ms = max(0, min(1200, int(request.args.get("wait") or 0)))
    except (TypeError, ValueError):
        wait_ms = 0

    surface, src, mime = _frame_source()
    src.last_view = time.monotonic()
    _streamer.vp_note_pull(cid)
    token = _frame_token(surface, src)

    # Long-poll only when the caller already owns the current pixels. The
    # producer keeps capturing independently; this request wakes within one
    # 20ms check when a new frame lands, or returns an empty heartbeat. One
    # outstanding request preserves the existing backpressure guarantee.
    deadline = time.monotonic() + wait_ms / 1000.0
    while since and token == since and time.monotonic() < deadline:
        time.sleep(0.02)
        next_surface = _active_surface["name"]
        if next_surface != surface:
            surface, src, mime = _frame_source()
        src.last_view = time.monotonic()
        token = _frame_token(surface, src)

    f = src.frame
    mime = "image/jpeg" if surface == "browser" else src.mime
    if since and token == since:
        resp = Response(status=204)
    else:
        resp = Response(f or _PLACEHOLDER_JPEG,
                        mimetype=mime if f else "image/jpeg")
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Operator-Frame"] = "live" if f else "placeholder"
    resp.headers["X-Operator-Frame-ID"] = token
    return resp


@bp.route("/operator/tabs")
def operator_tabs():
    return jsonify(tabs=_streamer.list_tabs())


@bp.route("/operator/tab/<int:idx>", methods=["POST"])
def operator_tab_switch(idx):
    return jsonify(_streamer.switch_tab(idx))


@bp.route("/operator/tab/<int:idx>/close", methods=["POST"])
def operator_tab_close(idx):
    return jsonify(_streamer.close_tab(idx))


@bp.route("/operator/homepage", methods=["GET", "POST"])
def operator_homepage():
    """The landing page for new tabs and the last-tab reset.

    Server-side because Python is what opens those tabs — a localStorage
    preference could never reach it (the owner 2026-08-07).
    """
    if request.method == "GET":
        return jsonify(ok=True, homepage=operator_prefs.homepage(),
                       default=operator_prefs.DEFAULT_HOMEPAGE)
    try:
        value = operator_prefs.set_homepage((request.get_json(silent=True) or {}).get("homepage", ""))
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    return jsonify(ok=True, homepage=value)


@bp.route("/operator/tab/new", methods=["POST"])
def operator_tab_new():
    return jsonify(_streamer.new_tab())


# Dirs whose screenshots are served read-only by basename, so a screenshot the
# agent references inline (![](file://.../x.png)) renders in the chat instead
# of being stripped to a text note. The list is owned by operator_trace (1.0.8
# R3) so this route and the trace rewriter can never disagree (a mismatch 404s
# the rewritten links). [0] is the MCP output dir; the rest are per-bot cwds.
from operator_trace import shot_dirs as _shot_dirs
_SHOT_DIRS = _shot_dirs()
_SHOT_DIR = _SHOT_DIRS[0]


def _find_shot(base: str) -> str | None:
    """Locate a screenshot by basename across the servable dirs (first hit)."""
    for d in _SHOT_DIRS:
        target = _os_cfg.path.realpath(_os_cfg.path.join(d, base))
        if _os_cfg.path.commonpath([target, d]) == d and _os_cfg.path.isfile(target):
            return d
    return None


@bp.route("/operator/shot/<path:name>")
def operator_shot(name):
    """Serve an agent screenshot PNG/JPG by basename from the computer-use output
    dir or a bot session dir. Basename-only + extension whitelist + realpath
    containment → no traversal."""
    from flask import send_from_directory, abort
    base = _os_cfg.path.basename(name)            # strip any path components
    if not base or base != name or base.startswith("."):
        abort(404)
    if _os_cfg.path.splitext(base)[1].lower() not in (".png", ".jpg", ".jpeg", ".webp"):
        abort(404)
    d = _find_shot(base)
    if d is None:
        abort(404)
    resp = send_from_directory(d, base)
    resp.headers["Cache-Control"] = "private, no-store, max-age=0"
    return resp


@bp.route("/operator/status")
def operator_status():
    _streamer.last_view = time.monotonic()
    # cockpit is open and polling → any finished-run badge is "seen"
    if not DEMO:
        try:
            import operator_schedule as _os_mod
            _os_mod.clear_unseen()
        except Exception:
            pass
    surface = _active_surface["name"]
    if surface != "browser":
        # desktop feed: freshness from ITS buffer; no viewport/url/click mapping
        # (manual control is browser-only — the UI disables it on desktop).
        _desktop_feed.last_view = time.monotonic()
        fresh = (_desktop_feed.frame is not None
                 and (time.monotonic() - _desktop_feed.frame_ts) < 8.0)
        return jsonify(status=("live" if fresh else "connecting"),
                       detail=_desktop_feed.detail, has_frame=fresh,
                       vw=0, vh=0, url="", click=None, surface=surface)
    attached = _streamer.has_attached_page()
    fresh = (attached and _streamer.frame is not None
             and (time.monotonic() - _streamer.frame_ts) < 6.5)
    status = _streamer.status
    if status == "live" and not attached:
        status = "connecting"
    cur_url = _streamer.cur_url
    lx, ly, lt = _streamer.last_click
    click = None
    if lt and (time.monotonic() - lt) < 1.2:
        click = {"x": round(lx, 4), "y": round(ly, 4), "age": round(time.monotonic() - lt, 3)}
    return jsonify(status=status, detail=_streamer.detail,
                   has_frame=fresh, vw=_streamer.vw, vh=_streamer.vh, url=cur_url,
                   click=click, surface=surface,
                   # Real Chrome reachability, NOT the feed's state — the two
                   # diverge exactly when it matters (idle feed, dead browser).
                   browser_up=_cdp_up_cached())


@bp.route("/operator/debug/viewport")
def operator_debug_viewport():
    """Viewport flight recorder — who reflowed the shared browser, when. The
    2026-07 strobe/zoom hunts took five rounds because nothing recorded the
    writers; now every reflow path logs here. Read-only, cheap, always on."""
    import datetime as _dt
    now_m = time.monotonic()
    return jsonify(
        view=[_streamer.view_w, _streamer.view_h],
        owner=_streamer._vp_owner,
        pullers={c: round(now_m - t, 1) for c, t in _streamer._vp_seen.items()},
        events=[{"at": _dt.datetime.fromtimestamp(e["t"]).strftime("%H:%M:%S"),
                 "kind": e["kind"], "detail": e["detail"]}
                for e in list(_streamer._vp_events)])


@bp.route("/operator/history")
def operator_history_list():
    """Flight-recorder rows, newest first (lean — no trace payloads)."""
    if DEMO:
        return jsonify(ok=False, error="history is live-cockpit only"), 403
    import operator_history as _hist
    try:
        limit = min(max(int(request.args.get("limit", 30)), 1), 200)
    except (TypeError, ValueError):
        limit = 30
    return jsonify(ok=True, runs=_hist.recent(limit))


@bp.route("/operator/history/<int:run_id>")
def operator_history_get(run_id: int):
    """One recorded run, full trace included."""
    if DEMO:
        return jsonify(ok=False, error="history is live-cockpit only"), 403
    import operator_history as _hist
    row = _hist.get(run_id)
    if row is None:
        return jsonify(ok=False, error="no such run"), 404
    return jsonify(ok=True, run=row)


@bp.route("/operator/session", methods=["GET", "POST"])
def operator_session():
    """One conversation's shared cockpit state, revisioned across devices."""
    if DEMO:
        return jsonify(ok=False, error="demo sessions are per-visitor"), 403
    import operator_session as _sess_store
    if request.method == "GET":
        cid = (request.args.get("conversation_id") or "").strip()
        try:
            got = _sess_store.load(conversation_id=cid or None)
        except KeyError:
            return jsonify(ok=False, error="no such conversation"), 404
        cid = cid or _sess_store.active_id() or "legacy"
        try:
            after = int(request.args.get("after_rev", "") or -1)
        except (TypeError, ValueError):
            after = -1
        if after >= 0 and after == got["conversation_rev"]:
            return "", 204
        return jsonify(ok=True, rev=got["rev"], data=got["data"],
                       conversation_rev=got["conversation_rev"],
                       conversation_id=cid)
    body = request.get_json(silent=True) or {}
    data = body.get("data")
    if not isinstance(data, dict):
        return jsonify(ok=False, error="body must be {data: {...}}"), 400
    try:
        cid = str(body.get("conversation_id") or "").strip()
        cid = cid or _sess_store.active_id() or "legacy"
        client_id = str(body.get("client_id") or "").strip()
        if client_id:
            control = _sess_store.touch_presence(
                cid, client_id, str(body.get("device_label") or ""))
            if not control["can_control"]:
                current = _sess_store.load(conversation_id=cid)
                return jsonify(
                    ok=False,
                    error=f"thread is open on {control['controller_label']}; take over to continue",
                    conversation_id=cid, **current), 409
        rev = _sess_store.save(
            data, conversation_id=cid,
            expected_rev=body.get("expected_rev"))
        current = _sess_store.load(conversation_id=cid)
    except _sess_store.SessionConflict as e:
        return jsonify(ok=False, error=str(e), conversation_id=cid,
                       **e.current), 409
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 413
    except KeyError:
        return jsonify(ok=False, error="no such conversation"), 404
    return jsonify(ok=True, rev=rev,
                   conversation_rev=current["conversation_rev"],
                   conversation_id=cid)


@bp.route("/operator/sessions", methods=["GET", "POST"])
def operator_sessions():
    """The conversation list. GET → {ok, rev, active, sessions[]}; POST → a new
    empty conversation, made active. Demo stays per-visitor, so it is gated
    exactly like /operator/session."""
    if DEMO:
        return jsonify(ok=False, error="demo sessions are per-visitor"), 403
    import operator_session as _sess_store
    if request.method == "GET":
        got = _sess_store.listing()
        client_id = str(request.args.get("client_id") or "").strip()
        statuses = operator_agent.runner.conversation_summaries()
        for row in got["sessions"]:
            row["presence"] = _sess_store.presence(row["id"], client_id)
            row.update(statuses.get(row["id"], {
                "state": "idle", "bot": None, "alive": False}))
        return jsonify(ok=True, **got)
    body = request.get_json(silent=True) or {}
    made = _sess_store.create(str(body.get("title") or ""))
    return jsonify(ok=True, **made)


@bp.route("/operator/sessions/<sid>", methods=["POST", "DELETE"])
def operator_session_one(sid: str):
    """POST {action: activate|rename, title?} / DELETE. One route because the
    client only ever does these three things to a conversation, and three
    near-identical routes is more surface than the feature needs."""
    if DEMO:
        return jsonify(ok=False, error="demo sessions are per-visitor"), 403
    import operator_session as _sess_store
    try:
        if request.method == "DELETE":
            status = operator_agent.runner.conversation_summaries().get(sid, {})
            if status.get("alive"):
                return jsonify(ok=False, error="stop this conversation before deleting it"), 409
            out = _sess_store.delete(sid)
            _browser_tab_command("release", sid, close=True)
            return jsonify(ok=True, **out)
        body = request.get_json(silent=True) or {}
        action = body.get("action") or "activate"
        if action == "rename":
            title = body.get("title")
            if not isinstance(title, str):
                return jsonify(ok=False, error="rename needs a title"), 400
            return jsonify(ok=True, rev=_sess_store.rename(sid, title))
        if action != "activate":
            return jsonify(ok=False, error=f"unknown action {action!r}"), 400
        return jsonify(ok=True, **_sess_store.activate(sid))
    except KeyError:
        return jsonify(ok=False, error="no such conversation"), 404


def _browser_tab_command(action: str, sid: str, *, close: bool = False) -> bool:
    """Best-effort bridge to the shared-Chrome tab registry.

    Kept lazy so the standalone public demo (which intentionally has no sibling
    browse module) never imports private browser plumbing.
    """
    import subprocess
    import sys
    here = Path(__file__).resolve()
    candidates = (
        here.parents[1] / "browse" / "operator_browser_tabs.py",  # monorepo
        here.parent / "browse" / "operator_browser_tabs.py",      # standalone
    )
    helper = next((path for path in candidates if path.exists()), None)
    if DEMO or helper is None:
        return False
    cmd = [sys.executable, str(helper), action, sid, CDP_URL]
    if close:
        cmd.append("--close")
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=6,
                              check=False)
        if done.returncode:
            return False
        return (done.stdout.strip() != "0") if action == "activate" else True
    except (OSError, subprocess.SubprocessError):
        return False


@bp.route("/operator/sessions/<sid>/browser", methods=["POST"])
def operator_session_browser(sid: str):
    """Bring the selected conversation's owned Chrome tab to the cockpit feed."""
    if DEMO:
        return jsonify(ok=False, error="demo sessions are per-visitor"), 403
    return jsonify(ok=True, activated=_browser_tab_command("activate", sid))


@bp.route("/operator/sessions/<sid>/presence", methods=["POST"])
def operator_session_presence(sid: str):
    """Renew, observe or explicitly take over one conversation's edit lease."""
    if DEMO:
        return jsonify(ok=False, error="demo sessions are per-visitor"), 403
    import operator_session as _sess_store
    body = request.get_json(silent=True) or {}
    try:
        out = _sess_store.touch_presence(
            sid, str(body.get("client_id") or ""),
            str(body.get("label") or body.get("device_label") or ""),
            take_over=bool(body.get("take_over")))
        return jsonify(ok=True, conversation_id=sid, **out)
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    except KeyError:
        return jsonify(ok=False, error="no such conversation"), 404


@bp.route("/operator/unseen")
def operator_unseen():
    """Finished-runs-you-haven't-looked-at count — feeds the red badge on the
    host-app operator nav tab. Always 0 in the demo (no scheduler there)."""
    if DEMO:
        return jsonify(count=0)
    try:
        import operator_schedule as _os_mod
        return jsonify(count=_os_mod.unseen_count())
    except Exception:
        return jsonify(count=0)


def _img_dims(data: bytes) -> tuple | None:
    """(w, h) from PNG/JPEG header bytes — no PIL dependency in the server."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return (int.from_bytes(data[16:20], "big"),
                    int.from_bytes(data[20:24], "big"))
        if data[:2] == b"\xff\xd8":                     # JPEG: scan SOF marker
            i = 2
            while i < len(data) - 9:
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    return (int.from_bytes(data[i + 7:i + 9], "big"),
                            int.from_bytes(data[i + 5:i + 7], "big"))
                i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    except Exception:  # noqa: BLE001
        pass
    return None


def _desktop_steer(action: dict) -> dict:
    """Manual steer for the desktop surfaces — the same normalized-coordinate
    gestures the browser stage sends, injected via the surface's own backend
    (docker-exec xdotool for the sandbox, win input for the real desktop)."""
    surface = _active_surface["name"]
    mod = _load_cu("sandbox_container.py" if surface == "desktop-sandbox"
                   else "win_backend.py")
    # frame size: sandbox is fixed; real desktop = the last streamed frame's
    # dims (win_backend scales image→physical coords itself at exec time)
    if surface == "desktop-sandbox":
        w, h = mod.size()
    else:
        dims = _img_dims(_desktop_feed.frame or b"")
        if not dims:
            return {"ok": False, "error": "no desktop frame yet"}
        w, h = dims

    def _xy(nx, ny):
        return [max(0, min(w - 1, int(float(nx or 0) * w))),
                max(0, min(h - 1, int(float(ny or 0) * h)))]

    def _run(a: dict) -> None:
        if surface == "desktop-sandbox":
            # no ensure() here — that's a docker-inspect subprocess (~80ms) per
            # action; the input pipe already self-heals (its retry calls ensure
            # when the pipe is dead), so a live pipe means a live container.
            mod.execute(a)
        else:
            mod.execute(a, "windows-primary")

    kind = action.get("kind")
    try:
        if kind == "move":
            _run({"action": "mouse_move", "coordinate": _xy(action["x"], action["y"])})
        elif kind in ("click_at", "dblclick_at"):
            n = action.get("count") or (2 if kind == "dblclick_at" else 1)
            act_name = {1: "left_click", 2: "double_click"}.get(
                min(int(n), 3), "triple_click")
            if surface == "desktop-real" and act_name == "triple_click":
                act_name = "double_click"          # win backend caps at double
            _run({"action": act_name, "coordinate": _xy(action["x"], action["y"])})
        elif kind == "rclick_at":
            _run({"action": "right_click", "coordinate": _xy(action["x"], action["y"])})
        elif kind == "drag":
            _run({"action": "left_click_drag",
                  "start_coordinate": _xy(action.get("x0"), action.get("y0")),
                  "coordinate": _xy(action.get("x1"), action.get("y1"))})
        elif kind in ("mousedown_at", "mouseup_at"):
            _run({"action": "mouse_move", "coordinate": _xy(action["x"], action["y"])})
            _run({"action": "left_mouse_down" if kind == "mousedown_at"
                  else "left_mouse_up"})
        elif kind == "scroll":
            dx, dy = action.get("dx"), action.get("dy")
            if isinstance(dy, (int, float)) and dy:
                _run({"action": "scroll",
                      "scroll_direction": "down" if dy > 0 else "up",
                      "scroll_amount": max(1, min(10, round(abs(dy) / 80)))})
            if isinstance(dx, (int, float)) and dx:
                _run({"action": "scroll",
                      "scroll_direction": "right" if dx > 0 else "left",
                      "scroll_amount": max(1, min(10, round(abs(dx) / 80)))})
        elif kind == "type":
            _run({"action": "type", "text": str(action.get("value", ""))})
        elif kind in ("key", "key_down", "key_up"):
            k = kind
            if surface == "desktop-real" and kind != "key":
                # win backend has no keydown/keyup — degrade a hold to one press
                if kind == "key_up":
                    return {"ok": True}
                k = "key"
            _run({"action": k, "text": str(action.get("value", ""))})
        else:
            return {"ok": False, "error": f"{kind!r} not supported on this surface"}
        # F2: the action landed — wake the capture loop so its result paints
        # now instead of up to a full poll interval later (scrot path; the
        # ffmpeg stream is already continuous).
        _desktop_feed._wake.set()
        return {"ok": True}
    except Exception as e:  # noqa: BLE001 — surface the reason to the cockpit
        return {"ok": False, "error": str(e)}


@bp.route("/operator/steer", methods=["POST"])
def operator_steer():
    data = request.get_json(silent=True) or request.form
    if DEMO and data.get("kind") == "extensions":
        return jsonify(ok=False, error="extensions are unavailable in demo mode"), 403
    action = {"kind": data.get("kind"), "value": data.get("value", ""),
              "x": data.get("x", 0), "y": data.get("y", 0),
              # dx/dy carry the wheel/touch scroll delta (kind=="scroll"). Must default
              # to None, NOT 0 — _do_action tells "a real delta was sent" apart from
              # "no delta, use the up/down/top/bottom keyword" via isinstance(dy, (int,
              # float)), and 0 is itself an int. This dict used to whitelist only
              # kind/value/x/y, silently dropping dx/dy off every scroll request, so
              # _do_action always fell through to the keyword branch with val=="" (the
              # wheel handler never sends `value`) → amt defaulted to 600 (down) no
              # matter which way the wheel actually moved. That's why wheel-up did
              # nothing while wheel-down "worked" (the owner 2026-06-30).
              "dx": data.get("dx"), "dy": data.get("dy"),
              # drag endpoints (kind=="drag") — were silently dropped by this
              # whitelist (same class as the dx/dy bug above), so a user drag
              # collapsed to (0,0)→(0,0). Pass them through.
              "x0": data.get("x0", 0), "y0": data.get("y0", 0),
              "x1": data.get("x1", 0), "y1": data.get("y1", 0),
              # count carries the native multi-click detail (1=single, 2=double,
              # 3=triple → word/paragraph selection on the remote page).
              "count": data.get("count"),
              # cid identifies the cockpit tab for viewport ownership
              # (kind=="stage_size") — same whitelist-drop class as dx/dy above.
              "cid": data.get("cid"),
              # force makes an entry/re-entry beacon repair actual Chrome
              # metrics even when the streamer's desired WxH already matches.
              "force": data.get("force") is True}
    if not action["kind"]:
        return jsonify(ok=False, error="missing action kind"), 400
    # desktop surfaces: same gestures, injected via the surface backend instead
    # of CDP — manual steer works everywhere the feed does.
    if _active_surface["name"] != "browser":
        return jsonify(_desktop_steer(action))
    return jsonify(_streamer.run_action(action))


# ── Live-session driving (the owner 2026-06-26) ──────────────────────────────────
# Dispatch a task to one of the host bots' real Discord sessions; the bot
# runs it on the SAME shared Chrome the operator views. The browser actions are
# surfaced via the MCP action-tap (operator-events.ndjson) which every bot's
# playwright-mcp wrapper writes to — so the operator shows "🤖 <bot> · Clicking…"
# + the step trail regardless of which bot is driving. (Reasoning relay = stage 2.)
import json as _json
import os as _os

# The 5 drivers: host bots that can take the wheel. home_channel = where the
# operator posts the task (the running bot picks it up as a prompt). `key` is the
# bot name the action-tap stamps events with (must match detect_bot()).
DRIVERS = [
    {"key": "claude-a", "label": "claude-a"},
    {"key": "claude-b", "label": "claude-b"},
    {"key": "gpt", "label": "gpt"},
    # gemma drives via the agy runtime (flat Google sub) — agy IS gemma's engine,
    # so there's one pickable entry, not a separate "agy" row.
    {"key": "gemma", "label": "gemma"},
]
_DRIVER_BY_KEY = {d["key"]: d for d in DRIVERS}

_EVENT_LOG = _os.path.expanduser("~/.cache/computer-use/operator-events.ndjson")


def _recent_events(limit: int = 40) -> list:
    """Tail the action-tap event log → recent {bot,action,detail,ts} events."""
    if DEMO:
        return []
    try:
        with open(_EVENT_LOG, encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
        out = []
        for ln in lines:
            try:
                out.append(_json.loads(ln))
            except Exception:
                pass
        return out
    except OSError:
        return []


def _current_driver(window_s: float = 12.0) -> dict | None:
    """The bot whose last browser action was within `window_s` → who's driving now."""
    evs = _recent_events(8)
    if not evs:
        return None
    last = evs[-1]
    if time.time() - last.get("ts", 0) <= window_s:
        # demo: never leak squad bot names to a public visitor -> generic label.
        _b = "assistant" if DEMO else last.get("bot")
        return {"bot": _b, "action": last.get("action"),
                "detail": last.get("detail", "")}
    return None


@bp.route("/operator/drivers")
def operator_drivers():
    """The pickable drivers — the operator runs them headless. In demo mode this is
    a single generic 'gpt' driver (never leak squad bot names to a public visitor)."""
    if DEMO:
        return jsonify(drivers=[{"key": "bot", "label": "bot"}])
    return jsonify(drivers=[{"key": d["key"], "label": d["label"]} for d in DRIVERS])


@bp.route("/operator/surfaces")
def operator_surfaces():
    """The pickable surfaces (Track C). Demo gets the browser + the (isolated,
    demo-scoped) sandbox; the REAL desktop exposes the host machine, so it shows
    but stays grayed out — live-cockpit only."""
    out = [dict(s, available=_surface_available(s["key"]))
           for s in _SURFACE_DEFS]
    if DEMO:
        for s in out:
            if s["key"] == "desktop-real":
                s["available"] = False
                s["unavailable_hint"] = "Live cockpit only."
        return jsonify(surfaces=out, active=_active_surface["name"])
    return jsonify(surfaces=out, active=_active_surface["name"])


@bp.route("/operator/surface", methods=["POST"])
def operator_surface_set():
    """Switch the active surface: swaps the live-feed source immediately and
    sets the default surface for the next dispatch. desktop-real demands the
    explicit confirm flag every time (the UI shows the consent step)."""
    data = request.get_json(silent=True) or request.form
    name = (data.get("surface") or "").strip()
    if DEMO and name == "desktop-real":
        return jsonify(ok=False, error="the real desktop is live-cockpit only"), 403
    if name not in [s["key"] for s in _SURFACE_DEFS]:
        return jsonify(ok=False, error=f"unknown surface {name!r}"), 400
    if not _surface_available(name):
        return jsonify(ok=False, error=f"{name} not available on this host"), 409
    if name == "desktop-real" and not data.get("confirm"):
        return jsonify(ok=False, error="desktop-real needs confirm"), 403
    _active_surface["name"] = name
    if name != "browser":
        _desktop_feed.ensure_running(name)
    return jsonify(ok=True, active=name)


# Game maps live in vision/maps/ — scanned directly (no heavy import). Selecting
# one only scopes the agent's perceive/game_macro calls; there's no host-side
# "active map" state — the pick is folded into the dispatched task text.
_MAPS_DIR = str(Path(__file__).resolve().parent / "vision" / "maps")


@bp.route("/operator/maps")
def operator_maps():
    """Game maps shippable to perceive/game_macro. Demo never plays games."""
    if DEMO:
        return jsonify(maps=[])
    names = []
    try:
        for f in sorted(_os_cfg.listdir(_MAPS_DIR)):
            base, ext = _os_cfg.path.splitext(f)
            if ext in (".yaml", ".yml", ".json"):
                names.append(base)
    except FileNotFoundError:
        pass
    return jsonify(maps=names)


# apps the taskbar can launch inside the sandbox (whitelist — the route never
# execs a client-supplied binary name).
_SANDBOX_APPS = {"chromium": "Chromium", "xfce4-terminal": "Terminal",
                 "thunar": "Files", "mousepad": "Editor"}


@bp.route("/operator/sandbox/ctl", methods=["POST"])
def operator_sandbox_ctl():
    """Taskbar controls for the sandbox desktop. launch/restart act on the
    persistent container; delete is the ONE destructive teardown — the next
    capture boots a factory-fresh desktop. Demo instances act on their own
    container (OPERATOR_SANDBOX_CONTAINER is demo-scoped at module load),
    so this is safe to expose there too."""
    if not _surface_available("desktop-sandbox"):
        return jsonify(ok=False, error="sandbox not available on this host"), 409
    data = request.get_json(silent=True) or request.form
    act = (data.get("action") or "").strip()
    sb = _load_cu("sandbox_container.py")
    try:
        if act == "launch":
            app_name = (data.get("app") or "").strip()
            if app_name not in _SANDBOX_APPS:
                return jsonify(ok=False, error=f"unknown app {app_name!r}"), 400
            sb.ensure()
            sb.launch(app_name)
        elif act == "restart":
            sb.stop()
            sb.ensure()
        elif act == "delete":
            sb.delete()
        else:
            return jsonify(ok=False, error=f"unknown action {act!r}"), 400
    except Exception as e:  # noqa: BLE001 — surface the reason to the taskbar
        return jsonify(ok=False, error=str(e)), 500
    return jsonify(ok=True)


# ── sandbox file exchange (Transfer) ─────────────────────────────────────────
# In and out of the container's Downloads/Desktop/Documents only; path shape is
# validated by sandbox_container.safe_rel. NEVER in the demo — the demo box is
# shared between strangers, and one visitor must not see another's files.
def _sandbox_files_guard():
    if DEMO:
        return jsonify(ok=False, error="file transfer is live-cockpit only"), 403
    if not _surface_available("desktop-sandbox"):
        return jsonify(ok=False, error="sandbox not available on this host"), 409
    return None


@bp.route("/operator/sandbox/files")
def operator_sandbox_files():
    guard = _sandbox_files_guard()
    if guard:
        return guard
    sb = _load_cu("sandbox_container.py")
    try:
        return jsonify(ok=True, dirs=sb.list_files())
    except Exception as e:  # noqa: BLE001
        return jsonify(ok=False, error=str(e)), 500


@bp.route("/operator/sandbox/upload", methods=["POST"])
def operator_sandbox_upload():
    guard = _sandbox_files_guard()
    if guard:
        return guard
    sb = _load_cu("sandbox_container.py")
    if (request.content_length or 0) > sb.MAX_FILE_BYTES:
        return jsonify(ok=False, error="file too large"), 413
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(ok=False, error="no file"), 400
    import tempfile
    import werkzeug.utils as _wu
    name = _wu.secure_filename(f.filename) or "upload"
    try:
        with tempfile.TemporaryDirectory(dir=_SHOT_DIR) as td:
            tmp = _os.path.join(td, name)
            f.save(tmp)
            rel = sb.put_file(tmp, name)
        return jsonify(ok=True, path=rel)
    except Exception as e:  # noqa: BLE001
        return jsonify(ok=False, error=str(e)), 500


@bp.route("/operator/sandbox/file/<path:rel>")
def operator_sandbox_file(rel: str):
    guard = _sandbox_files_guard()
    if guard:
        return guard
    sb = _load_cu("sandbox_container.py")
    try:
        out = sb.get_file(rel, _os.path.join(_SHOT_DIR, "sandbox-out"))
    except Exception as e:  # noqa: BLE001
        return jsonify(ok=False, error=str(e)), 400
    return send_file(out, as_attachment=True,
                     download_name=_os.path.basename(out))


def _desktop_real_preflight() -> str | None:
    """Probe real-desktop capture and input before a desktop-real run.

    Returns an error string when the console can't actually be controlled. A
    locked or non-interactive Windows session reports the disconnected-console default
    geometry (exactly 1024×768) and captures a blank frame — on this panel
    (2816×1940) that geometry is unambiguous. The probe costs one capture
    (~1-2s), paid only on desktop-real dispatches."""
    try:
        wb = _load_cu("win_backend.py")
        wb.ensure_input()
        w, h = wb.screen_size()
    except Exception as e:  # noqa: BLE001 — no powershell / capture broke
        return f"desktop-real unavailable: {e}"
    if (w, h) == (1024, 768):
        return ("the Windows console looks locked or headless (phantom "
                "1024×768 screen) — unlock the desktop, then dispatch again")
    return None


def _conversation_for(data=None, *, scheduled: str = "") -> str:
    """Resolve the runner key while preserving old clients that send no id."""
    if scheduled:
        return "scheduled-" + scheduled
    data = data or {}
    cid = str(data.get("conversation_id") or "").strip()
    if not cid and has_request_context():
        cid = str(request.args.get("conversation_id") or "").strip()
    if cid:
        return cid
    if not DEMO:
        try:
            import operator_session as _sess_store
            cid = _sess_store.active_id()
        except Exception:
            cid = ""
    return cid or "legacy"


def _thread_control_guard(data, conversation_id: str):
    """Browser clients carry a device id; only the live controller may mutate.

    Schedulers, MCP and old clients send no id and retain their existing API
    contract. The lease protects competing cockpit tabs, not trusted server
    integrations.
    """
    if DEMO:
        return None
    client_id = str((data or {}).get("client_id") or "").strip()
    if not client_id:
        return None
    import operator_session as _sess_store
    try:
        control = _sess_store.touch_presence(
            conversation_id, client_id,
            str((data or {}).get("device_label") or ""))
    except (KeyError, ValueError) as exc:
        return jsonify(ok=False, error=str(exc)), 409
    if control["can_control"]:
        return None
    return jsonify(
        ok=False,
        error=f"thread is open on {control['controller_label']}; take over to continue",
        controller_label=control["controller_label"]), 409


@bp.route("/operator/dispatch", methods=["POST"])
def operator_dispatch():
    """Start a headless Claude Code agent (as the chosen persona) to do the task
    on the shared Chrome — on the subscription, no Discord, no API key."""
    data = request.get_json(silent=True) or request.form
    bot = (data.get("bot") or "").strip()
    task = (data.get("task") or "").strip()
    if not task:
        return jsonify(ok=False, error="empty task"), 400
    model = (data.get("model") or "").strip()
    effort = (data.get("effort") or "").strip()
    conversation_id = _conversation_for(data)
    control_error = _thread_control_guard(data, conversation_id)
    if control_error:
        return control_error
    # surface: explicit in the request, else the cockpit's active pick. The
    # runner re-validates (gating is server-side, not a UI courtesy).
    surface = (data.get("surface") or _active_surface["name"] or "browser").strip()
    real_ok = bool(data.get("real_ok"))
    if DEMO:
        # #27: the demo agent may drive the browser or the ISOLATED demo sandbox
        # container — never the real desktop (coerced, no confirm honored).
        if surface not in ("browser", "desktop-sandbox"):
            surface = "browser"
        real_ok = False
    if surface == "desktop-real":
        # pre-flight the capture BEFORE any side effect (surface flip, run
        # start): with the Windows console locked, win_capture returns a
        # phantom blank 1024×768 screen (verified 2026-07-11) and the run
        # would burn itself clicking into a white void.
        err = _desktop_real_preflight()
        if err:
            return jsonify(ok=False, error=err), 409
    if surface != _active_surface["name"] \
            and surface in [s["key"] for s in _SURFACE_DEFS]:
        _active_surface["name"] = surface     # feed follows the dispatch
    # A browser task and its visible feed are one transaction: do not start the
    # agent unless the exact Playwright/CDP path has produced a fresh frame.
    # The old best-effort block swallowed every attach failure, so the runner
    # cheerfully began "Browsing" behind a SIGNAL LOST cockpit.
    if surface == "browser":
        browser_error = _streamer.require_ready()
        if browser_error:
            return jsonify(ok=False, error=browser_error), 503
    if DEMO:
        # public demo: gemma/agy runtime, model locked to the 2-entry demo list
        # (off-list → Flash 3.7 Low default). The tier lives in the model string
        # ("(Thinking)"/"(Low)"), so client-sent effort is discarded — the lock
        # owns effort. demo=True strips squad context/identity/tools.
        bot = "gemma"
        if model not in {m["value"] for m in OPERATOR_MODELS_DEMO}:
            model = OPERATOR_MODELS_DEMO[0]["value"]
        if surface == "desktop-sandbox":
            # Flash has no computer-use tools (the owner 2026-07-09) — a sandbox run
            # would just shell around. Desktop runs force Sonnet.
            model = "Claude Sonnet 4.6 (Thinking)"
        effort = ""
        r = operator_agent.runner.start(bot, task, model=model, effort=effort,
                                        demo=True, surface=surface,
                                        conversation_id=conversation_id)
    else:
        r = operator_agent.runner.start(bot, task, model=model, effort=effort,
                                        surface=surface, real_ok=real_ok,
                                        conversation_id=conversation_id)
        if r.get("ok"):
            # An untitled conversation takes the name of the first task run in
            # it, so the switcher reads as a list of errands rather than a
            # column of "New chat". Best-effort by contract.
            import operator_session as _sess_store
            _sess_store.title_if_unset(task, conversation_id=conversation_id)
    return (jsonify(r), 200) if r.get("ok") else (jsonify(r), 409)


# ── Saved tasks (#30) ──────────────────────────────────────────────────────
# A saved task = a named, re-runnable dispatch bundle (prompt + preferred sites
# + default bot/model/effort + optional start_url). v1: no scheduling, and
# preferred-sites is a prompt HINT not a hard sandbox (both deferred — see the
# handoff spec). Persistence + slug logic live in operator_tasks.py; these routes
# are thin wrappers that, on /run, do exactly what /operator/dispatch does.
# DEMO (the owner 2026-07-09): available, but against a demo-scoped store — the demo
# instance MUST set OPERATOR_TASKS_PATH so visitors never see the squad's tasks.
# Demo saves strip bot/schedule (forced at run / scheduler never runs in demo),
# the store is capped, and /run applies the same lock as /operator/dispatch.

DEMO_TASKS_MAX = 24     # demo store cap — visitors can't grow the file unboundedly


def _demo_tasks_guard():
    """Fail closed: demo saved tasks REQUIRE the demo-scoped store. If the demo
    launch didn't set OPERATOR_TASKS_PATH, serving these routes would expose the
    owner's real task store — keep the old 404 gate instead."""
    if DEMO and not _os.environ.get("OPERATOR_TASKS_PATH"):
        return jsonify(ok=False, error="not available"), 404
    return None


def _task_public(slug: str, t: dict) -> dict:
    """The safe outward shape of a saved task for the UI (slug + the fields the
    dispatch box needs to populate, plus stamps)."""
    return {
        "slug": slug,
        "name": t.get("name", ""),
        "prompt": t.get("prompt", ""),
        "vars": operator_tasks_store.extract_vars(t.get("prompt", "")),
        "sites": t.get("sites", []),
        "bot": t.get("bot", ""),
        "model": t.get("model", ""),
        "effort": t.get("effort", ""),
        "start_url": t.get("start_url", ""),
        "schedule": t.get("schedule", ""),
        "created": t.get("created"),
        "last_run": t.get("last_run"),
    }


@bp.route("/operator/tasks", methods=["GET", "POST"])
def operator_tasks():
    """GET → list saved tasks. POST → create/update (body = data-model fields);
    validates non-empty name+prompt; returns the slug."""
    guard = _demo_tasks_guard()
    if guard:
        return guard
    if request.method == "GET":
        tasks = operator_tasks_store.load_tasks()
        items = [_task_public(s, t) for s, t in sorted(tasks.items())]
        return jsonify(ok=True, tasks=items)
    # POST create/update
    data = request.get_json(silent=True) or request.form
    if DEMO:
        tasks_now = operator_tasks_store.load_tasks()
        if (data.get("slug") or "").strip() not in tasks_now \
                and len(tasks_now) >= DEMO_TASKS_MAX:
            return jsonify(ok=False, error="demo task limit reached"), 400
    slug, err = operator_tasks_store.save_task({
        "slug": (data.get("slug") or "").strip(),
        "name": data.get("name"),
        "prompt": data.get("task") or data.get("prompt"),
        "sites": data.get("sites"),
        # bot/schedule are dead fields in demo: bot is forced at run and the
        # scheduler never starts on a public instance — don't store them.
        "bot": "" if DEMO else data.get("bot"),
        "model": data.get("model"),
        "effort": data.get("effort"),
        "start_url": data.get("start_url"),
        "schedule": "" if DEMO else data.get("schedule"),
    })
    if err:
        return jsonify(ok=False, error=err), 400
    return jsonify(ok=True, slug=slug)


@bp.route("/operator/tasks/<slug>/run", methods=["POST"])
def operator_task_run(slug):
    """Load a saved task and dispatch it — mirrors /operator/dispatch exactly,
    plus: optional nav to start_url first, and a preferred-sites prompt preamble.
    Stamps last_run. Body may override bot/model/effort (the UI's editable path);
    absent overrides fall back to the task's stored defaults."""
    guard = _demo_tasks_guard()
    if guard:
        return guard
    data = request.get_json(silent=True) or request.form or {}
    r, status = _dispatch_saved_task(slug, data)
    return jsonify(r), status


def _dispatch_saved_task(slug: str, overrides: dict | None = None,
                         *, scheduled: bool = False) -> tuple[dict, int]:
    """The shared saved-task dispatch path — the ▶ run route and the scheduler
    (operator_schedule) both come through here."""
    overrides = overrides or {}
    conversation_id = _conversation_for(
        overrides, scheduled=slug if scheduled else "")
    t = operator_tasks_store.get_task(slug)
    if not t:
        return {"ok": False, "error": "no such task"}, 404
    bot = (overrides.get("bot") or t.get("bot") or "").strip()
    model = (overrides.get("model") or t.get("model") or "").strip()
    effort = (overrides.get("effort") or t.get("effort") or "").strip()
    prompt = (t.get("prompt") or "").strip()
    if not prompt:
        return {"ok": False, "error": "task has empty prompt"}, 400
    # {{variables}} (1.0.13): fill from overrides.vars; anything left unfilled
    # bounces with the missing names so the client can collect values (and the
    # scheduler's bare dispatch of a var task fails loudly instead of running
    # a prompt full of literal braces — save_task also refuses that combo).
    if operator_tasks_store.extract_vars(prompt):
        prompt, missing = operator_tasks_store.fill_vars(
            prompt, overrides.get("vars") or {})
        if missing:
            return {"ok": False, "vars": missing,
                    "error": "task needs variable values: "
                             + ", ".join(missing)}, 400

    # Saved tasks and scheduled runs obey the same fail-closed browser gate as
    # an ordinary dispatch. No alternate entry point may launch blind.
    browser_error = _streamer.require_ready()
    if browser_error:
        return {"ok": False, "error": browser_error}, 503

    # Optional: navigate to the task's start_url before handing off to the agent.
    start_url = (t.get("start_url") or "").strip()
    if start_url:
        try:
            _streamer.run_action({"kind": "goto", "value": start_url})
        except Exception:
            pass

    # v1 preferred-sites = prompt hint (not a hard sandbox).
    preamble = operator_tasks_store.sites_preamble(t.get("sites", []))
    task_prompt = f"{preamble}{prompt}" if preamble else prompt

    if DEMO:
        # same lock as /operator/dispatch: forced runtime, model allowlist,
        # no client effort. Saved-task runs are browser-surface, so no
        # sandbox model force needed here. demo=True strips squad identity.
        bot = "gemma"
        if model not in {m["value"] for m in OPERATOR_MODELS_DEMO}:
            model = OPERATOR_MODELS_DEMO[0]["value"]
        r = operator_agent.runner.start(bot, task_prompt, model=model,
                                        effort="", demo=True,
                                        conversation_id=conversation_id)
    else:
        r = operator_agent.runner.start(
            bot, task_prompt, model=model, effort=effort,
            conversation_id=conversation_id)
    if r.get("ok"):
        operator_tasks_store.mark_run(slug)
        return r, 200
    return r, 409


@bp.route("/operator/tasks/<slug>", methods=["DELETE"])
def operator_task_delete(slug):
    """Remove a saved task."""
    guard = _demo_tasks_guard()
    if guard:
        return guard
    return jsonify(ok=operator_tasks_store.delete_task(slug))



@bp.route("/operator/agent")
def operator_agent_state():
    """The running agent's reasoning/replies since `since` epoch (for the chat)."""
    try:
        since = float(request.args.get("since", "0") or 0)
    except (TypeError, ValueError):
        since = 0.0
    return jsonify(operator_agent.runner.snapshot(
        since, conversation_id=_conversation_for(request.args)))


@bp.route("/operator/agent/stop", methods=["POST"])
def operator_agent_stop():
    data = request.get_json(silent=True) or request.form or {}
    conversation_id = _conversation_for(data)
    control_error = _thread_control_guard(data, conversation_id)
    if control_error:
        return control_error
    return jsonify(operator_agent.runner.stop(
        conversation_id=conversation_id))


@bp.route("/operator/agent/say", methods=["POST"])
def operator_agent_say():
    """Mid-run steering (1.0.12): queue a user message for the LIVE run —
    delivered mid-loop by the steer hook (claude runtime) or as one more
    resumed turn at the exit seam. 409 when nothing is running (the client
    falls back to a normal dispatch). Allowed in demo: the demo runner is a
    separate process AND its steer queue is a separate file (demo-scoped
    OPERATOR_STEER_PATH + the .demo default backstop — a shared queue would
    let a visitor steer a live production run; found in review 2026-07-11),
    and a visitor already controls the task text — no new surface."""
    data = request.get_json(silent=True) or request.form
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify(ok=False, error="empty message"), 400
    if len(text) > 4000:
        return jsonify(ok=False, error="message too long (max 4000 chars)"), 413
    conversation_id = _conversation_for(data)
    control_error = _thread_control_guard(data, conversation_id)
    if control_error:
        return control_error
    r = operator_agent.runner.steer(
        text, conversation_id=conversation_id)
    return (jsonify(r), 200) if r.get("ok") else (jsonify(r), 409)


@bp.route("/operator/agent/reset", methods=["POST"])
def operator_agent_reset():
    """Clear the agent's conversation memory (wired to the operator trash button)."""
    data = request.get_json(silent=True) or {}
    bot = data.get("bot", "")
    conversation_id = _conversation_for(data)
    control_error = _thread_control_guard(data, conversation_id)
    if control_error:
        return control_error
    return jsonify(operator_agent.runner.reset_session(
        bot, conversation_id=conversation_id))


@bp.route("/operator/driver-status")
def operator_driver_status():
    """Who's driving + recent bot-action trail (tap log) + the driver's reasoning
    (transcript tail) newer than the client's  epoch."""
    drv = _current_driver()
    try:
        since = float(request.args.get("since", "0") or 0)
    except (TypeError, ValueError):
        since = 0.0
    reasoning = []
    bot = (request.args.get("bot") or (drv or {}).get("bot") or "").strip()
    # in demo mode the agent has no squad transcript to tail (and we must not read
    # any squad bot's transcript) -> the live trace comes from the agent runner only.
    if bot and not DEMO:
        reasoning = _tail_reasoning(bot, since)
    return jsonify(driver=drv, events=_recent_events(30), reasoning=reasoning)


# ── Stage 2: reasoning relay (the owner 2026-06-26) ──────────────────────────────
# Tail the driving bot's live session transcript JSONL → surface its assistant
# text (its reasoning/replies) so the operator chat shows thinking, not just
# clicks. Per-bot transcript dir = <config_dir>/projects/<cwd-slug>/; we take the
# most-recently-modified .jsonl there (the live session).
import glob as _glob

# bot → (config_dir, cwd) used to locate its transcript project dir.
_BOT_PROJECT = {
    "claude-a":     ("~/.claude",            "~/agents/claude-a"),
    "claude-c":  ("~/.claude",            "~/agents/claude-c"),
    "claude-d": ("~/.claude",            "~/agents/claude-d"),
    "claude-b":      ("~/.config/claude-b",        "~"),
    "gpt":        (None, None),  # different arch; no claude transcript
}


def _slug(path: str) -> str:
    """Claude's project-dir slug: the abspath with /._ → -."""
    ap = _os.path.abspath(_os.path.expanduser(path))
    return ap.replace("/", "-").replace("_", "-").replace(".", "-")


def _transcript_file(bot: str) -> str | None:
    """Newest .jsonl for this bot's live session, or None."""
    cfg_cwd = _BOT_PROJECT.get(bot)
    if not cfg_cwd or not cfg_cwd[0]:
        return None
    cfg, cwd = cfg_cwd
    d = _os.path.join(_os.path.expanduser(cfg), "projects", _slug(cwd))
    cands = _glob.glob(_os.path.join(d, "*.jsonl"))
    if not cands:
        # fallback: newest jsonl anywhere under this config's projects
        cands = _glob.glob(_os.path.join(_os.path.expanduser(cfg), "projects", "*", "*.jsonl"))
    if not cands:
        return None
    return max(cands, key=lambda f: _os.path.getmtime(f))


def _assistant_text(msg: dict) -> str:
    """Extract plain assistant text from a transcript line's message.content."""
    m = msg.get("message") or {}
    content = m.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return " ".join(p for p in parts if p).strip()
    return ""


def _tail_reasoning(bot: str, since_ts: float, limit: int = 8) -> list:
    """Return up to `limit` recent assistant messages (text) newer than since_ts,
    as {text, ts}. Best-effort; never raises."""
    f = _transcript_file(bot)
    if not f:
        return []
    out = []
    try:
        # read only the tail for cheapness
        with open(f, encoding="utf-8") as fh:
            lines = fh.readlines()[-120:]
        for ln in lines:
            try:
                d = _json.loads(ln)
            except Exception:
                continue
            if d.get("type") != "assistant":
                continue
            ts = d.get("timestamp")
            # timestamp is ISO; convert to epoch for comparison
            ep = _iso_epoch(ts)
            if ep <= since_ts:
                continue
            txt = _assistant_text(d)
            if txt:
                out.append({"text": txt[:400], "ts": ep})
    except OSError:
        return []
    return out[-limit:]


def _iso_epoch(ts) -> float:
    """ISO-8601 string → epoch seconds; 0 on failure."""
    if not ts:
        return 0.0
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


import subprocess as _sp

# bot → marker to find its running session. cwd match for the agent-dir bots;
# claude-b runs from ~ so match its CLAUDE_CONFIG_DIR in the environ instead.
_BOT_LIVE_CWD = {
    "claude-a": "/claude-agents/claude-a",
    "claude-c": "/claude-agents/claude-c",
    "claude-d": "/claude-agents/claude-d",
}
_BOT_LIVE_ENV = {"claude-b": ".claude-alt"}


def _live_bots() -> set:
    """Which driver bots have a running `claude --channels` session right now."""
    live = set()
    try:
        out = _sp.run(["pgrep", "-f", "claude --channels"], capture_output=True,
                      text=True, timeout=6, stdin=_sp.DEVNULL).stdout
        pids = [x for x in out.split() if x.isdigit()]
        for pid in pids:
            try:
                cwd = _os.readlink(f"/proc/{pid}/cwd")
            except OSError:
                cwd = ""
            for bot, marker in _BOT_LIVE_CWD.items():
                if marker in cwd:
                    live.add(bot)
            # claude-b: check environ for its config dir
            if _BOT_LIVE_ENV:
                try:
                    with open(f"/proc/{pid}/environ", "rb") as fh:
                        env = fh.read().decode("utf-8", "ignore")
                    for bot, mk in _BOT_LIVE_ENV.items():
                        if mk in env:
                            live.add(bot)
                except OSError:
                    pass
    except Exception:
        pass
    # gpt is a service bot (always-on if its unit is active) — but it can't drive
    # reliably (one MCP slot, IBKR), so we don't mark it live for driving.
    return live


# Model picker options. The VALUE is the alias (opus/sonnet/haiku) — claude
# resolves an alias to the *latest* of that family, so the actual model the agent
# runs is always current. The LABEL is the human version; bump these two lines
# when a family's latest version changes (the only manual touch-point).
OPERATOR_MODELS = [
    {"value": "opus", "label": "Opus 5"},
    {"value": "claude-sonnet-5", "label": "Sonnet 5"},
    {"value": "haiku", "label": "Haiku 4.5"},
]
# claude-a-only roster (the owner 2026-07-22): adds Fable 5 (Mythos-class, above Opus)
# on top of the base Claude list. claude-b keeps the base roster — the models
# endpoint branches on the driver key, so scope stays per-bot.
OPERATOR_MODELS_CLAUDE_A = [
    {"value": "claude-fable-5", "label": "Fable 5"},
] + OPERATOR_MODELS
# codex/gpt models (default gpt-5.6-sol low per the owner). The 5.6 family ships three
# capability tiers (Sol flagship / Terra balanced / Luna fast) — each a distinct
# -m id with its OWN effort ladder (see EFFORT_BY_MODEL in operator.html):
# Sol adds max+ultra, Luna caps at minimal/low. Effort is the separate picker.
OPERATOR_MODELS_GPT = [
    {"value": "gpt-5.6-sol", "label": "GPT-5.6 Sol"},
    {"value": "gpt-5.6-terra", "label": "GPT-5.6 Terra"},
    {"value": "gpt-5.6-luna", "label": "GPT-5.6 Luna"},
    {"value": "gpt-5.5", "label": "GPT-5.5"},
]
# gemma drives via agy (Antigravity) — exposes the full agy model lineup on the owner's
# flat Google sub. Gemini families use the effort picker for tier; the Claude/GPT-OSS
# ones have a fixed tier baked in (no effort). start() folds family+effort into the
# agy --model string. Gemini families take a bare slug (agy applies --effort
# separately); the fixed-tier entries bake their tier into the name and are
# dispatched WITHOUT --effort. NB (2026-07-24): agy stopped accepting the old
# "Gemini 3.5 Flash (High)" display form together with --effort — it now errors
# "--effort is not supported for model …". Gemini values must be slugs.
OPERATOR_MODELS_GEMMA = [
    {"value": "gemini-3.7-flash", "label": "3.7 Flash"},
    {"value": "Claude Sonnet 4.6 (Thinking)", "label": "Sonnet 4.6"},
    {"value": "Claude Opus 4.6 (Thinking)", "label": "Opus 4.6"},
    {"value": "GPT-OSS 120B (Medium)", "label": "GPT-OSS 120B"},
]


# public demo: LOCKED 2-model choice on the gemma/agy runtime (the owner 2026-07-09):
# Flash 3.7 Low default (first = picker default + server fallback), Sonnet 4.6
# as the heavier alt. Tier is baked into each value — the effort control is
# hidden in the demo UI, the lock owns effort (dispatch sends effort="", so the
# baked-tier form is what agy gets).
OPERATOR_MODELS_DEMO = [
    {"value": "gemini-3.7-flash-low", "label": "3.7 Flash"},
    {"value": "Claude Sonnet 4.6 (Thinking)", "label": "Sonnet 4.6"},
]


@bp.route("/operator/models")
def operator_models():
    if DEMO:
        return jsonify(models=OPERATOR_MODELS_DEMO)
    driver = request.args.get("driver", "")
    if driver == "gpt":
        return jsonify(models=OPERATOR_MODELS_GPT)
    if driver == "gemma":
        return jsonify(models=OPERATOR_MODELS_GEMMA)
    if driver == "claude-a":
        return jsonify(models=OPERATOR_MODELS_CLAUDE_A)
    return jsonify(models=OPERATOR_MODELS)


# ── background housekeeping (#2 scheduled tasks + #3 completion pings) ────────
# Started at import (the server imports this module once); the thread is a
# daemon and a no-op when OPERATOR_SCHEDULER=0. Never in the demo — a public
# instance must not fire stored prompts on a clock.
if not DEMO:
    try:
        import operator_schedule as _op_sched
        _op_sched.start(run_fn=lambda slug: _dispatch_saved_task(
                            slug, scheduled=True)[0],
                        runner=operator_agent.runner)
    except Exception:  # noqa: BLE001 — housekeeping must never block the app
        pass
