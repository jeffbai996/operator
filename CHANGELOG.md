# Changelog

Operator release history, newest first. The release notes for the current
line come first, then the version ladder from v1.2.7 back to v1.0.8, then the
earlier releases folded and grouped by theme.

## v1.2.7 — pushed browser frames, adaptive 25 fps streaming and reliable Stop

The browser feed now uses pushed compositor frames (`Page.startScreencast`) as
its primary path, falling back to polled screenshots only if a page fails to
emit frames. High stream quality runs at 25 fps, Medium holds 15 fps (`960px`
cap), and Low steps down cadence and resolution for constrained links. `Auto`
scales across High, Medium and Low from observed frame delivery speed. Pushed
frames record the accepted viewport dimensions directly so steady pages no
longer re-run layout-metric probes on every 1 Hz gate tick, and cursor handoff
after a user tap in AUTO mode glides faster (`140 ms`).

Stopping a run now settles the cockpit immediately instead of waiting on
backend teardown, keeps a dedicated Stop control beside Send while a follow-up
draft is in the composer, and prevents stopped `agy`/`gemma` runs from flushing
trailing trajectory steps into a new task card. Large session flushes skip
`keepalive` while the tab is visible so long transcripts stay within browser
request limits.

The browser settings menu is narrower and more compact, with the stream quality
control styled as an inline pill that shows the active `Auto` tier directly, and
pending permission cards use a warmer amber border and heading (`Operator needs
your permission`).

## v1.2.6 — dependable connections and named failures

Claude bots run on their long-lived sign-in token when their config folder has
one, so an expired stored login no longer fails the run. When a bot's sign-in
has lapsed, Operator refuses the dispatch and says which bot needs to sign in
again. An error result from the runtime appears as the failure reason instead
of as the reply.

The browser connection budget now ends at the first frame. Optional attachment
hooks run afterward with their own short limits, so a slow page cannot cancel a
feed that is already showing. A dead connection can no longer freeze the feed,
tab-follow probes run outside the capture loop, and held frame requests wake as
soon as a frame is published. Closed-page errors from abandoned browser calls
are no longer logged.

A run state that has not been recorded stays unresolved rather than done, in
history, Operator MCP, Codex tool status and downloads. Deleting a conversation
retires its runner and saved state. A saved task opens its start page only after
admission, and an effort the model does not offer is refused. Keys released
while queued are still pressed, and the sandbox container pauses when unused.
The model menu includes Claude Haiku 5.5 and Mythos 5.1 with measured usage,
and OLED black follows the host-app theme.

## v1.2.5 — faster startup and clearer task status

Input/output token counts share the step count’s baseline in live and completed
traces. Copied order numbers now show a visible confirmation, with a larger
touch target and an inline failure state if clipboard access is denied.

The status headline describes the task lifecycle; the smaller line keeps the
current action. Pending approvals and human handoffs remain explicit even while
the browser reconnects. Model and effort controls follow the mobile composer’s
text scale, and the viewing notice uses a neutral dark surface.

The first browser frame is captured before optional attachment setup. After
90 seconds without a viewer, capture pauses while the attachment remains warm
until 270 seconds. Startup timing milestones are available in the viewport
diagnostics; slow initial connection no longer presents as a lost signal.

## v1.2.2 — durable approvals and clearer results

Permission requests remain in the conversation after a decision, and approved
requests stay visible after reload. Result cards group repeated checks of the
same page into one source link, with check times available on hover. A verified
order number links to its observed order page when one is available; otherwise
the number can be copied without guessing a destination.

The live status card shows a short action and compact input/output token counts
when reported by the runtime. The approval transition reads “Requesting
approval”.

## v1.2.1 — the tab you watch is the tab being driven

The cockpit streams the tab the agent is actually working in. Reopening the
cockpit after an idle stop lands on the live run's own tab, then on Chrome's
real front tab, then on the newest page with content; Chrome's empty New Tab
is never promoted over a page with work in it. During a run the streamer
follows the conversation's registered tab from the tab registry instead of
re-asserting its own guess, so a same-URL workload (one SPA page for the
whole task) can no longer pin the view on the wrong tab. Every page switch
leaves a flight-recorder event, and the recorder holds 240 of them.

