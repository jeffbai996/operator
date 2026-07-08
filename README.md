<h1>Operator</h1>
<p><b>Computer-Using Agent</b></p>

<p>
  <img src="https://img.shields.io/badge/version-1.0.0-blue" alt="version">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="license">
  <img src="https://img.shields.io/github/languages/top/jeffbai996/operator" alt="top language">
  <img src="https://img.shields.io/badge/python-3.11+-3776ab" alt="python">
</p>

<p>
  <img src="docs/img/operator-openrsc.png" alt="Operator playing a live canvas game">
</p>

<p><sub><i>Operator's agent playing RuneScape Classic (OpenRSC) live — left: the interleaved thinking + action trace (“We're fighting!” → Clicking → “Rat is dead!”) reasoning over what it sees on the canvas; right: the actual game it's driving, streamed frame-by-frame. The agent reads the canvas by screenshot and clicks by pixel coordinate — no DOM to rely on.</i></sub></p>

<p align="center"><sub><i>More: <a href="docs/img/operator-geoguessr.jpeg">reasoning through a live GeoGuessr round</a>.</i></sub></p>

---

A live **browser / computer-use agent cockpit**. Watch a real Chrome in real time, steer it manually, or hand control to a subscription-backed agent — Claude, GPT, or Gemini — that drives the browser and reports back.

> **Inspired by OpenAI's Operator.** This project borrows the name and the spirit of a watch-the-agent-drive interface. It is an independent implementation, not affiliated with, endorsed by OpenAI, or derived from any OpenAI products.

> **MIT licensed** — free to use, modify, and distribute. See [`LICENSE`](LICENSE).

---

## Quickstart

```bash
git clone https://github.com/jeffbai996/operator
cd operator
pip install -r requirements.txt
cp .env.example .env          # optional — defaults are fine

# launch the browser the agent drives (logged-in, separate profile):
bash browse/chrome-attach.sh  # sign into your sites in the window it opens, once

python app.py                 # open http://127.0.0.1:5005
```

**Agent runtime — bring your own subscription** (no metered API key, the cheap path):
- **Claude** — install the `claude` CLI and `claude login` (creds in `~/.claude`)
- **GPT** — install the `codex` CLI and sign in (creds in `~/.codex`)

Operator detects whichever you have and drives the browser with it. An API-key
fallback is documented in `.env.example`, but driving a browser over the API is
expensive (a screenshot per step) — the logged-in CLI path is strongly preferred.

> **Status:** full hands-off computer-use shipped in **v1.0.0** — browser, an
> isolated sandbox desktop, and (gated, confirm-required) the real desktop, all
> with local perception and a fast macro controller for repetitive sequences.

## What it does

| | |
|---|---|
| **Live view** | MJPEG stream of an attached Chrome via CDP `Page.captureScreenshot`. |
| **Manual steer** | Click / type / scroll / press-hold / drag flow straight through to the page. |
| **Agent drive** | `claude-a` + `claude-b` (Claude) and `gpt` (Codex), all on subscription auth — no metered API keys. Conversation is shared across bot switches and persisted across restarts. |
| **Trace** | Interleaved thinking + actions; commands and URLs render as code blocks, element targets as plain text; per-turn step counts; modern error blocks that surface the failure reason. |
| **UX** | MAN/AUTO modes, drag-to-resize chat, live font controls, mobile layout, launchpad of saved tasks, a `/` slash palette, and a real scheduler (repeat/time/day → cron). |
| **Reliability** | Chrome launched once at server boot (no racy on-demand relaunch), scheduler fired-keys persisted across restarts, vision module loads without OCR present, and an env-tunable token-cap governor that stops a runaway vision run. |
| **Surfaces** | Browser (default), an isolated sandbox desktop (Xvfb), or the real desktop (gated — explicit per-session confirm, panic-STOP always on screen). Switch from a popover on the brand mark; the live feed follows. |
| **Perception** | Zero-token local vision (`vision/`): template/colour-blob target finding + OCR, per-game region maps, and grid/crop grounding overlays — the agent reads labeled targets instead of squinting at raw pixels. |
| **game_macro** | A planner/controller split (`control/`): the model emits a multi-step macro once, a local controller executes + verifies it at machine speed with zero mid-macro model calls, and only reports back on completion or surprise. |

