<h1 align="center">Operator</h1>
<p align="center"><b>Computer-Using Agent</b></p>

<p align="center">
  <img src="https://img.shields.io/badge/version-0.3.8-blue" alt="version">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="license">
  <img src="https://img.shields.io/github/languages/top/jeffbai996/operator" alt="top language">
  <img src="https://img.shields.io/badge/python-3.11+-3776ab" alt="python">
</p>

<p align="center">
  <img src="docs/img/operator-geoguessr.jpeg" alt="Operator driving a live browser">
</p>

<p align="center"><sub><i>Operator's GPT agent reasoning through a live GeoGuessr round — left: the interleaved thinking + action trace (Browsing / Reading / Clicking) with a live status card; right: the actual browser it's driving, streamed frame-by-frame.</i></sub></p>

---

A live **browser / computer-use agent cockpit**. Watch a real Chrome (or desktop) in real time, steer it manually, or hand control to a subscription-backed agent — Claude or GPT — that drives the browser and reports back.

> **Inspired by OpenAI's Operator.** This project borrows the name and the spirit of a watch-the-agent-drive interface. It is an independent implementation, not affiliated with, endorsed by, or derived from OpenAI.

> **MIT licensed** — free to use, modify, and distribute. See [`LICENSE`](LICENSE).

---

## What it does

| | |
|---|---|
| **Live view** | MJPEG stream of an attached Chrome via CDP `Page.captureScreenshot`. |
| **Manual steer** | Click / type / scroll / press-hold / drag flow straight through to the page. |
| **Agent drive** | `claude-a` + `claude-b` (Claude) and `gpt` (Codex), all on subscription auth — no metered API keys. Conversation is shared across bot switches and persisted across restarts. |
| **Trace** | Interleaved thinking + actions; commands and URLs render as code blocks, element targets as plain text; per-turn step counts; modern error blocks that surface the failure reason. |
| **UX** | MAN/AUTO modes, drag-to-resize chat, live font controls, mobile layout, self-healing feed (flicker-free, auto-relaunch on a wedged Chrome). |

---

## Layout

```text
__init__.py               exports bp (Flask blueprint) + runner (AgentRunner)
operator_view.py          blueprint: streamer (CDP screenshots) + /operator routes
operator_agent.py         AgentRunner: claude -p / codex exec, transcript, action labels
templates/operator.html   the whole UI (CSS + JS, single file)
align_audit.py            dev tool: measures header / urlbar alignment
```

---

## Run

Mounted as a Flask blueprint by a host app — it registers `operator_view.bp`, the template extends the host's `_base.html`, and it's served behind the host app (optionally a reverse proxy / tunnel).

---

## Changelog

**v0.3.8** — major mobile + reliability pass.
- **Mobile bottom-sheet redesign**: browser fills the screen, the chat is a draggable sheet (peek / half / full). The browser pane fits the *visible* area above the sheet (no black band, page stays visible at half-height). At **peek**, the sheet collapses to the Message box plus a full-width one-row status bar (spinner · `Ready`/`<bot>` · current action · caret). The site header is kept (so you can navigate out); headerless is reserved for the fullscreen toggle.
- **Mobile input/zoom**: focusing the Message box no longer zooms the viewport (focus-time viewport lock, so native pinch/zoom still works otherwise); the URL bar is no longer hidden under the header.
- **Agent vision**: DOM/snapshot by default (fast), but `browser_take_screenshot` (real pixels) for visual tasks — snapshot is blind to images/maps/video/canvas/game graphics (it was guessing blind on visual tasks).
- **Status card**: live status reads present-continuous ("Taking screenshot…", "Browsing"); past tense stays in the completed trace. Tool labels generalize to present-continuous (`fetch_messages` → "Fetching messages") with a code-chip fallback for unknown verbs. `<bot>` sits inline with the state; idle reads `<bot> · idle`. SIGNAL LOST is spinner-only and no longer flickers on transient feed hiccups (only a sustained drop shows it).
- **Streaming perf**: frame-dedup — a static page streams ~0.5fps (heartbeat only) instead of full-rate, big battery/data win; the moving feed stays smooth.
- **Theme/polish**: deeper light-mode surfaces + readable disclaimer/hover; tab `+`/close as SVGs; wider tab spacing; manual mode persistently shows "no screenshots while you steer"; bigger jump-to-latest caret; symmetric desktop margins.

**v0.3.7** — scroll-through (mouse wheel + iPad touch vertical-swipe scroll the live page); status-card minimize to a slim pill; status subline `<bot> · <action> <emoji>` with the bot bold in every state; clean red ring on error (no X); animated model/effort picker switching; tighter chat code blocks.

**v0.3.6** — per-message hover timestamps (smooth reveal); edit/retry the last prompt (no branches — continues from that point); status subline `<bot> · <action>` with the bot semibold, animating on each action change; status fonts scale with the +/− control; "Starting up…" status on bot launch; matched gpt/claude action verbiage.

**v0.3.5** — tab UI: square close button, open/pop/close animations, home / last-tab / new-tab go to the browser's new-tab page; the live view follows the agent into a newly-opened tab; agents nudged to navigate in-place rather than spawning a tab per step.

**v0.3.4** — sliding MAN/AUTO segmented control (thumb slides + color crossfade, no jank); mode persists across refresh (no slide on restore); manual mode shows a clean "Manual" notice + "Ready" status; restored the in-flight trace head (spinner + live action label → checkmark), collapsible mid-run.

**v0.3.3** — agent cursor (CDP click-capture → smooth GPT-Agent-style glide, hidden in manual mode); browser zoom in/out/reset + back/forward chrome; URL bar Google-searches non-URL input; modern lock hover tooltip; darker theme-aware code blocks; chrome icons sized correctly (flex-collapse fix).

**v0.3.2** — manual-mode card redesign (warn triangle) + animate-in; convo dims rather than clears in MAN; MAN/AUTO persists across refresh; mobile bottom-sheet chat; theme-toggle + nav fixes; stderr-sourced specific error reasons.

**v0.3.1** — trace fences commands + URLs only (element names render plain); stderr captured so failures surface a specific reason; error-mark + header alignment nudges; license / README split out to this repo.

**v0.3.0** — Operator version label; markdown fixed (fenced blocks → `<pre>`, bare URLs auto-linked); trace command/URL details as code blocks + step-count header; modernized error blocks.

**v0.2.x** — drag-to-resize chat; mobile scroll + capped chat height; iOS focus-zoom fix; chrome made non-selectable; clicks on eval-disabled sites via CDP `getLayoutMetrics`; lock moved inside the URL box.

**v0.2.0** — multi-driver (claude-a / claude-b / gpt) on subscription auth; shared cross-bot transcript persisted across restarts; browser-first agent behavior.

**v0.1.x** — feed hardening (flicker-free, wedge auto-recovery, SIGNAL-LOST overlay); status card; MAN/AUTO; trace with per-action emoji.

**v0.1.0** — initial live browser stream (CDP MJPEG) + manual steering.