Tab titles use the chat face: DM Sans on the private host, DM Sans in
the public package and demo.

The polish pass under the same label: one table-tested decision picks the
streamed tab (dead-current, owned, mover, front, new-tab) and a count change
mid-run is no longer a reason to switch. The delegation MCP gains
`operate_look`, one JPEG of a delegation's own tab as image content, and runs
from the published release. Delegation chats carry `origin: mcp`, stay out of
the Chats library unless the Delegations filter is chosen, never become the
human's draft, and are pruned after a week untouched. On a brand-new cockpit
the trash button works again (a fresh delete of the implicit conversation is
idempotent). The idle-tab reaper's websocket dependency is declared, so it can
finally close a tab. The suite is green apart from stated skips, and the mirror
proves the public cut collects before it ships.

Everything since the 1.2.0 label ships under this number: Operator MCP
delegation from other bots, steering that no longer loses the run it
interrupted, durable chat delete and chat metadata, result cards anchored to
their turn, composer drafts that survive refresh, owned-tab reaping, the
1Password popup left alone, per-run isolation of sandbox transport, downloads
and Gemini MCP config, and Gemini Flash 3.8.

## v1.2.0 — jobs, results, files and steering

The private cockpit now keeps a durable job alongside each conversation:
checkpoints in the status card, expandable constraints/decisions, and result
cards with observed sources and chat files. Simple questions remain simple.
Saved recipes can specify success criteria and exact authorization limits;
otherwise Operator prepares the action and asks for approval.

Corrections use Codex's native in-flight steering. Other runtimes deliver at a
tool boundary, with a three-second interrupt/resume fallback when needed.
Stop remains separate. The paperclip opens Chat files (uploads, generated files
and completed downloads from owned Windows Chrome tabs). Operator health is
hidden behind Settings → Diagnostics and records bounded metadata, not content.

See [WORKSPACE.md](WORKSPACE.md) for storage, rollback, provider boundaries and
verification. This is a private-service release; no package or public-demo
deployment is implied. Existing chats and native thread IDs are retained.

## Version ladder