---

## Layout

```text
__init__.py               exports bp (Flask blueprint) + runner (AgentRunner)
operator_view.py          blueprint: streamer (CDP screenshots) + /operator routes
operator_agent.py         AgentRunner: claude -p / codex exec, transcript, action labels
operator_tasks.py         saved-task store (name / prompt / bot / model / tools)
operator_schedule.py      cron matcher + background dispatcher, disk-persisted dedupe
templates/operator.html   the UI markup + JS (styles live in static/operator.css)
static/operator.css       the UI stylesheet (extracted from the template)
align_audit.py            dev tool: measures header / urlbar alignment
vision/                   local perception: template/colour targets, OCR, per-game maps
control/                  surface interface + game_macro controller + control MCP
computer-use/             sandbox (Xvfb) + real-desktop (PowerShell/WSL) backends
```

---

## Run

Mounted as a Flask blueprint by a host app — it registers `operator_view.bp`, the template extends the host's `_base.html`, and it's served behind the host app (optionally a reverse proxy / tunnel).

---

## Roadmap

**v1.1 — perception depth + the canvas-game showcase**
- Self-hosted OpenRSC demo (zero ToS risk) — the flagship RuneScape-class canvas run.
- Sprite-capture workflow: lift template sprites from live frames into `vision/maps/`.
- Map auto-calibration: derive region geometry from perception (blob grids) instead of static seed coordinates, so maps survive layout/viewport changes.
- Desktop + macro combined: `game_macro` on the sandbox desktop — grind a native app the way a canvas game is ground.
- OCR in anger: system tesseract, text conditions and chat/tooltip reading in real macros.

**v1.2 — continuity + driver parity**
- Long-lived controller sessions: controller state and watchers persist across `game_macro` calls; events push to the planner instead of being polled.
- Auto-replan loop: on a macro yield the planner re-decides and continues under a hard step/token budget — sustained autonomous play sessions.
- Driver parity: the operator-control MCP wired into the GPT and Gemini runtimes, so desktop surfaces and `game_macro` stop being Claude-only.

**Explicitly not planned**: twitch-reflex games (physics, not skill — a different control layer), and the real desktop as a default anything — it stays confirm-gated with STOP on screen.

## Changelog

**v1.0.0** — **full hands-off computer-use — perception, game_macro, desktop surfaces**. This fulfills the `v1.0.0` promise: the agent can now drive **three surfaces** — the logged-in browser (as before), an **isolated sandbox desktop** (Xvfb — nothing outside it can be touched), and the **real desktop** (gated: never the default, needs an explicit per-session confirm, panic-**STOP** always on screen). Switch from a popover on the brand mark; the live feed follows whichever surface is active, mid-session, no reconnect. **Local perception** (`vision/`): a `perceive` tool grounds the agent in labeled on-screen targets without a single extra model call — template + colour-blob matching, OCR text extraction, per-game region maps (Lichess, OpenRSC shipped), and an optional coordinate grid or region-crop overlay for when raw pixels are still the fastest read. **game_macro planner/controller split** (`control/`): instead of one LLM round-trip per click, the model emits a multi-step macro once — click-by-target-label, waits on local conditions, repeats — and a local controller executes and verifies it at machine speed with **zero mid-macro model calls**, bailing back to the planner only on completion or genuine surprise. **Trace integration**: every macro op and perception call streams into the same live action trace as browser tool calls, so a desktop run reads exactly like a browser one — thinking interleaved with what actually happened on screen.

**v0.9.0** — **coordinate contract + hardening**. Nailed down the coordinate contract across the surface stack: `BrowserSurface` records the device-px→CSS-px scale at each capture and converts on inject (CDP screenshots are device pixels, CDP input is CSS pixels — unconverted clicks land down-right of the target on any DPR>1 window), and `win_backend` does the equivalent image→physical scaling on the real desktop. `control/README.md` documents the planner/controller split and the safety model (shared STOP file, `desktop-real` refusing to construct without an explicit per-session confirm). Plus a **launch-error hardening fix**: a desktop-surface persona swap used `.format()` against mandate text containing literal braces (`computer{action:'screenshot'}`), which threw a bare `KeyError` that used to die silently — the whole prompt-build/MCP-config/persona-swap path is now wrapped so a dead launch surfaces a real error in the chat instead of leaving the run stuck in "running" forever.

