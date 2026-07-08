"""maps.py — per-game map files: named UI regions + sprite templates + colour
specs, so the planner can say "find the inventory" instead of re-deriving pixel
rectangles every run.

A map is a small YAML (or JSON) file in vision/maps/:

    game: lichess
    viewport: [1280, 800]          # reference frame size the coords were taken at
    regions:                       # name -> [x, y, w, h]
      board: [212, 76, 640, 640]
    templates:                     # label -> sprite path (relative to the map file)
      white_pawn: sprites/lichess/white_pawn.png
    colors:                        # label -> HSV blob spec
      highlight: {lo: [50, 0.3, 0.5], hi: [70, 1.0, 1.0], min_area: 100}
    ocr:                           # OCR passes; region is a region NAME or [x,y,w,h]
      - {label: clock, region: clock_area, min_conf: 40}

`spec_from_map()` compiles a map into the finder-spec list `build_world_state()`
consumes, scaling regions from the reference viewport to the actual frame size.
Sprites that don't exist yet are skipped (maps ship regions-first; sprites get
added as they're captured — a missing file must not brick perception).

YAML needs PyYAML (lazy import; .json maps work without it).
"""
from __future__ import annotations

import json
import os

from PIL import Image

_HERE = os.path.dirname(os.path.abspath(__file__))
MAPS_DIR = os.path.join(_HERE, "maps")


class MapError(ValueError):
    """A map file is missing or malformed."""


def map_path(name: str) -> str:
    """Path of a shipped map by game name (yaml preferred, json fallback)."""
    for ext in (".yaml", ".yml", ".json"):
        p = os.path.join(MAPS_DIR, name + ext)
        if os.path.exists(p):
            return p
    raise MapError(f"map not found for game {name!r} in {MAPS_DIR}")


def list_maps() -> list:
    """Game names of all shipped maps."""
    if not os.path.isdir(MAPS_DIR):
        return []
    names = set()
    for f in os.listdir(MAPS_DIR):
        base, ext = os.path.splitext(f)
        if ext in (".yaml", ".yml", ".json"):
            names.add(base)
    return sorted(names)


def _parse(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    if path.endswith(".json"):
        return json.loads(raw)
    try:
        import yaml
    except ImportError as e:
        raise MapError(
            f"{os.path.basename(path)} is YAML but PyYAML is not installed "
            "(pip install pyyaml, or use a .json map)") from e
    return yaml.safe_load(raw)


def load_map(path: str) -> dict:
    """Load + validate a map file. Returns the dict with `_dir` set to the map's
    directory (sprite paths resolve against it)."""
    if not os.path.exists(path):
        raise MapError(f"map file not found: {path}")
    try:
        m = _parse(path)
    except (ValueError, OSError) as e:
        raise MapError(f"could not parse {path}: {e}") from e
    if not isinstance(m, dict) or not m.get("game"):
        raise MapError(f"{path}: map must be a mapping with a 'game' name")
    vp = m.get("viewport") or [1280, 800]
    if not (isinstance(vp, (list, tuple)) and len(vp) == 2):
        raise MapError(f"{path}: viewport must be [width, height]")
    m["viewport"] = [int(vp[0]), int(vp[1])]
    for name, r in (m.get("regions") or {}).items():
        if not (isinstance(r, (list, tuple)) and len(r) == 4):
            raise MapError(f"{path}: region {name!r} must be [x, y, w, h]")
    m["_dir"] = os.path.dirname(os.path.abspath(path))
    return m


def scale_region(m: dict, name: str, frame_size: tuple) -> tuple:
    """A named region scaled from the map's reference viewport to `frame_size`
    (w, h). Returns (x, y, w, h) ints."""
    regions = m.get("regions") or {}
    if name not in regions:
        raise MapError(f"unknown region {name!r} (map has: {sorted(regions)})")
    return _scale(regions[name], m["viewport"], frame_size)


def _scale(region, viewport, frame_size) -> tuple:
    fx = frame_size[0] / viewport[0]
    fy = frame_size[1] / viewport[1]
    x, y, w, h = region
    return (int(round(x * fx)), int(round(y * fy)),
            int(round(w * fx)), int(round(h * fy)))


def spec_from_map(m: dict, frame_size: tuple) -> list:
    """Compile a loaded map into the finder-spec list build_world_state() takes.

    - colors  -> {"kind": "color", ...}
    - templates -> {"kind": "template", "template": <PIL image>} (missing sprite
      files are skipped, not fatal — regions-first maps are valid)
    - ocr     -> {"kind": "text", "region": resolved+scaled (x,y,w,h)}
    """
    spec = []
    for label, c in (m.get("colors") or {}).items():
        spec.append({"kind": "color", "label": label,
                     "lo": tuple(c["lo"]), "hi": tuple(c["hi"]),
                     "min_area": int(c.get("min_area", 60))})
    for label, rel in (m.get("templates") or {}).items():
        p = rel if os.path.isabs(rel) else os.path.join(m.get("_dir", MAPS_DIR), rel)
        if not os.path.exists(p):
            continue                      # sprite not captured yet — skip quietly
        spec.append({"kind": "template", "label": label,
                     "template": Image.open(p).convert("RGB")})
    for o in (m.get("ocr") or []):
        region = o.get("region")
        if isinstance(region, str):
            region = scale_region(m, region, frame_size)
        elif isinstance(region, (list, tuple)) and len(region) == 4:
            region = _scale(region, m["viewport"], frame_size)
        else:
            region = None                 # whole frame
        spec.append({"kind": "text", "label": o.get("label", "text"),
                     "region": region, "min_conf": float(o.get("min_conf", 40.0))})
    return spec
