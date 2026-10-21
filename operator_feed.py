"""Per-viewer image encoding; capture geometry and CDP remain independent."""
from collections import OrderedDict
from dataclasses import dataclass
import io
import threading
from PIL import Image


class FrameEncoder:
    """At most two derived JPEGs, shared by viewers of the latest source frames."""
    def __init__(self) -> None:
        self._cache = OrderedDict()
        self._lock = threading.Lock()

    def for_viewer(self, data: bytes, tier: str, source_tier: str = "hi") -> bytes:
        rank = {"eco": 0, "lo": 1, "hi": 2}
        # A native lo/eco capture is already compressed. Encoding again costs
        # CPU and loses text detail; only derive strictly lower viewer tiers.
        if rank.get(tier, 2) >= rank.get(source_tier, 2):
            return data
        if not data or tier not in {"lo", "eco"}:
            return data
        key = (data, tier)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
            try:
                cap, quality = (640, 24) if tier == "eco" else (900, 35)
                with Image.open(io.BytesIO(data)) as source:
                    image = source.convert("RGB")
                    if image.width > cap:
                        image = image.resize((cap, max(1, round(image.height * cap / image.width))), Image.Resampling.LANCZOS)
                    output = io.BytesIO()
                    image.save(output, "JPEG", quality=quality)
                    encoded = output.getvalue()
            except (OSError, ValueError):
                # Keep the existing full-frame fallback; client byte pacing still applies.
                return data
            self._cache[key] = encoded
            while len(self._cache) > 2:
                self._cache.popitem(last=False)
            return encoded


@dataclass(frozen=True)
class FrameSnapshot:
    data: bytes
    sequence: int
    epoch: str
    target_id: str
    source_tier: str
    width: int
    height: int
    capture_started: float
    capture_finished: float
    published_at: float


def producer_delay(interval: float, elapsed: float, *, active: bool, busy: bool) -> float:
    """Active capture is start-to-start; idle and agent-sharing limits stay intact."""
    return max(0.0, interval - elapsed) if active and not busy else interval
