"""Tests for overlay.py — grounding aids for direct mode: coordinate grid,
crop-to-region, and target annotation. Synthetic images only."""
import numpy as np
from PIL import Image

import overlay as O


def blank(w=400, h=300, color=(20, 22, 28)):
    return Image.new("RGB", (w, h), color)


# ── draw_grid ────────────────────────────────────────────────────────────────
def test_draw_grid_paints_lines_at_step():
    img = blank()
    out = np.asarray(O.draw_grid(img, step=100))
    base = np.asarray(img)
    # grid lines land exactly on multiples of the step
    assert (out[:, 100] != base[:, 100]).any()      # vertical line at x=100
    assert (out[150] != base[150]).any()            # horizontal line at y=100? no—150
    assert (out[100, :] != base[100, :]).any()      # horizontal line at y=100
    # a pixel well away from any line or label is untouched
    assert (out[150, 155] == base[150, 155]).all()


def test_draw_grid_returns_new_image_and_preserves_size():
    img = blank(640, 480)
    out = O.draw_grid(img, step=100)
    assert out.size == (640, 480)
    assert out is not img


def test_draw_grid_labels_near_line_intersections():
    img = blank()
    out = np.asarray(O.draw_grid(img, step=100, labels=True))
    base = np.asarray(img)
    # label text is drawn just inside the (100,100) intersection → some pixels
    # in that neighborhood (excluding the lines themselves) changed
    region_out = out[102:118, 102:140]
    region_base = base[102:118, 102:140]
    assert (region_out != region_base).any()


# ── crop_region ──────────────────────────────────────────────────────────────
def test_crop_region_exact():
    img = blank()
    a = np.asarray(img).copy()
    a[50:60, 80:100] = (200, 10, 10)
    img = Image.fromarray(a)
    crop = O.crop_region(img, (80, 50, 20, 10))
    assert crop.size == (20, 10)
    assert (np.asarray(crop) == (200, 10, 10)).all()


def test_crop_region_clamps_to_frame():
    img = blank(100, 100)
    crop = O.crop_region(img, (90, 90, 50, 50))   # overhangs → clamped
    assert crop.size == (10, 10)


# ── annotate_targets ─────────────────────────────────────────────────────────
def test_annotate_targets_draws_markers():
    img = blank()
    targets = [{"label": "tree", "x": 200, "y": 150, "score": 0.97}]
    out = np.asarray(O.annotate_targets(img, targets))
    base = np.asarray(img)
    # a marker is drawn around the target center
    assert (out[145:156, 195:206] != base[145:156, 195:206]).any()
    # far corner untouched
    assert (out[:20, :20] == base[:20, :20]).all()
