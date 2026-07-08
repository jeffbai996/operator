"""perceive.py — local, zero-token visual perception for the Operator agent.

Turns a screenshot into a WorldState: a labeled list of on-screen targets
({label, x, y, score}) the planner can pick from by index, instead of the model
eyeballing raw pixels. This is the canvas-game unlock (RuneScape/Lichess/GeoGuessr
have no DOM to ground on — only pixels).

Slice 1: target finding (template + colour). Slice 2: OCR (read_text).
Pure numpy + Pillow — no OpenCV, no browser, no network — except read_text(),
which shells out to the system tesseract binary via pytesseract (still no
network). pytesseract is imported LAZILY inside read_text(), so the module and
all its numpy target-finders load and run on a box with no OCR installed. All
functions are input -> output so they're trivially testable with synthetic
images.

Coordinate convention: (x, y) in pixels from the top-left of the frame, where x is
the column and y is the row — the same convention the operator's coordinate-mouse
tools (browser_mouse_click_xy) expect.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from PIL import Image


# pytesseract is imported LAZILY (see _get_pytesseract) so that importing this
# module — and using every pure-numpy finder in it (find_template,
# find_color_blobs, build_world_state without a text spec) — works on a box that
# has no OCR installed. Only read_text() actually needs the tesseract binding;
# a hard top-level import there would take the whole module down with it.
def _get_pytesseract():
    """Import pytesseract on first OCR use; raise a clear, actionable error if
    the binding (or the system tesseract binary behind it) isn't installed."""
    try:
        import pytesseract
    except ImportError as e:                       # noqa: BLE001 — re-raise clean
        raise RuntimeError(
            "read_text() needs OCR, but pytesseract is not installed. "
            "Install it (`pip install pytesseract`) plus the system tesseract "
            "binary. The numpy target-finders (find_template / find_color_blobs) "
            "work without it."
        ) from e
    return pytesseract


# ── frame normalization ─────────────────────────────────────────────────────
def _to_rgb_array(frame) -> np.ndarray:
    """Accept a PIL.Image, a numpy array (H,W,3 / H,W,4 / H,W grayscale), or raw
    bytes-like that PIL can open, and return a contiguous uint8 RGB array (H,W,3)."""
    if isinstance(frame, Image.Image):
        return np.asarray(frame.convert("RGB"), dtype=np.uint8)
    arr = np.asarray(frame)
    if arr.ndim == 2:                                   # grayscale -> RGB
        arr = np.stack([arr] * 3, axis=-1)
    elif arr.ndim == 3 and arr.shape[2] == 4:           # RGBA -> RGB
        arr = arr[:, :, :3]
    elif arr.ndim == 3 and arr.shape[2] == 3:
        pass
    else:
        raise ValueError(f"unsupported frame shape {arr.shape}")
    return np.ascontiguousarray(arr.astype(np.uint8))


def _rgb_to_hsv(rgb: np.ndarray) -> np.ndarray:
    """Vectorized RGB(0-255) -> HSV with H in [0,360), S,V in [0,1]. (H,W,3) float."""
    r, g, b = (rgb[..., 0] / 255.0, rgb[..., 1] / 255.0, rgb[..., 2] / 255.0)
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    d = mx - mn
    h = np.zeros_like(mx)
    # avoid div-by-zero where d==0 (gray); those hues stay 0
    nz = d > 1e-9
    # which channel is max
    rmask = nz & (mx == r)
    gmask = nz & (mx == g) & ~rmask
    bmask = nz & (mx == b) & ~rmask & ~gmask
    with np.errstate(invalid="ignore", divide="ignore"):
        h[rmask] = (((g - b) / d)[rmask] % 6)
        h[gmask] = (((b - r) / d)[gmask] + 2)
        h[bmask] = (((r - g) / d)[bmask] + 4)
    h = (h * 60.0) % 360.0
    s = np.where(mx > 1e-9, d / np.maximum(mx, 1e-9), 0.0)
    v = mx
    return np.stack([h, s, v], axis=-1)


# ── results ─────────────────────────────────────────────────────────────────
@dataclass
class Match:
    x: int
    y: int
    score: float
    label: str = ""

    def as_dict(self) -> dict:
        return {"label": self.label, "x": int(self.x), "y": int(self.y),
                "score": round(float(self.score), 4)}


@dataclass
class TextHit:
    x: int
    y: int
    w: int
    h: int
    text: str
    conf: float
    label: str = ""

    def as_dict(self) -> dict:
        return {"label": self.label, "x": int(self.x), "y": int(self.y),
                "w": int(self.w), "h": int(self.h), "text": self.text,
                "conf": round(float(self.conf), 2)}


