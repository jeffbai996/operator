"""Tests for maps.py — per-game map files (named UI regions + sprite templates
+ colour specs) and the map → build_world_state spec compiler.

Synthetic maps written to tmp_path only — no real game assets. Run via
run_tests.sh (vision venv).
"""
import json

import pytest
from PIL import Image

import maps as M


BASE = {
    "game": "testgame",
    "viewport": [1280, 800],
    "regions": {"inventory": [1000, 600, 200, 150], "board": [200, 80, 640, 640]},
    "colors": {
        "tree": {"lo": [90, 0.3, 0.2], "hi": [150, 1.0, 1.0], "min_area": 80},
    },
    "ocr": [{"label": "inv_text", "region": "inventory", "min_conf": 50}],
}


def _write_json_map(tmp_path, data=None, name="testgame.json"):
    p = tmp_path / name
    p.write_text(json.dumps(data if data is not None else BASE))
    return str(p)


# ── loading ──────────────────────────────────────────────────────────────────
def test_load_map_json(tmp_path):
    m = M.load_map(_write_json_map(tmp_path))
    assert m["game"] == "testgame"
    assert m["regions"]["inventory"] == [1000, 600, 200, 150]
    assert m["colors"]["tree"]["min_area"] == 80


def test_load_map_yaml(tmp_path):
    yaml = pytest.importorskip("yaml")
    p = tmp_path / "testgame.yaml"
    p.write_text(yaml.safe_dump(BASE))
    m = M.load_map(str(p))
    assert m["game"] == "testgame"
    assert m["regions"]["board"] == [200, 80, 640, 640]


def test_load_map_missing_file_raises(tmp_path):
    with pytest.raises(M.MapError, match="not found"):
        M.load_map(str(tmp_path / "nope.yaml"))


def test_load_map_rejects_bad_region(tmp_path):
    bad = dict(BASE, regions={"x": [1, 2, 3]})   # 3 elems, not 4
    with pytest.raises(M.MapError, match="region"):
        M.load_map(_write_json_map(tmp_path, bad))


# ── region scaling ───────────────────────────────────────────────────────────
def test_region_scaling_to_frame(tmp_path):
    m = M.load_map(_write_json_map(tmp_path))
    # frame is half the reference viewport in both axes → regions halve
    r = M.scale_region(m, "inventory", frame_size=(640, 400))
    assert r == (500, 300, 100, 75)


def test_region_scaling_identity_at_reference_size(tmp_path):
    m = M.load_map(_write_json_map(tmp_path))
    r = M.scale_region(m, "board", frame_size=(1280, 800))
    assert r == (200, 80, 640, 640)


def test_scale_region_unknown_name_raises(tmp_path):
    m = M.load_map(_write_json_map(tmp_path))
    with pytest.raises(M.MapError, match="unknown region"):
        M.scale_region(m, "nope", frame_size=(1280, 800))


# ── spec compilation ─────────────────────────────────────────────────────────
def test_spec_from_map_color_and_ocr(tmp_path):
    m = M.load_map(_write_json_map(tmp_path))
    spec = M.spec_from_map(m, frame_size=(1280, 800))
    kinds = {s["kind"] for s in spec}
    assert kinds == {"color", "text"}
    color = next(s for s in spec if s["kind"] == "color")
    assert color["label"] == "tree" and color["min_area"] == 80
    text = next(s for s in spec if s["kind"] == "text")
    assert text["label"] == "inv_text"
    assert text["region"] == (1000, 600, 200, 150)   # resolved from region name


def test_spec_from_map_scales_ocr_region(tmp_path):
    m = M.load_map(_write_json_map(tmp_path))
    spec = M.spec_from_map(m, frame_size=(640, 400))
    text = next(s for s in spec if s["kind"] == "text")
    assert text["region"] == (500, 300, 100, 75)


def test_spec_from_map_template_loads_sprite(tmp_path):
    sprite_dir = tmp_path / "sprites"
    sprite_dir.mkdir()
    Image.new("RGB", (16, 16), (220, 40, 40)).save(sprite_dir / "gem.png")
    data = dict(BASE, templates={"gem": "sprites/gem.png",
                                 "ghost": "sprites/missing.png"})
    m = M.load_map(_write_json_map(tmp_path, data))
    spec = M.spec_from_map(m, frame_size=(1280, 800))
    tmpls = [s for s in spec if s["kind"] == "template"]
    # the present sprite loads; the missing one is skipped (not a crash)
    assert len(tmpls) == 1
    assert tmpls[0]["label"] == "gem"
    assert tmpls[0]["template"].size == (16, 16)


def test_list_maps_finds_shipped_maps():
    # the maps/ dir ships with starter maps; the loader must at least parse them
    names = M.list_maps()
    assert "lichess" in names
    assert "openrsc" in names
    for n in names:
        m = M.load_map(M.map_path(n))
        assert m["game"] == n