**v0.8.0** — **the surface axis**. Before this, Operator only ever drove the browser. Now dispatch takes a **surface** — `browser` / `desktop-sandbox` / `desktop-real` — routed through a dedicated **control MCP** (`computer` / `perceive` / `game_macro`) instead of Playwright on desktop surfaces (a browser tool on a desktop run would just mislead the model). A **surface picker** lives in a popover off the brand mark, with a live-updating chip next to the version number; picking `desktop-real` demands an inline two-step confirm every session, never persisted. The live feed source switches with the surface (`_DesktopFeed` captures via the same sandbox/real backends the agent drives, at a gentler cadence than the browser's CDP frames) and a **panic STOP button** sits over the feed whenever a desktop run is live — it arms a shared kill-switch file that any in-flight macro or injection checks before its next op, so a stop lands even before the process tree dies.

**v0.7.2** — **stability + UI polish pass**. Chrome now launches **exactly once at server boot** and the old on-demand / on-wedge / on-dispatch auto-relaunch is gone — multiple call sites used to each independently decide Chrome was down and shell out to the launcher at the same time, spawning duplicate windows even with a lock around one of them; a wedged or manually-closed browser now surfaces cleanly and is restarted via the launch script instead. The scheduler's per-minute **fired-keys are persisted to disk** (atomic tmp+replace) so a restart inside the same minute doesn't double-fire a scheduled task. The vision module **lazy-imports `pytesseract`** so it loads even where OCR isn't installed. The UI stylesheet is **extracted to a served `static/operator.css`** (was inline in the template) — same look, cacheable and easier to read. Plus a **1Password autofill hint** (the agent tries the inline 1Password suggestion at any login before hunting for credentials), the agy step-by-step directive is factored into a testable constant, and assorted tab-drag / palette / favicon / font fixups.

**v0.7.1** — intermediate infra tag (folded into v0.7.2): scheduler persistence, CSS extraction, single-boot Chrome, lazy OCR, and the env-tunable token-cap governor (`OPERATOR_TOKEN_TURN_STOP` / `OPERATOR_TOKEN_RUN_STOP`).

**v0.7.0** — **saved tasks + the run governor**. The headline: OpenAI-Operator-style **saved tasks** — a **/ slash palette** in the composer (type "/" → filterable list, ↵ runs a task as stored, Tab loads it into the composer with its bot/model/effort applied, inline delete with click-again confirm), a **Save task modal** (name / "What would you like Operator to do?" / a tools-and-websites **pill field** with favicon autocomplete) opened from a floppy button in the urlbar, and a **launchpad** — saved tasks as cards on the idle stage, shown on fresh sessions until the conversation starts. Tasks can carry an optional 5-field **cron schedule**: a background thread dispatches them through the same path as ▶ (stdlib cron matcher, per-minute dedupe), and finished runs feed an **unseen-runs counter** (`/operator/unseen`) you can wire to a nav badge — it clears the moment the cockpit is viewed or polling. **Run governor**: per-run cumulative token tracking with hard caps (default 3M/turn, 20M/run, env-tunable) that auto-stop a runaway vision task like a human Stop tap, and an **image governor** in the MCP pipe (browse/mcp_image_governor.js) that downscales oversized screenshot blocks (long edge >1100px → 1024 JPEG) before the model ingests them — fail-open at every layer, ~2× context headroom on long vision runs (requires `sharp` next to the script; passes through untouched without it). UI: pointer-based **drag-to-reorder tabs** (works on touch — the old HTML5 drag never fired there), per-tab **favicons**, a live **site favicon in the urlbar lock slot**, a green/amber/red **nav status dot** + loading hairline, a quieter hamburger menu (soft hover, chip-styled shortcut keys), slightly darker default-dark surfaces, smaller urlbar icons, restrained entrance animations, and a radial vignette + grain under the launchpad.


<details>
<summary>Version history (click to expand)</summary>

**v0.6.8** — **mobile polish + agent reliability + trace cleanup**: model/effort pickers no longer hide long names (e.g. "Flash 3.5") behind the dropdown caret; larger default content scale on phones; picker padding tuned so words never tuck under the caret at any zoom. **Dispatch reliability**: a run whose process died without cleanly finishing no longer wedges every future dispatch ("X is already running" with nothing running) — liveness is verified and the reset button force-clears a stuck state. **Gemini live trace, for real**: the streaming fix is completed — the live-poll now waits for *this* run's trajectory instead of replaying a prior run's steps, and a step-by-step directive stops Gemini one-shotting its whole plan, so steps stream as they happen. **Cleaner action detail**: clicks/types show a human description ("Clicking — learn more link") instead of opaque element refs; tool args are read case-insensitively (so Gemini's CapitalCase args surface detail too); absolute filesystem paths are scrubbed from the trace. **Click-crash fix**: clicks no longer intermittently freeze the live feed and drop the cursor — a desynced CDP page handle made Playwright's high-level mouse calls block forever holding the frame lock; clicks now go through a raw CDP input dispatch that can't wedge. **Tab-following**: the live view now follows whichever tab is actually in the foreground, not just the newest one. **Scroll-up fixed**: the steer endpoint was silently dropping the wheel-delta fields, so scrolling up was indistinguishable from scrolling down server-side. Viewport-size detection no longer blocks indefinitely on a slow/unresponsive page (falls back to CDP layout metrics). The "stalled" watchdog now checks the agent process is actually dead instead of just quiet. Sonnet bumped to **Sonnet 5**.

**v0.6.7** — **trace + streaming polish**: agent thinking/tool steps now STREAM live during a turn for the Gemini/Antigravity driver (they were stalling until end-of-turn — the live-poll was flip-flopping between trajectory files; now it locks onto the run's file); the status card shows **Ready / Connecting** by live-browser state instead of a mode label; harmony-format reasoning tokens from open models (gpt-oss) are stripped so traces read clean; agent sessions isolated from the interactive session list.

**v0.6.6** — **reliability + cost + status-card pass**: clean interrupt handling (Stop reads "Interrupted", no phantom error card, next turn isn't stuck on a half-killed session) and the orphaned browser-tool process is reaped so the agent never hangs after the first turn; a **screenshot-economy** directive + a per-turn **token guard** that warns when a vision-heavy task's context balloons (long games re-sending accumulated screenshots can otherwise burn a huge amount of tokens); modern eased status-card spinner with smooth transitions and a "Reconnecting" state instead of a false "Ready" over a dropped feed; the action trace reveals search queries + tool args across drivers; the agent is steered off inspecting browser internals; unified header; and agent sessions are isolated so they don't clutter the interactive session list.

**v0.6.5** — **inline agent screenshots** + cleanup: when an agent reports a screenshot in its reply, it now renders inline in the chat (served from the run's output dir via a guarded `/operator/shot/<name>` route — basename-only, image-extension whitelist, path-traversal safe) instead of collapsing to a "took a screenshot" note. Gemini driver: the Playwright MCP it wires into the global CLI config is now stripped back out after each run, so a normal (non-Operator) session of the same CLI doesn't inherit the browser tool. **Search-query reveal + arg parity**: the trace now shows WHAT a tool acted on — `Searching ("the terms")`, file paths, commands — across every driver's tool set, not just a bare verb. Plus de-dup/label fixes across the action trace.

**v0.6.2** — **third driver + richer trace**: adds a **Gemini** driver (Google's Antigravity CLI, subscription-backed like the Claude/GPT paths) with its thinking + tool-call trace surfaced. Browser **gesture tools** (coordinate mouse down/move/up, drag) for canvas/board UIs. Markdown + code-block backgrounds in replies. Fixes: no spurious turn after Stop, accurate MCP action labels, code-block scroll no longer traps the page, last-tab handling.

**v0.5.9** — **smarter agent**: a sharper computer-use system prompt — act→wait→continue (waits for async loads), vision fallback when the DOM isn't working, scroll-to-find (both directions), never repeat a failed action, and dismiss cookie/consent banners by pixel-click instead of dead element-ref retries. Plus a prompt-injection guard (page content is data, not orders), stuck-loop backtracking, and an expanded take-control (hands back when genuinely unsure or the browser is visibly stuck — but always executes clear instructions). **Click accuracy fix**: vision clicks landed a few px off in attach mode (viewport/DPR mismatch) — now pixel-perfect. **UX**: fullscreen persists across refresh; inline trace details (coords/durations/short labels on one line).

**v0.5.8** — **control row + responsive header**: all header controls (MAN/AUTO, font −/+, clear, contrast, fullscreen) now sit on one tidy row at equal height, optically aligned. The chat rail can be dragged much narrower so the browser pane maximizes — as it narrows the header sheds chrome via container queries and the "Operator" wordmark *smoothly collapses* (the version label stays put, the status dot stays centered on the title). Also fixes the Manual-mode *Finish up* block leaking into user-entered manual mode (a `hidden` attribute that CSS was overriding).

**v0.5.7** — **Finish-up hand-back**: after Operator hands control to you (Take control), the Manual-mode panel shows a *Finish up* pill — tap it to optionally leave a note and hand control back, resuming the agent where it left off. Plus clickable browse-URL links in the trace, coordinate-click coords shown, back/forward no longer stalls the live feed, a rebuilt collapse caret, rounder button pills, and trace/output font tuning.

**v0.5.6** — hand-off polish: a turn ending in a *Take control* hand-off no longer also emits a redundant done/"no summary" line under the card; the card's status dot is now traffic-light yellow; SVG collapse caret; trace checkmark aligned to the rule; one error card per failed turn; held arrow keys scroll continuously (server-side key auto-repeat); refresh no longer re-appends the last messages; the ×N repeat badge is a centered circular pill; desktop chat-input text bumped a touch.

**v0.5.5** — **Take control** hand-off: when the agent hits a human-only gate (captcha, 2FA/OTP, a password login, a payment or “are you sure?” confirm) it now surfaces a *Take control* card in the chat instead of brute-forcing it; one click stops the agent, drops a “Took control” notice, and hands you the wheel in manual mode. **Verify-after-action**: the agent is directed to re-check the page after each consequential action (screenshot/snapshot → confirm it did what it intended → self-correct) so games and multi-step flows are more reliable. **Running plan**: it keeps a numbered plan + progress ledger across steps so long tasks don’t drift. **Trace polish**: consecutive identical actions coalesce into an animated ×N badge instead of flooding the trace with duplicate lines.

**v0.5.3** — native coordinate mouse tools (vision caps): `browser_mouse_drag_xy` etc. for board/canvas games (Lichess, GeoGuessr) and drag UIs; `browser_pdf_save` to save a page as PDF; held-key navigation (hold an arrow for smooth continuous map pan/rotate instead of laggy taps); interrupt-steer polish — a mid-run message closes the turn as “Steered after Xs” (real elapsed time) with no spurious entry; cleaner action labels (Clicking/Dragging) for the coordinate tools; trace alignment + bigger steps font.

**v0.5.2** — interrupt-steer: a message sent mid-run now stops the current turn and immediately redirects the agent (instead of queueing); the −/+ control scales the whole chat box (input + model/effort pickers), not just the messages.

**v0.5.1** — fixed a JS temporal-dead-zone crash that could halt the page's scripts on load (feed stuck "Connecting", agent/steering dead while the server was fine); idle status shows the *selected* driver (not whoever last ran); the last reply no longer duplicates on refresh.

**v0.5.0** — runtime documented + generalized: drivers are now generic `claude` (Claude Code) + `gpt` (codex), both BYO-subscription / no metered key; config via env; added `.env.example` + a Quickstart. (Hands-off computer-use lands at v1.0.0; on 0.5.x until then.)

**v0.4.1** — vendored a cross-platform Chrome harness (`browse/chrome-attach.sh` launches/attaches a debug Chrome with a separate automation profile on macOS / Linux / Windows / WSL; `browse/playwright-mcp.sh` wires the Playwright MCP to it). Code paths now resolve `browse/` relative to the package.

**v0.4.0** — standalone app: `app.py` + a minimal base template + `requirements.txt`, so it runs on its own (`python app.py`) instead of needing a host Flask app to mount the blueprint.

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

</details>
