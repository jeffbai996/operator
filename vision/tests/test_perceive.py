"""Tests for perceive.py — slice 1 (template + colour target finding) and
slice 2 (OCR, read_text).

Synthetic images only (PIL) — no browser, no real screenshots. Run with:
  pytest test_perceive.py -q
"""
import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

import perceive as P


def _has_tesseract() -> bool:
    # the binding importing is NOT enough — the system binary must answer too
    # (pip-installed pytesseract without tesseract-ocr fails at call time)
    try:
        import pytesseract
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


# Real-OCR tests need the pytesseract binding installed; skip cleanly without it.
needs_ocr = pytest.mark.skipif(not _has_tesseract(),
                               reason="pytesseract not installed")


# ── helpers ──────────────────────────────────────────────────────────────────
def blank(w=640, h=400, color=(20, 22, 28)):
    return Image.new("RGB", (w, h), color)


def draw_text(img, x, y, s, color=(0, 0, 0)):
    d = ImageDraw.Draw(img)
    d.text((x, y), s, fill=color, font=ImageFont.load_default(size=28))
    return img


def paste_square(img, x, y, size=20, color=(220, 40, 40)):
    sq = Image.new("RGB", (size, size), color)
    img.paste(sq, (x, y))
    return img


def draw_disc(img, cx, cy, r=14, color=(40, 200, 70)):
    a = np.asarray(img).copy()
    yy, xx = np.ogrid[:a.shape[0], :a.shape[1]]
    m = (xx - cx) ** 2 + (yy - cy) ** 2 <= r * r
    a[m] = color
    return Image.fromarray(a)


# ── find_template ─────────────────────────────────────────────────────────────
def test_finds_single_template_at_known_xy():
    img = paste_square(blank(), 300, 200, size=20, color=(220, 40, 40))
    tmpl = Image.new("RGB", (20, 20), (220, 40, 40))
    hits = P.find_template(img, tmpl, threshold=0.7)
    assert len(hits) >= 1
    best = hits[0]
    # center of a 20x20 square pasted at (300,200) is (310,210)
    assert abs(best.x - 310) <= 2 and abs(best.y - 210) <= 2
    assert best.score >= 0.7


def test_finds_multiple_instances():
    img = blank()
    for (x, y) in [(100, 80), (300, 200), (500, 320)]:
        paste_square(img, x, y, size=20, color=(220, 40, 40))
    tmpl = Image.new("RGB", (20, 20), (220, 40, 40))
    hits = P.find_template(img, tmpl, threshold=0.7)
    centers = sorted((h.x, h.y) for h in hits)
    assert len(hits) == 3
    assert centers == [(110, 90), (310, 210), (510, 330)]


def test_no_match_below_threshold():
    img = blank()                          # template absent
    tmpl = Image.new("RGB", (20, 20), (220, 40, 40))
    hits = P.find_template(img, tmpl, threshold=0.85)
    assert hits == []


def test_template_larger_than_frame_returns_empty():
    img = blank(40, 40)
    tmpl = Image.new("RGB", (80, 80), (1, 2, 3))
    assert P.find_template(img, tmpl) == []


# ── find_color_blobs ──────────────────────────────────────────────────────────
def test_finds_green_blob_centroid():
    img = draw_disc(blank(), 220, 160, r=16, color=(40, 200, 70))
    blobs = P.find_color_blobs(img, lo_hsv=(90, 0.3, 0.3), hi_hsv=(160, 1.0, 1.0),
                               min_area=40)
    assert len(blobs) >= 1
    b = blobs[0]
    assert abs(b.x - 220) <= 3 and abs(b.y - 160) <= 3


def test_ignores_tiny_noise():
    img = blank()
    # a couple of single green pixels — below min_area
    a = np.asarray(img).copy()
    a[10, 10] = (40, 200, 70); a[300, 400] = (40, 200, 70)
    img = Image.fromarray(a)
    blobs = P.find_color_blobs(img, lo_hsv=(90, 0.3, 0.3), hi_hsv=(160, 1.0, 1.0),
                               min_area=40)
    assert blobs == []


def test_separates_two_blobs():
    img = draw_disc(blank(), 150, 150, r=14, color=(40, 200, 70))
    img = draw_disc(img, 480, 250, r=14, color=(40, 200, 70))
    blobs = P.find_color_blobs(img, lo_hsv=(90, 0.3, 0.3), hi_hsv=(160, 1.0, 1.0),
                               min_area=40)
    assert len(blobs) == 2
    centers = sorted((round(b.x / 10) * 10, round(b.y / 10) * 10) for b in blobs)
    assert centers == [(150, 150), (480, 250)]


def test_red_hue_wraparound():
    img = draw_disc(blank(), 200, 200, r=14, color=(230, 30, 30))   # red ~0deg
    blobs = P.find_color_blobs(img, lo_hsv=(350, 0.4, 0.3), hi_hsv=(10, 1.0, 1.0),
                               min_area=40)   # wrap-around band
    assert len(blobs) >= 1
    assert abs(blobs[0].x - 200) <= 3 and abs(blobs[0].y - 200) <= 3