| version | what shipped |
|---|---|
| v1.2.7 | **pushed browser frames, adaptive 25 fps streaming and reliable Stop** — Chrome screencast push is the primary browser capture path with 25 fps High / 15 fps Medium / Low tiers and dynamic `Auto` scaling; Stop settles immediately and stays available while typing a follow-up draft; compact browser settings menu and warmer amber permission cards. |
| v1.2.6 | **dependable connections and named failures** — Claude bots use their long-lived sign-in token, an expired sign-in is refused by name before dispatch, the connection budget ends at the first frame, and unrecorded run states stay unresolved. |
| v1.2.5 | Trace token counts align with the step count; order-number copy controls have larger touch targets and visible success/failure feedback. |
| v1.2.2 | **durable approvals and clearer results** — completed permission cards remain in history; result sources are deduplicated and observed order links are actionable; the status card has concise actions and input/output token counts. |
| v1.2.1 | **the tab you watch is the tab being driven** — attach and follow prefer the run's registered tab, then Chrome's real front tab, then the newest page with content; busy enforcement fronts the owned tab only; every page switch is recorded; tab titles use the chat face. Carries the 1.2.0-labelled work on Operator MCP delegation, steering, chat library, results, composer drafts, tab reaping and per-run isolation. |
| v1.2.0 | **jobs, results, files and steering** — durable checkpoints and evidence-linked results, native Codex steering, chat files and owned-browser downloads, recipe authorization limits, and bounded diagnostics. See [WORKSPACE.md](WORKSPACE.md) for provider and deployment boundaries. |
| v1.1.0 | **threads that travel, one browser that does not become a knife fight** — conversations are server-authoritative, revisioned, and visible on every signed-in device. The launchpad owns a finished, responsive Chats library with search, prompt previews, bot/surface/live state, inline rename, guarded delete, and a reusable empty draft so repeated New chat taps do not leave blank debris. The modal lives in the browser top layer, so opening it cannot reflow the launchpad or knock the composer out of place. One device holds the editing lease at a time; another can watch live and deliberately take over without stale local state overwriting newer work. Concurrent conversations retain separate runners, transcripts, resumed model sessions, working directories, state files, and browser-tab leases behind the existing shared admission cap. Every agent sees only its own tab and any popup it opened even though all tabs live in the same logged-in Windows Chrome, and selecting a conversation foregrounds its tab for the human. Recovery nudges such as the loop breaker now render as calm amber notices instead of false red “Turn failed” errors. The Operator browser returns to an interactive Windows launch so its existing 1Password and AdBlock extensions load again. This release starts a fresh conversation store; run history remains intact. |
| v1.0.40 | **one clear handoff + a finished header** — the alert headline now says “Operator needs your input,” while the specific takeover instruction appears once in the body instead of being duplicated. The compact wordmark now sets its close-coupled version in self-hosted Urbanist, the yellow SANDBOX and red COMPUTER surface badges both remain explicit and optically centered, and the three-stop theme control crossfades and rotates between its sun/moon marks instead of snapping through `display:none`. Reduced-motion users still get an instant state change. |
| v1.0.39 | **alerts mean the human is needed** — routine Operator messages, actions, failures, and completions stay in the cockpit. `#alerts` receives one card only when Operator emits an explicit `TAKE_CONTROL` handoff (including a token-cap takeover), with the reason and latest frame attached. |
| v1.0.38 | **one run, one alert** — the live Discord card still edits in place while Operator works and the completion still arrives as a new post so the phone buzzes, but a successful completion now removes its retired live card. Posting happens before cleanup, so a failed completion leaves the useful original card intact instead of deleting the only receipt. |
| v1.0.37 | **correct on arrival + restart-safe runs** — a first-load/re-entry stage beacon force-reapplies CDP metrics even when the saved target dimensions already match, repairing the stale bottom letterbox a dead runner can leave without waiting for a divider drag. Host-app publishing drains Operator admission and refuses to restart through an active run; a systemd stop-boundary guard covers direct `systemctl restart` calls too, waiting for the live slot to finish while new dispatches receive an explicit deployment-in-progress refusal. |
| v1.0.36 | **conversation-scoped runners + bounded admission** — each cockpit conversation owns its runner, transcript, resumed bot sessions, steer/stop channel, state file, and working directory. Two different bots can run concurrently by default; a shared global cap and one-slot-per-bot guard prevent one conversation from starving or colliding with another. Dispatch rejection names the requested bot and the conversation holding the blocking slot, stop and telemetry target only the selected conversation, and history records the conversation id. The original single-conversation path keeps its legacy state and behavior. |
| v1.0.35 | **the transaction batch, and runs that tell you they finished** — the pieces a real purchase needs land together: the 1Password extension fills cards and IDs (load-time injection noted), the CVV request and the go-ahead to submit are asked for as ONE authorisation rather than two separate favours, and personal data is treated as a manifest instead of something to hunt for. Browsing gets more honest about the page underneath it: element refs beat stale pixel coordinates on a moving page, dialog/hover/download reachability is taught rather than discovered by failing, a collapsed viewport can no longer calibrate a click, and the feed streams the tab the agent is actually working on. The loop-breaker learns the failure it kept missing — a rolling window catches VARIED-coordinate flailing (a booking run burned ~25 calls clicking different wrong spots, resetting the exact-repeat streak every time), while a `browser_snapshot` clears the window, because re-grounding on the DOM is the recovery the nudge asks for. Restarts requested mid-turn defer to the terminal transition. New surface: **an alerts channel that carries the run**. A run posts an in-flight card the moment it starts and EDITS it as the trace grows, so the phone shows what the agent is doing right now without opening the cockpit; when the run ends, that card retires to its final state and a separate completion ping lands as a new message with the final frame attached (edits don't notify, posts do — one card and exactly one buzz per run). Change is measured by what the agent DID, not by the clock, so a silent hour costs one edit rather than one per poll. Both are off unless `OPERATOR_PING_CHANNEL` names a channel, both run off the run thread, and neither can break a run by contract. Headings stay markdown and every readout line sits in a code block, so a ping reads as an instrument panel rather than as chat. This reverses the 2026-07-01 rejection: the objection was never the ping, it was the ping landing in a channel people talk in, which a dedicated alerts channel answers. Also: the effort auto-switcher is reverted and effort stays manual, the launchpad reports real browser state instead of the idle feed's, midnight-theme code wells go to true black, **conversations** land with it — the cockpit's one-and-only chat becomes a map of them, switchable/renamable/deletable from a Chats popover, New chat lands on the welcome view, and an untitled chat takes the name of the first task run in it; and the release version stops being typed into three templates and pinned in a test — one `OP_VERSION` constant, injected into every template the blueprint renders. |
| v1.0.33 | **the letterbox, root-caused** — two long-standing viewport complaints turn out to be one mechanism failing in two places, both found in the viewport flight recorder rather than reasoned about. Re-entry: returning to a backgrounded cockpit tab fires visibilitychange, not resize, so nothing re-beaconed the stage size — and while away the remote target drifts, because a force-desktop on navigation re-applies the stored target, which before any beacon is `WIDTHx0` (auto height = the window's native height) and object-fit:contain shows that as a letterbox (recorder: `apply 1280x0`, eight minutes before the session's first beacon). Re-entry now resyncs, clearing the no-op guard first because the stage size is normally unchanged across the away period — the reason the old code swallowed the re-send. Collapse: the repair for a collapsed layout viewport required REPAIR_AFTER_MISSES *consecutive* gate misses, and a healthy read zeroed the counter — so a viewport FLIPPING between healthy and collapsed (recorder: ten `vp-walk 1024->651` in one session) never reached the threshold and the captured frame kept changing size under the viewer. The collapsed band now scores instead of counting: a collapsed read outweighs a healthy one, so a flip-flop converges on a repair while a lone navigation transient still decays to nothing and never reflows the real window. Also: the version line under the wordmark moves to DM Sans. |
| v1.0.32 | **the light-mode papercut siege + a resilient renderer** — a full contrast pass lands non-flat light mode for real: the missing --bg-4 that rendered user bubbles dark-on-dark (flat-light masked it for weeks), a darkened muted tier (disclaimer, "Follow up", version line), the chat's live verb recolored to the muted shade (and re-set in DM Sans at 600/0.78rem after a full up-and-back tuning loop), a truer input-blinker yellow, theme-following scrollbars via color-scheme, and the fullscreen page canvas finally following the theme instead of framing light mode in black. The renderer grows resilience for malformed model output: +---+ ASCII grids render as real tables even OUTSIDE code fences (Flash stutters its fences — ```text immediately closed — dumping grids into prose), empty fences render nothing, and an unterminated trailing fence renders as text instead of swallowing the message. Viewport: frames net the FULL stage width (the vertical scrollbar's ~15px shave is measured in device units and compensated in the override — the persistent left/right pillarbox), and the emulated clip grows to the page's laid-out body so body-min-width sites (google.com under profile page zoom) keep their right edge. Composer: a multi-line draft deleted in one go can't leave the grown pill behind (inline styles reset outright — iPad Safari outlives the CSS clamp), and the model-picker row is pinned to the painted input height so a 3-line draft can't spill onto it. Ops: template hot-reload makes style deploys restart-free (every restart blanked the live feed to the placeholder — the "random black screens"), and fam gets its favicon cache-buster, an "Operator" tab title, and the restored tailnet serve mapping. |
| v1.0.31 | **bots can talk in tables, and the shared viewport learns whose aspect it is** — chat markdown grows real pipe-table rendering (bordered, rounded, sideways-scrolling, host-site table chrome neutralized) and tabular code fences (ASCII +---+ art, box-drawing) render unwrapped with horizontal scroll while prose code keeps wrapping; the chat scroller tops out in an 18px fade so bubbles melt out under the status card instead of hard-clipping. Three viewport bugs die: the per-nav clear+apply pulse (force-desktop now overwrites stale metrics instead of clearing first — the host window strobe), the letterbox chin (captures now clip to the CSS layout viewport, which display-scale zoom renders 1:1 rather than canvas-filling), and viewport ownership (the cockpit finally sends a per-tab client id, so a backgrounded phone tab can no longer re-aspect the browser under the active desktop viewer). Light mode reclaims the splash-ring hover card and the wordmark hover cue. |
| v1.0.30 | **the About card, the mobile refactor, and geometric state marks** — clicking the splash status ring opens an About card: logo + wordmark + version on one baseline, rights line, and an Operator · BaiCloud brand foot, animated open/close with a backdrop that no longer clicks through to the splash. The phone cockpit gets its real refactor: the splash is a fixed full-viewport takeover above the sheet (no more painting under it), with the ring/theme/X controls pinned as one top row, the hero dead-centered until a pill tap animates it up, card titles at desktop proportions, and saved-task cards hidden on entry until summoned. The chat's finished-state ✓/✕/⏹ become geometric masked glyphs matched to the flat UI, the trail spinner's em-vs-rem drift is re-anchored so alignment survives font scaling, saved-task examples grow by six per category (every card on a unique site), and gemma/gpt runs finally board with squad context — resumed threads deliver the boot prompt exactly once. Placeholders settle at weight 525, and web pages stay in dark mode through a run instead of strobing light. |
| v1.0.29 | **canonical Operator mark: favicon + animated splash logo** — the icon stack finally carries the real logo. The PWA/tab icon was a generic cursor arrow; it becomes the Operator mark itself (the two interlocking hooks from the BaiCloud services dropdown) set in the host-app favicon's ring stack, generated reproducibly by `tools/gen_icons.py` rather than hand-authored. The same mark lands on the launchpad as a STATUS RING in the top-right control row (sized and spaced to the theme toggle and the X, not stacked over the wordmark): a green arc sweeps it while the feed connects, then closes into a full ring — the loader IS the logo assembling, not a spinner that gets swapped for one — and thereafter the ring is a health readout, going amber while connecting or hung and pulsing red when the browser backend is down, all driven off the cockpit's existing `data-state` rather than new plumbing. Hovering it plays a greet: the mark swells as it turns a full 360deg and relaxes back to rest. It settles on the first resolved feed state (not `op-ready`, which flips ~32ms after parse and would skip the animation entirely), with a min-spin floor so a fast connect can't flicker and a 4s ceiling so a dead backend can't spin forever; `prefers-reduced-motion` gets the finished mark and no spin. The chat's per-task spinner becomes the mark too: the conic-gradient arc (and the z-order bug where its track swallowed it into a bare gray ring) is replaced by the logo doing a halted spin — a fast 360deg sweep, then a hold during which the mark inhales and exhales, then a second full turn (720deg per cycle). This one wears NO outer ring (that belongs to the splash badge) and its viewBox is cropped to the hooks' own bounds so the art fills the icon instead of floating in the ring's dead margin, with the box sized in em of the verb beside it — a fixed 14px ran 1.39x the text height at the smallest chat scale and 0.84x at the largest. Each pause reads as deliberate rather than stalled. The same halt-and-breathe now runs INSIDE the status card's ring too, so the cockpit's two live indicators speak one language (the ring itself is untouched and keeps every state colour). A pivot bug is fixed with it: the hooks carried `transform-origin: 12px 12px`, but a px origin on an SVG child resolves against the viewport rather than user units — against the cropped viewBox that landed at 80% of the box instead of the glyph's centre, so the mark orbited a corner and swung out of frame for a split second every turn; the finished ✓/✕/⏹ still owns the box at busy=0. Also: the host-app favicon's white outer ring was a 7-unit band against the green's 6, rendering ~15% heavy at every real size — the radii now step by a uniform 6, asserted as a step so it can't silently drift back. |
| v1.0.28 | **DM Sans sweep, native-select overlay, chat-error hygiene, installed-app fullscreen** (folds the unreleased 1.0.27 batch) — the cockpit's text voice moves to DM Sans: chat, composer, status card, disclaimers, history, and the Jump pill (un-bolded with the new face), with the model picker's dynamic caret re-fitting on zoom, the send button bottom-pinned with even input padding, and the minimized status card centering its action emoji without clipping. Native `<select>` clicks render an in-page overlay instead of the OS popup the feed can't show. Chat-error hygiene: manual steering fires one silent action per gesture, and the network-failure catch ignored the silent flag — a disconnected browser now coalesces into a single ⚠-flagged "Action failed" line with a ×N counter instead of a message per wheel tick, and error step-lines actually render red (the muted `!important` base rule had been silently beating the err color). Also in the fold: code blocks no longer trap chat scroll (wheel + touch), finish-input carved out of the iOS 16px force-up, SVG history icon, trace tweaks, and the steer orphan fix. The 1.0.14 PWA becomes a real fullscreen app on iPhone/iPad: an operator-only `viewport-fit=cover` override takes the installed app edge-to-edge, with standalone-scoped safe-area padding clearing the status bar, home indicator, and landscape sensor housing — browser-tab layout untouched. |
| v1.0.26 | **flash-free splash boot + the agent drives the browser you're watching** — the splash ships pre-collapsed in the markup, so a refresh paints the calm wordmark-and-composer assembly directly instead of flashing the category tabs and card grid for the beat before the post-paint JS collapse landed. The wrong-browser regression is fixed at both layers: the bot-Chrome split (v1.0.20 era) had flipped the shared Playwright launcher's default to the bots' :9224 browser and the operator runtimes never pinned themselves back, so the agent browsed a Chrome the cockpit feed never shows — all three runtime adapters now pin `BROWSE_CHROME_PORT=9222` (claude via MCP-entry env, codex via per-server config overrides through its scrubbed env, agy via inherited process env), and `OPERATOR_REQUIRE_CDP=1` makes an unreachable cockpit Chrome fail the MCP loudly instead of silently browsing a headless fallback nobody can see. Underneath both: Windows interop (powershell/cmd/schtasks) fails from a Linux-filesystem cwd on this host, and every MCP launches from `~/.cache/computer-use` — so the CDP probe always reported Chrome "down" and the self-heal died before launching; all Windows-binary calls now hop to `/mnt/c` first, which un-breaks Chrome attach for every browse consumer, squad bots included. |
| v1.0.25 | **restored-session wiring + composer auto-resize** — the launchpad lifecycle separates into wiring, rendering, and visibility layers: control wiring always runs, so a device restoring a cached conversation boots a live cockpit with a working HOME/reopen path instead of a painted, inert splash (the nonempty-log guard used to return before any listeners attached, skipping the saved-tasks fetch; init failures were also swallowed by blind catches — they surface on the console now). Both composers auto-resize like a chat: the splash gains a block-context scaled pill on coarse-pointer WebKit — computed 16px painted at the compact 11.2px, immune to the flex-shrink and centered-margin defeats that froze the 1.0.23 hack, overflow-clipped at a 22px radius so text and caret stay inside, growing per painted line to a ~9-line cap before scrolling. Polish: the trash can lands on the solid splash rather than the over-feed blur, the rail chatbox and save-task dialog controls are carved out of the coarse-pointer 16px force-up so typed text matches its placeholders at the compact scale, category pills gain clearance from their scroll row's clip edge, the splash composer line sits a hair higher, the URL bar slims toward the rail head, fullscreen panel margins tighten from 10px to 6px, and the schedule picker's empty state reads "None". |
| v1.0.23 | **splash, viewport, and composer polish** — restores separate top-right theme and close controls, hides steering hints while the splash owns the screen, and replaces light-mode gray blocks with white surfaces and softer card borders. Operator now paints the splash before restoring session state, retains the original 10px fullscreen panel margin without zoom drift, and follows the visible browser stage without crossing the desktop layout floor. Launchpad controls initialize independently from model discovery, so a slow backend cannot leave cards, pills, or close inert. The launchpad uses a compact 24px click-away halo, clears category selection when collapsed, optically aligns its theme/close controls, and treats the full composer pill as a focus target without allowing empty placeholder selection. Both splash and rail composers expand and shrink through useful multiline drafts before scrolling, including compensation for iOS text scaling; the iOS URL field retains its compact browser typography. Asset cache revisions remain independent from the displayed release so long-lived tabs cannot retain stale styling. |
| v1.0.22 | **viewport recovery + launchpad polish** — browser targets retain a persistent desktop-metrics session, switched tabs reassert it, and capture self-repairs collapsed viewports instead of rendering a blank/mobile-width frame. The launchpad gets quieter crossfades, smaller composer type, stable search/refresh/add ordering, a centered SVG add glyph, no cursor flash through the splash, clearer Browse/Food/Local/Shop/Travel/Research/Media categories, and a Saved source that exists only when tasks do. Operator remains full-bleed while the shared site header keeps its normal centered gutter. The unavailable public-demo notice remains in place. |
| v1.0.21 | **true homepage state** — the fresh-session splash owns the full viewport with no chat rail, resize gutter, site header, browser chrome, or empty-cockpit escape hatch; the first submitted task reveals the complete interface in one transition. The oversized Plus Jakarta Sans wordmark keeps its prior weight while opening tracking from `-.06em` to `-.03em` for cleaner H1 forms. |
| v1.0.20 | **real-desktop input boundary + bot-Chrome lifecycle** — WSL-interoped PowerShell can capture Session 1 but Windows denies its window-station input writes (`SetCursorPos=false`); input now crosses a per-user `InteractiveToken` Task Scheduler broker, rejected actions fail loudly, and desktop-real dispatch pre-flights both capture and broker health. The bot Chrome itself launches through the same scheduler pattern: watchdog self-heals land in the interactive session instead of a session-0 void (where the window clamps to a 1024×768 virtual display and poisons later manual opens), with a single launch-flags source shared by the startup entry, the manual opener, and the self-heal. Both Operator browsers standardize on a 1500×1360 window; the demo-badge regex accepts bare version strings (the hardcoded `v` silently ate the " demo" suffix); chat input + placeholder proportioned to the model picker; saved-task card titles get a smaller face and a longer run before the ellipsis |
| v1.0.19 | **viewport geometry + browser chrome** — scrolled CDP captures follow the live viewport instead of recording the blank document region above it, the demo browser returns to a landscape frame, touch scrolling follows touchscreen convention without native stage overscroll, and the URL brow regains its full-height proportions |
| v1.0.18 | **iOS keyboard geometry** — visual-viewport tracking lifts the fixed sheet above Safari's software keyboard, removing the per-keystroke caret-pan fight that remained after the JavaScript typing optimizations |
| v1.0.17 | **runtime + mobile reliability** — first-turn watchdog birth race fixed, cold-state polling stops the signal-lost strobe, Codex MCP environment/exec-bridge compatibility restored, iPhone typing work reduced, and the mobile sheet gains a feed-fit detent |
| v1.0.16 | **the fix release closing the 1.0.x line** — demo/prod store isolation (a demo visitor's steer could reach a live production run — review-found, launch-scoped + `.demo` code backstops), Win-key hotkeys via `keybd_event` (SendKeys silently dropped the Win modifier, so "switch virtual desktop" sent Ctrl+Right into the void), sandbox latency back to browser-level (stale X lock wedged container restarts; boot script self-heals now), trash pushes the cleared session (the server used to resurrect the chat), smooth Finish-up flow, surface-chip shed ladder on narrow rails, consent pills (red ✕ / green ✓), taskbar auto-minimize |
| v1.0.15 | **run economics + trace replay** — live token meter on the working card (the ledger's burn numbers while the run burns; cache reads excluded as ever), History rows expand on click into the full role-tagged trace (lazy-fetched, toggling). Run queue deliberately not built: the single-flight runner is a safety property and 1.0.12 steering already answers "message while busy" |
| v1.0.14 | **JS extraction + PWA** — the ~3,600-line inline cockpit script becomes `static/js/operator.js` (endpoints injected via a tiny inline `window.OP_URLS` config — the only Jinja the client needs; cacheable, lintable, `node --check`-able), plus install-to-home-screen: web manifest (relative start_url/scope so it works under both mount paths), 192/512/180 icons, apple standalone meta |
| v1.0.13 | **task templates + re-run** — saved-task prompts take `{{variables}}` (filled at dispatch; unfilled runs bounce 400 with the missing names; Go loads the prompt into the composer with the first placeholder selected; scheduled×variables refused at save), History rows get ↻ run-again with the row's own bot/model/effort/surface (desktop-real still demands its consent flow), launchpad cards grow a variables chip |
| v1.0.12 | **mid-run steering** — a message sent during a live run reaches the agent instead of killing it: PostToolUse steer hook injects it right after the agent's next tool call (claude runtime, mid-loop; project-settings wiring so `--resume` survives), leftovers become one more resumed turn at the exit seam (every runtime), composer soft-steers with queued→delivered feedback, Stop (■) stays the hard redirect |
| v1.0.11 | **one shared session + flight recorder** — server-side session (chat history follows you across devices; demo stays per-visitor), SQLite run ledger written at the terminal transition + History popover (who/where/how-it-ended/token spend), desktop-real dispatch pre-flight refuses a locked Windows console, sandbox/control/vision surfaces re-verified end-to-end |
| v1.0.10 | **client harness + feed truth** — headless-Chromium page harness (pageerror watch, signal-state sampling), placeholder frames no longer count as signal (kills the Connecting↔Reconnecting flap), streamer driver stopped on every exit path + relaunch backoff (the orphaned-driver leak), emulation sweep on run end + "Fix stuck zoom", launchpad mobile collapse |
| v1.0.9 | **runner decomposed** — `operator_runtimes.py` launch adapters (exact-argv tests), `operator_agy.py` trajectory quarantine (fixture-replay parity), `_consume`/`_consume_codex` hardened against malformed stream input |
| v1.0.8 | **feed tiers + monolith relief** — `?tier=lo\|hi` adaptive frames (mobile ~35% of bytes), eager post-action grabs, `operator_trace.py` + `operator_prompts.py` extracted (byte-parity proven), shot-dirs single source, cron dom/dow OR fix |

## Before v1.0.8

### v1.0.7 — the run state machine, correctness-first

Four confirmed races in the agent runner fixed: the status poll reading the
message list while the run thread appends (intermittent 500s under load); the
stall watchdog killing a healthy run during the inter-turn gap of a completion
check; a Stop pressed in that same gap being silently swallowed; and racing
terminal flags resolved by one deterministic priority — token-cap beats stall
beats stop beats exit code — so a stopped run can never end mislabeled "done".

### v1.0.4 – v1.0.6 — reliability + the launchpad

- **Stall watchdog**: a run that stops making progress gets flagged softly in
  the UI, then auto-stopped with the reason in the trace — but a quiet agent
  that's genuinely working is never killed just for being silent.
- **Loop-breaker**: the same action repeated with no effect trips a warning
  and a corrective nudge on the next turn.
- **Completion gate**: a run that ends without visual evidence of its outcome
  (or sounding blocked) gets exactly one verify-or-replan follow-up turn.
- **Auto-look on failed clicks**: a rejected click comes back with a zoomed
  crop and OCR'd coordinates in the same tool result.
- **Self-clocking frame pump**: the client fetches the next frame only after
  the previous one decodes — latency stays bounded on any device, and the
  feed can never fall progressively behind.
- **Launchpad**: saved-task cards with search, auto-cycling pages, and
  tap-to-load examples on the idle stage.

### v1.0.1 – v1.0.3 — the sandbox becomes a real computer

The isolated desktop went from a throwaway X server to a persistent Docker
machine: full XFCE session, a real ffmpeg MJPEG feed, file transfer in and
out of the container, and a home directory that survives restarts. Driver
parity pulled forward: every supported agent runtime gets the desktop
control tools, not just one.

### v1.0.0 — GA: perception + macros

The agent can *see* and *grind*: `perceive` finds targets by template,
colour, and OCR against per-game maps with grid/crop grounding, and
`game_macro` runs a planner/controller split — the model plans, a local
controller executes and verifies each step by pixels, zero model calls
mid-macro. Proven end-to-end on a live canvas game.

### v0.9.0 — hardening

Coordinate contracts documented in the tool descriptions (frame space vs
screenshot space), launch errors surfaced instead of silently killing the
run thread, service-environment fixes.

### v0.8.0 — the surface axis

One cockpit, three screens: the browser, an isolated sandbox desktop, and
the real desktop. Surface picker in the header, server-side gating (the real
desktop demands explicit confirmation, every session, no shortcuts), and a
panic STOP overlaid on the feed.

### v0.7.x — cockpit stability, saved tasks, run governor

The foundation release line: a stable live feed, re-runnable saved tasks,
and hard token caps so a runaway run stops itself instead of burning the
subscription.
