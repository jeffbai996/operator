# operator/vision — perception pass (Stage 1)

Local, zero-token visual grounding for the Operator agent. Turns a screenshot into
a **WorldState**: a labeled list of on-screen targets (`{label, x, y, score}`) plus
OCR'd text (`{label, x, y, w, h, text, conf}`) the planner reads instead of
eyeballing raw pixels. The canvas-game unlock (Lichess/GeoGuessr/RuneScape have no
DOM — only pixels).

**Slice 1:** target finding — `find_template` (abs-diff slide match) +
`find_color_blobs` (HSV segment + connected components) -> `build_world_state`.
Pure numpy + Pillow. No browser, no OpenCV, no network.

**Slice 2 (this):** OCR — `read_text` (pytesseract over the whole frame or a
cropped region — chat box, item tooltips, XP counters; coords always returned in
full-frame space). Wired into `build_world_state` via `{"kind": "text", ...}`
specs, populating `WorldState.text` alongside `.targets`. Needs the system
`tesseract-ocr` binary (`apt install tesseract-ocr`) plus `pytesseract`.

**Not yet:** the `game_macro` MCP tool that calls this (slice 3). This module is
standalone and NOT wired into the live agent loop yet.

Run tests:  `cd vision && ./run_tests.sh`  (uses this module's own `venv/` — see
`requirements.txt`; needs the system `tesseract-ocr` package installed too)