# ── build_world_state ─────────────────────────────────────────────────────────
def test_world_state_labels_and_sorts():
    img = blank()
    paste_square(img, 300, 200, size=20, color=(220, 40, 40))
    img = draw_disc(img, 480, 300, r=16, color=(40, 200, 70))
    tmpl = Image.new("RGB", (20, 20), (220, 40, 40))
    spec = [
        {"label": "button", "kind": "template", "template": tmpl, "threshold": 0.7},
        {"label": "tree", "kind": "color", "lo": (90, 0.3, 0.3), "hi": (160, 1.0, 1.0), "min_area": 40},
    ]
    ws = P.build_world_state(img, spec)
    labels = {t["label"] for t in ws.targets}
    assert "button" in labels and "tree" in labels
    assert ws.w == 640 and ws.h == 400
    # sorted by score desc
    scores = [t["score"] for t in ws.targets]
    assert scores == sorted(scores, reverse=True)
    # every target has integer x,y within frame
    for t in ws.targets:
        assert 0 <= t["x"] < ws.w and 0 <= t["y"] < ws.h
        assert isinstance(t["x"], int) and isinstance(t["y"], int)


def test_empty_frame_empty_targets():
    ws = P.build_world_state(blank(), [
        {"label": "x", "kind": "color", "lo": (90, 0.5, 0.5), "hi": (160, 1.0, 1.0), "min_area": 40},
    ])
    assert ws.targets == []
    assert ws.w == 640 and ws.h == 400


def test_grayscale_and_rgba_inputs_normalize():
    # grayscale ndarray
    g = np.full((50, 60), 128, dtype=np.uint8)
    ws = P.build_world_state(g, [])
    assert ws.w == 60 and ws.h == 50
    # RGBA ndarray
    rgba = np.zeros((30, 40, 4), dtype=np.uint8); rgba[..., 3] = 255
    ws2 = P.build_world_state(rgba, [])
    assert ws2.w == 40 and ws2.h == 30


# ── lazy pytesseract import (module must load without OCR installed) ─────────
def test_module_imports_and_numpy_funcs_work_without_pytesseract():
    # The whole point: perceive.py must import and its pure-numpy finders must
    # run even when pytesseract is NOT installed. If pytesseract is absent in
    # this env, the plain import + call below already proves it. If it IS
    # present, hide it so the guarantee is still exercised.
    import builtins
    real_import = builtins.__import__

    def no_tess(name, *a, **k):
        if name == "pytesseract" or name.startswith("pytesseract."):
            raise ImportError("pytesseract hidden for test")
        return real_import(name, *a, **k)

    import sys
    saved = sys.modules.pop("pytesseract", None)
    builtins.__import__ = no_tess
    try:
        import importlib
        mod = importlib.reload(P)              # re-import with pytesseract blocked
        # pure-numpy target finder still works
        img = paste_square(blank(), 300, 200, size=20, color=(220, 40, 40))
        tmpl = Image.new("RGB", (20, 20), (220, 40, 40))
        hits = mod.find_template(img, tmpl, threshold=0.7)
        assert len(hits) >= 1
        # ...and read_text raises a CLEAR error only when OCR is actually invoked
        with pytest.raises(RuntimeError, match="pytesseract"):
            mod.read_text(blank(50, 20))
    finally:
        builtins.__import__ = real_import
        if saved is not None:
            sys.modules["pytesseract"] = saved
        importlib.reload(P)                    # restore module for other tests


# ── read_text (slice 2: OCR) ────────────────────────────────────────────────
@needs_ocr
def test_read_text_finds_known_word():
    img = draw_text(blank(200, 60, color=(255, 255, 255)), 10, 10, "XP: 1234")
    hits = P.read_text(img)
    joined = " ".join(h.text for h in hits)
    assert "XP" in joined and "1234" in joined
    for h in hits:
        assert h.conf > 50


@needs_ocr
def test_read_text_region_crop_offsets_coords_to_full_frame():
    img = blank(400, 120, color=(255, 255, 255))
    draw_text(img, 10, 10, "ignored")
    draw_text(img, 220, 60, "target")
    # crop to ONLY the second word's area
    hits = P.read_text(img, region=(200, 40, 180, 60))
    texts = [h.text for h in hits]
    assert texts == ["target"]
    hit = hits[0]
    # coords must be in FULL-FRAME space (region origin added back), not
    # region-local — i.e. roughly where "target" actually sits in `img`.
    assert hit.x > 200 and hit.y > 40


def test_read_text_min_conf_filters_low_confidence(monkeypatch):
    # Deterministic: mock pytesseract.image_to_data directly rather than
    # trying to coax a real low-confidence OCR result out of a synthetic image.
    # pytesseract is imported lazily (inside read_text), so patch the module
    # loader to hand back a fake tesseract instead of the real one.
    class FakeTess:
        class Output:
            DICT = "dict"
        @staticmethod
        def image_to_data(*a, **k):
            return {
                "text": ["good", "bad", ""],
                "conf": [95, 10, -1],
                "left": [0, 50, 0], "top": [0, 0, 0],
                "width": [10, 10, 0], "height": [10, 10, 0],
            }
    monkeypatch.setattr(P, "_get_pytesseract", lambda: FakeTess)
    hits = P.read_text(blank(100, 20), min_conf=40)
    assert [h.text for h in hits] == ["good"]


@needs_ocr
def test_read_text_blank_image_returns_no_hits():
    assert P.read_text(blank(200, 60)) == []


@needs_ocr
def test_build_world_state_text_kind():
    img = draw_text(blank(200, 60, color=(255, 255, 255)), 10, 10, "XP: 1234")
    ws = P.build_world_state(img, [
        {"label": "xp_counter", "kind": "text", "min_conf": 40},
    ])
    assert ws.targets == []   # text doesn't populate targets
    joined = " ".join(t["text"] for t in ws.text)
    assert "1234" in joined
    assert all(t["label"] == "xp_counter" for t in ws.text)
    assert ws.as_dict()["text"] == ws.text