@dataclass
class WorldState:
    targets: list = field(default_factory=list)   # list[dict] {label,x,y,score}
    text: list = field(default_factory=list)       # list[dict] TextHit.as_dict()
    w: int = 0
    h: int = 0

    def as_dict(self) -> dict:
        return {"targets": self.targets, "text": self.text, "w": self.w, "h": self.h}


# ── template matching ───────────────────────────────────────────────────────
def find_template(frame, template, threshold: float = 0.85,
                  max_results: int = 25, label: str = "") -> list:
    """Find `template` inside `frame` by sliding it and scoring each position with
    a normalized similarity = 1 - mean(|patch - template|)/255, in [0,1].

    Chosen over zero-mean cross-correlation because that one CANNOT match a
    solid-colour patch (a flat template zero-means to all-zeros) — and game UI
    targets (buttons, board squares, item icons) are often near-solid. Abs-diff
    handles both flat and textured templates. Returns CENTER-coord Matches sorted
    by score desc, non-max-suppressed. Pure numpy; fine for modest template counts."""
    F = _to_rgb_array(frame).astype(np.float32)
    T = _to_rgb_array(template).astype(np.float32)
    fh, fw = F.shape[:2]
    th, tw = T.shape[:2]
    if th > fh or tw > fw:
        return []
    scores = np.full((fh - th + 1, fw - tw + 1), -1.0, dtype=np.float32)
    # slide (bounded loop over the top-left grid; vectorized over the patch)
    for y in range(scores.shape[0]):
        for x in range(scores.shape[1]):
            patch = F[y:y + th, x:x + tw]
            mad = np.abs(patch - T).mean()           # mean abs diff, 0..255
            scores[y, x] = 1.0 - (mad / 255.0)       # 1.0 = perfect match
    hits = []
    ys, xs = np.where(scores >= threshold)
    cand = sorted(((float(scores[y, x]), int(x), int(y)) for y, x in zip(ys, xs)),
                  reverse=True)
    # non-max suppression: collapse hits whose centers fall within ~a full template
    # of a kept (higher-scoring) one — so the cluster of near-miss offsets around one
    # instance becomes a single detection.
    kept = []
    min_dx, min_dy = float(tw), float(th)
    for sc, x, y in cand:
        cx, cy = x + tw // 2, y + th // 2
        if any(abs(cx - kx) < min_dx and abs(cy - ky) < min_dy for kx, ky, _ in kept):
            continue
        kept.append((cx, cy, sc))
        if len(kept) >= max_results:
            break
    for cx, cy, sc in kept:
        hits.append(Match(x=cx, y=cy, score=sc, label=label))
    return hits


# ── colour blob finding ─────────────────────────────────────────────────────
def find_color_blobs(frame, lo_hsv, hi_hsv, min_area: int = 60,
                     max_results: int = 25, label: str = "") -> list:
    """Find connected regions whose HSV falls in [lo_hsv, hi_hsv]; return centroid
    Matches (score = normalized area). lo/hi are (H[0-360], S[0-1], V[0-1]).
    Hue range wraps if lo_h > hi_h (e.g. red spanning 350->10)."""
    rgb = _to_rgb_array(frame)
    hsv = _rgb_to_hsv(rgb)
    H, S, V = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    lh, ls, lv = lo_hsv
    hh, hs, hv = hi_hsv
    if lh <= hh:
        hue_mask = (H >= lh) & (H <= hh)
    else:                                   # wrap-around hue band (e.g. red)
        hue_mask = (H >= lh) | (H <= hh)
    mask = hue_mask & (S >= ls) & (S <= hs) & (V >= lv) & (V <= hv)
    blobs = _connected_components(mask, min_area)
    blobs.sort(key=lambda b: b[2], reverse=True)   # by area desc
    total = float(mask.shape[0] * mask.shape[1]) or 1.0
    out = []
    for cx, cy, area in blobs[:max_results]:
        out.append(Match(x=int(cx), y=int(cy), score=area / total, label=label))
    return out


