"""Bounded CPU microbenchmark; no browser, model, network or frame-path I/O."""
import io
import time
import pytest
import operator_diagnostics as metrics


def test_instrumentation_cost_below_five_percent_of_fixture_encode():
    Image = pytest.importorskip('PIL.Image')
    frame = Image.effect_noise((1280, 720), 30).convert('RGB')
    start = time.process_time()
    for _ in range(30):
        frame.save(io.BytesIO(), format='JPEG', quality=70)
    encode = (time.process_time() - start) / 30
    metrics.debug_recording(False)
    start = time.process_time()
    for _ in range(10000):
        for name in ('capture_ms', 'capture_encode_ms', 'frames_sent', 'frame_bytes'):
            metrics.record(name, 1)
    recording = (time.process_time() - start) / 10000
    overhead = recording / encode * 100
    print(f'four metric records: {recording*1e6:.2f} us/frame; JPEG fixture: {encode*1e6:.2f} us/frame; CPU ratio: {overhead:.2f}%')
    assert overhead < 5
