"""A held /operator/frame request returns when a frame lands, not on a 20ms tick."""
import threading
import time

import pytest

import operator_view as OV


@pytest.fixture
def client(monkeypatch):
    from flask import Flask
    monkeypatch.setattr(OV._streamer, "ensure_running", lambda: None)
    monkeypatch.setattr(OV._desktop_feed, "ensure_running", lambda s: None)
    monkeypatch.setitem(OV._active_surface, "name", "browser")
    app = Flask(__name__)
    app.register_blueprint(OV.bp)
    return app.test_client()


def _delivery_latencies(client, trials, publish_after):
    """Seconds from each publish to the held request returning the new frame."""
    out = []
    for i in range(trials):
        OV._streamer._publish_frame(b"\xff\xd8base-%d" % i)
        since = OV._frame_token("browser", OV._streamer)
        published = {}

        def publish():
            time.sleep(publish_after + i * 0.0017)  # stagger across the 20ms poll phase
            published["at"] = time.monotonic()
            OV._streamer._publish_frame(b"\xff\xd8next-%d" % i)

        t = threading.Thread(target=publish)
        t.start()
        resp = client.get(f"/operator/frame?since={since}&wait=900")
        returned = time.monotonic()
        t.join()
        assert resp.status_code == 200
        out.append(returned - published["at"])
    return out


def test_held_frame_request_returns_within_a_few_ms_of_the_publish(client):
    latencies = _delivery_latencies(client, trials=12, publish_after=0.05)
    mean_ms = sum(latencies) / len(latencies) * 1000
    assert mean_ms < 5, f"mean delivery latency {mean_ms:.1f}ms (20ms poll averages ~10ms)"


def test_held_frame_request_with_no_new_frame_still_returns_204_at_the_deadline(client):
    OV._streamer._publish_frame(b"\xff\xd8still")
    since = OV._frame_token("browser", OV._streamer)
    started = time.monotonic()
    resp = client.get(f"/operator/frame?since={since}&wait=150")
    elapsed = time.monotonic() - started
    assert resp.status_code == 204
    # The lower bound is the behavior (no early return); the upper bound only
    # rules out a hang, with room for a loaded gate host.
    assert 0.14 <= elapsed < 1.0


def test_held_frame_request_notices_a_surface_switch_without_a_publish(client, monkeypatch):
    OV._streamer._publish_frame(b"\xff\xd8browser")
    since = OV._frame_token("browser", OV._streamer)

    def switch():
        time.sleep(0.05)
        OV._active_surface["name"] = "desktop-sandbox"

    t = threading.Thread(target=switch)
    t.start()
    started = time.monotonic()
    resp = client.get(f"/operator/frame?since={since}&wait=3000")
    elapsed = time.monotonic() - started
    t.join()
    assert resp.status_code == 200
    # Returning far inside the 3s deadline proves the switch was noticed
    # without a publish; the margin keeps a loaded gate host from flaking it.
    assert elapsed < 1.0
