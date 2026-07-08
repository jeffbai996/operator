"""overlay.py — grounding aids for direct mode: a coordinate grid the model can
read pixel positions off, exact crops for precise aiming, and target markers for
the annotated perceive output. Pure Pillow, input -> new image, no side effects.
"""
from __future__ import annotations

from PIL import Image, ImageDraw

_GRID_COLOR = (255, 80, 80)
_LABEL_COLOR = (255, 200, 80)
_MARK_COLOR = (80, 255, 120)


def _to_image(frame) -> Image.Image:
    if isinstance(frame, Image.Image):
        return frame.convert("RGB")
    return Image.fromarray(frame).convert("RGB")


def draw_grid(frame, step: int = 100, labels: bool = True) -> Image.Image:
    """A copy of `frame` with a calibration grid every `step` px and (optionally)
    "x,y" labels just inside each interior intersection — so the model reasons in
    a labeled coordinate space instead of estimating pixel positions."""
    img = _to_image(frame).copy()
    d = ImageDraw.Draw(img)
    w, h = img.size
    for x in range(step, w, step):
        d.line([(x, 0), (x, h)], fill=_GRID_COLOR, width=1)
    for y in range(step, h, step):
        d.line([(0, y), (w, y)], fill=_GRID_COLOR, width=1)
    if labels:
        for x in range(step, w, step):
            for y in range(step, h, step):
                d.text((x + 3, y + 2), f"{x},{y}", fill=_LABEL_COLOR)
    return img


def crop_region(frame, region) -> Image.Image:
    """Full-resolution crop of (x, y, w, h), clamped to the frame bounds."""
    img = _to_image(frame)
    x, y, w, h = (int(v) for v in region)
    fw, fh = img.size
    x, y = max(0, x), max(0, y)
    return img.crop((x, y, min(x + w, fw), min(y + h, fh)))


def annotate_targets(frame, targets: list) -> Image.Image:
    """A copy of `frame` with each WorldState target marked (crosshair circle +
    label) — the visual companion to the JSON target list."""
    img = _to_image(frame).copy()
    d = ImageDraw.Draw(img)
    for t in targets or []:
        x, y = int(t.get("x", 0)), int(t.get("y", 0))
        r = 6
        d.ellipse([(x - r, y - r), (x + r, y + r)], outline=_MARK_COLOR, width=2)
        d.line([(x - r - 3, y), (x + r + 3, y)], fill=_MARK_COLOR, width=1)
        d.line([(x, y - r - 3), (x, y + r + 3)], fill=_MARK_COLOR, width=1)
        label = t.get("label", "")
        if label:
            d.text((x + r + 4, y - r - 2), label, fill=_MARK_COLOR)
    return img
