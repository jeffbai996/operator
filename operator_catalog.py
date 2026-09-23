"""Load shared model policy when Operator runs outside the server process."""
from pathlib import Path
import sys

_catalog_dir = str(Path(__file__).resolve().parent / 'catalog')
if _catalog_dir not in sys.path:
    sys.path.insert(0, _catalog_dir)
from model_catalog import aliases, default_slug, picker
