# control/ — the fast hands (planner/controller split)

The operator's real-time layer. The LLM plans; this executes at machine speed.

## Why

An LLM tool-call round-trip is ~2–5s. Canvas games (and any repetitive UI
grind) need sub-second reactions across dozens of steps. No surface change
fixes that — the fix is architectural: the model emits a **macro** once, a
local controller executes it with **local perception** between steps, and the
model only hears back on completion or surprise. One LLM call in, many fast
actions out.

## Pieces

| file | job |
|---|---|
| `surfaces.py` | one capture+inject interface over three surfaces: `browser` (raw CDP on the operator Chrome), `desktop-sandbox` (Xvfb via computer-use), `desktop-real` (Windows via PowerShell bridge, gated) |
| `macro.py` | the macro controller: validates + executes ops (`click_target`, `click_xy`, `drag_xy`, `type`, `key`, `scroll`, `wait_until`, `repeat`, `assert`, `yield_to_planner`), evaluates conditions locally (target counts, OCR text, pixel change), bails back to the planner on anything unexpected |
| `mcp_server.py` | stdio MCP exposing `perceive`, `game_macro`, and (desktop surfaces) `computer` to the headless agent |
| `events.py` | writes tool/op events into the cockpit's live trace log |
| `operator-mcp.sh` | launcher — resolves the venv, puts `vision/` + `control/` on `PYTHONPATH`, reads `OPERATOR_SURFACE` |

Perception lives in `../vision/` (`perceive.py` finders, `maps.py` per-game
region/sprite maps, `overlay.py` grid/crop/annotate grounding aids).

## Coordinate contract

All surface coordinates are **frame-pixel space** — the pixels perception saw.
`BrowserSurface` records the device-px→CSS-px scale at each capture and
converts on inject (CDP screenshots are device px, CDP input is CSS px; on any
DPR>1 window unconverted clicks land down-right of the target).
`win_backend` does the same image→physical scaling on the real desktop.

## Safety

- Every inject checks the shared STOP file first (`operator-stop.json`,
  armed by the cockpit STOP button and by `runner.stop()`); a stop newer than
  the surface's start raises `SurfaceStopped` — mid-macro, mid-drag, anywhere.
- `desktop-real` refuses to construct without `OPERATOR_REAL_OK=1`, which only
  the cockpit's per-session confirm flow sets. Never a default, never in demo.
- The controller carries step + wall-clock budgets; `repeat` has a hard `max`
  and hitting it is a bail (`repeat_max`), not silent completion.

## Proven

2026-07-08, Lichess analysis board (safe, no-ToS target): board geometry
calibrated from color-blob perception alone, `game_macro` played a move by
click-click with a destination-square `pixel_change` verify — 3.2s, zero LLM
calls mid-macro. Same flow exercised end-to-end by a live cockpit agent run
(`perceive` → `game_macro` → verified move → report).

## Tests

`bash run_tests.sh` — controller + MCP handlers against fake surfaces
(no browser, no display, no model). Browser/desktop integration is exercised
by the proving scripts and the cockpit itself.
