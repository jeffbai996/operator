"""MJPEG frame splitting for the sandbox live stream — pure-function tests.

Run from modules/operator:  PYTHONPATH=. pytest tests/test_sandbox_stream.py -q
"""
import importlib.util
import pathlib

_here = pathlib.Path(__file__).resolve()
for _cand in (_here.parents[1] / "computer-use" / "sandbox_container.py",
              _here.parents[2] / "computer-use" / "sandbox_container.py"):
    if _cand.exists():
        _p = _cand
        break
_spec = importlib.util.spec_from_file_location("sandbox_container", _p)
sb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sb)

SOI, EOI = b"\xff\xd8", b"\xff\xd9"


def jpg(body: bytes = b"x") -> bytes:
    return SOI + body + EOI


def test_single_complete_frame():
    frames, tail = sb.split_jpegs(jpg(b"aaa"))
    assert frames == [jpg(b"aaa")] and tail == b""


def test_multiple_frames_returns_all_in_order():
    buf = jpg(b"1") + jpg(b"2") + jpg(b"3")
    frames, tail = sb.split_jpegs(buf)
    assert frames == [jpg(b"1"), jpg(b"2"), jpg(b"3")] and tail == b""


def test_partial_frame_kept_as_tail():
    partial = SOI + b"incomplete"
    frames, tail = sb.split_jpegs(jpg(b"done") + partial)
    assert frames == [jpg(b"done")] and tail == partial


def test_tail_completes_on_next_chunk():
    first, second = jpg(b"stream")[:5], jpg(b"stream")[5:]
    frames, tail = sb.split_jpegs(first)
    assert frames == [] and tail == first
    frames, tail = sb.split_jpegs(tail + second)
    assert frames == [jpg(b"stream")] and tail == b""


def test_garbage_before_soi_dropped():
    frames, tail = sb.split_jpegs(b"\x00\x01junk" + jpg(b"ok"))
    assert frames == [jpg(b"ok")] and tail == b""


def test_no_frame_at_all_drops_garbage():
    frames, tail = sb.split_jpegs(b"no jpeg markers here")
    assert frames == [] and tail == b""


# ── safe_rel: the Transfer path gate ─────────────────────────────────────────
import pytest


def test_safe_rel_accepts_exchange_dirs():
    assert sb.safe_rel("Downloads/report.csv") == "Downloads/report.csv"
    assert sb.safe_rel("Desktop/a b.png") == "Desktop/a b.png"


@pytest.mark.parametrize("bad", [
    "Downloads/../../../etc/passwd", "/etc/passwd", "Downloads",
    "secrets/x", "Downloads/.hidden", "Downloads/..", "Downloads/a/b",
    "Downloads\\..\\x",
])
def test_safe_rel_rejects_escapes(bad):
    with pytest.raises(sb.SandboxError):
        sb.safe_rel(bad)
