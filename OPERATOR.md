# Operator

Instructions for every agent working in this module. `CLAUDE.md` and `AGENTS.md`
are symlinks to this file.

The live browser / computer-use agent cockpit. Flask blueprint (`operator_view.py`)
+ headless agent runner (`operator_agent.py`) + single-file UI (`templates/operator.html`),
mounted by the host-app server and served at `/squad/operator` over Tailscale.

## Version increments are release gates

Every Operator version increment must include a full version-number sweep in the
same change. Follow `RELEASING.md` before calling the bump complete: check runtime
metadata, every displayed version, current documentation, applicable packaging,
generated/demo surfaces, and version assertions. Updating `OP_VERSION` alone is
not sufficient.

Preserve historical release numbers and separately versioned dependencies,
protocols, schemas, and asset-cache revisions. Never blanket-replace a version.
Run the offline version checks, verify the served version when deployment is in
scope, and explicitly report any public mirror, package, or demo left unchanged.
Do not spend model quota on a version sweep.

## ⚠️ Feed-death post-mortem (2026-06-26→27) — READ THIS

An all-nighter where the live feed broke catastrophically. Root causes were almost all
**self-inflicted** while "improving" a feed that was already basically fine. What actually
broke it and how it came back:

### 1. The catastrophic bug: a JS temporal-dead-zone crash (THE one)
- Symptom: feed stuck on "Connecting…", agent dead, browser unresponsive — but the
  SERVER was 100% healthy in every test (frames produced, status `live`, stream delivered
  10MB through the real `:8443` path).
- Cause: a "MODE-seed" change made early init run `applyMode()→setFollowUp()`, which
  touched `_inFlight` / `_queue` **before** their `let`/`const` declarations later in the
  script → **`Cannot access '_inFlight' before initialization`** thrown at startup →
  **ALL page JS halted.** The `<img>` feed still rendered sometimes (no JS needed) which
  masked it. That single crash = the entire "server-fine-but-client-dead" paradox.
- Fix: **hoist `_inFlight`/`_queue` declarations to the top of the script** (before any
  early-init code can touch them).

### 2. A self-inflicted feed-killer: the stale-stream watchdog
- A watchdog I added assumed the `<img>` `load` event fires per-frame. For an MJPEG
  `multipart/x-mixed-replace` stream, **`load` fires ONCE** (first frame) and never again
  while healthily streaming. So ~6s in, the watchdog decided the live stream was "frozen"
  and force-reconnected it every poll → killed the working feed ("works then dies ~6s in").
- Fix: **removed the watchdog entirely. Do not re-add it.**

### 3. A real latent bug: single-threaded dev server
- `host-app/server.py` ran `app.run()` **without `threaded=True`**. The long-lived
  `/operator/stream` MJPEG connection held the single worker thread forever, blocking ALL
  other requests (status polls, new streams). Fix: **`app.run(..., threaded=True)`** — keep it.

### Things that were NOT the cause (chased + cleared, don't re-chase)
- Tailscale proxy / HTTP-2 / Safari MJPEG (verified the pipe delivers frames fine).
- The capture loop / CDP session staleness (DEATHLOG proved the server grab loop healthy
  forever). My dedup/reattach/fps "fixes" were unnecessary and some regressed it — they
  were reverted; the streamer is back at the `77984ce` baseline.

### THE debugging lesson
Server-side tests and `curl` **cannot** catch a client-side JS init crash. To debug the
feed, **load the real page in a headless browser WITH a representative `localStorage`
session** (`operator-session-v1`) and watch `pageerror` — that's the only thing that
caught the TDZ. Instrument the actual failing layer early instead of thrashing the server.

## Hard rules
- **Don't trial-and-error the streamer live.** It was already fine; over-tuning broke it.
- **Never re-add the stale-stream watchdog.**
- **Never put a ResizeObserver (or any self-measuring trigger) on the viewport-follow
  beacon.** 4f09e2b did; the cockpit's own layout shifts (scrollbar toggles, rail
  animation, frame swaps) then beaconed as "resizes" → remote reflow → repeat = the
  2026-07-22 resize strobe, and four rounds of server-side machinery (dead-band,
  rate-limit, per-frame re-assert) treated symptoms before the loop was cut at the
  source. Beacons must be user-driven only: `window.resize` + explicit calls on
  discrete user actions (rail-drag end via `_stageFollow`).
- **Keep `threaded=True`** on the server.
- **Declare vars before any early-init code references them** (TDZ).
- Template and `server.py` changes go live through `modules/host-app/deploy.sh`:
  the server runs from a pinned release, so restarting the service alone changes
  nothing. `operator_agent.py` changes are per-run. See [[project_operator]] / host-app #218.
