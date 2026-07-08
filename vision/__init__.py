"""operator.vision — local zero-token perception.
Turns screenshots into a labeled WorldState the agent can ground on. Wired
into the agent loop via control/mcp_server.py (the perceive + game_macro
tools); maps.py adds per-game region/sprite maps, overlay.py the grounding
aids (grid, crop, target markers)."""
from .perceive import (find_template, find_color_blobs, read_text, build_world_state,  # noqa: F401
                       WorldState, Match, TextHit)
from .maps import load_map, map_path, list_maps, spec_from_map, scale_region, MapError  # noqa: F401
from .overlay import draw_grid, crop_region, annotate_targets  # noqa: F401