def _connected_components(mask: np.ndarray, min_area: int) -> list:
    """4-connected labeling via iterative flood fill (stack). Returns
    [(centroid_x, centroid_y, area), …] for components with area >= min_area.
    No scipy dependency — fine for the handful of blobs a game frame has."""
    h, w = mask.shape
    seen = np.zeros((h, w), dtype=bool)
    comps = []
    m = mask
    ys, xs = np.where(m)
    for sy, sx in zip(ys, xs):
        if seen[sy, sx]:
            continue
        # iterative flood fill
        stack = [(sy, sx)]
        seen[sy, sx] = True
        sumx = sumy = area = 0
        while stack:
            y, x = stack.pop()
            sumx += x; sumy += y; area += 1
            if y > 0 and m[y - 1, x] and not seen[y - 1, x]:
                seen[y - 1, x] = True; stack.append((y - 1, x))
            if y < h - 1 and m[y + 1, x] and not seen[y + 1, x]:
                seen[y + 1, x] = True; stack.append((y + 1, x))
            if x > 0 and m[y, x - 1] and not seen[y, x - 1]:
                seen[y, x - 1] = True; stack.append((y, x - 1))
            if x < w - 1 and m[y, x + 1] and not seen[y, x + 1]:
                seen[y, x + 1] = True; stack.append((y, x + 1))
        if area >= min_area:
            comps.append((sumx / area, sumy / area, area))
    return comps


# ── OCR (slice 2) ────────────────────────────────────────────────────────────
def read_text(frame, region=None, min_conf: float = 40.0, label: str = "") -> list:
    """OCR `frame` (optionally cropped to `region` = (x, y, w, h)) via the system
    tesseract binary (pytesseract). One TextHit per recognized word, in FULL-FRAME
    coordinates — `region`'s origin is added back so callers never have to think
    in crop-local space. Hits below `min_conf` (tesseract's 0-100 word confidence)
    are dropped; empty/whitespace-only words are always dropped.

    Cropping to a known region (chat box, XP counter, tooltip) rather than OCR'ing
    the whole frame is both faster and more accurate — less unrelated UI for
    tesseract to misread."""
    pytesseract = _get_pytesseract()          # lazy: only OCR needs this
    rgb = _to_rgb_array(frame)
    ox, oy = 0, 0
    if region is not None:
        x, y, w, h = region
        rgb = rgb[y:y + h, x:x + w]
        ox, oy = x, y
    try:
        data = pytesseract.image_to_data(Image.fromarray(rgb),
                                         output_type=pytesseract.Output.DICT)
    except OSError as e:
        # pytesseract installed but the SYSTEM tesseract binary missing raises
        # TesseractNotFoundError (an OSError) at call time — normalize it to the
        # same RuntimeError the missing-binding path raises, so every caller's
        # OCR-unavailable degrade path works on both failure shapes.
        raise RuntimeError(
            "read_text() needs OCR, but the tesseract binary is not installed "
            "(sudo apt-get install tesseract-ocr). The numpy target-finders "
            "work without it.") from e
    hits = []
    for i, raw in enumerate(data.get("text", [])):
        txt = (raw or "").strip()
        if not txt:
            continue
        conf = float(data["conf"][i])
        if conf < min_conf:
            continue
        hits.append(TextHit(
            x=ox + int(data["left"][i]), y=oy + int(data["top"][i]),
            w=int(data["width"][i]), h=int(data["height"][i]),
            text=txt, conf=conf, label=label))
    return hits


# ── world state assembly ────────────────────────────────────────────────────
def build_world_state(frame, spec) -> WorldState:
    """Run a list of finder specs against `frame` and return a labeled WorldState.

    spec: list of dicts, each either
      {"label": str, "kind": "template", "template": <img>, "threshold": float}
      {"label": str, "kind": "color", "lo": (h,s,v), "hi": (h,s,v), "min_area": int}
      {"label": str, "kind": "text", "region": (x,y,w,h) | None, "min_conf": float}
    Targets are merged + sorted by score desc. Text hits are collected separately
    (OCR confidence isn't comparable to a template/color match score)."""
    rgb = _to_rgb_array(frame)
    h, w = rgb.shape[:2]
    targets = []
    text = []
    for s in (spec or []):
        kind = s.get("kind")
        label = s.get("label", "")
        if kind == "template":
            ms = find_template(rgb, s["template"],
                               threshold=s.get("threshold", 0.85), label=label)
            targets.extend(m.as_dict() for m in ms)
        elif kind == "color":
            ms = find_color_blobs(rgb, s["lo"], s["hi"],
                                  min_area=s.get("min_area", 60), label=label)
            targets.extend(m.as_dict() for m in ms)
        elif kind == "text":
            hs = read_text(rgb, region=s.get("region"),
                           min_conf=s.get("min_conf", 40.0), label=label)
            text.extend(t.as_dict() for t in hs)
    targets.sort(key=lambda t: t["score"], reverse=True)
    return WorldState(targets=targets, text=text, w=w, h=h)
