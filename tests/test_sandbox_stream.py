"""MJPEG frame splitting for the sandbox live stream — pure-function tests.

Run from modules/operator:  PYTHONPATH=. pytest tests/test_sandbox_stream.py -q
"""
import importlib.util
import os
import pathlib
import subprocess

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


def test_sandbox_geometry_is_compact_five_by_four():
    assert (sb.SCREEN_W, sb.SCREEN_H) == (960, 768)
    assert sb.GEOMETRY == "960x768x24"


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


@pytest.mark.parametrize("directory", sb.FILE_DIRS)
def test_get_file_copies_regular_exchange_files(tmp_path, monkeypatch, directory):
    name = "résumé notes.txt"
    monkeypatch.setattr(sb, "ensure", lambda: None)

    def copy_regular(args, **_kwargs):
        pathlib.Path(args[-1]).write_bytes(b"ordinary sandbox file")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(sb, "_run", copy_regular)

    out = pathlib.Path(sb.get_file(f"{directory}/{name}", str(tmp_path)))

    assert out.name == name
    assert out.read_bytes() == b"ordinary sandbox file"


def test_get_file_rejects_a_copied_symlink_without_touching_its_target(
    tmp_path, monkeypatch
):
    secret = tmp_path / "host-secret.txt"
    secret.write_bytes(b"host secret")
    out_dir = tmp_path / "downloads"
    monkeypatch.setattr(sb, "ensure", lambda: None)

    def copy_symlink(args, **_kwargs):
        os.symlink(secret, args[-1])
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(sb, "_run", copy_symlink)

    with pytest.raises(sb.SandboxError, match="regular files"):
        sb.get_file("Downloads/report.txt", str(out_dir))

    assert secret.read_bytes() == b"host secret"
    assert list(out_dir.iterdir()) == []


@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_get_file_rejects_copied_non_regular_files(tmp_path, monkeypatch, kind):
    monkeypatch.setattr(sb, "ensure", lambda: None)

    def copy_special(args, **_kwargs):
        candidate = pathlib.Path(args[-1])
        if kind == "directory":
            candidate.mkdir()
        else:
            os.mkfifo(candidate)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(sb, "_run", copy_special)

    with pytest.raises(sb.SandboxError, match="regular files"):
        sb.get_file("Downloads/report", str(tmp_path))


@pytest.mark.parametrize(
    ("content", "allowed"),
    [(b"12345678", True), (b"123456789", False)],
)
def test_get_file_enforces_final_copied_size(
    tmp_path, monkeypatch, content, allowed
):
    monkeypatch.setattr(sb, "MAX_FILE_BYTES", 8)
    monkeypatch.setattr(sb, "ensure", lambda: None)

    def copy_regular(args, **_kwargs):
        pathlib.Path(args[-1]).write_bytes(content)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(sb, "_run", copy_regular)

    if allowed:
        out = pathlib.Path(sb.get_file("Documents/report.bin", str(tmp_path)))
        assert out.read_bytes() == content
    else:
        with pytest.raises(sb.SandboxError, match="file too large to download"):
            sb.get_file("Documents/report.bin", str(tmp_path))


def test_get_file_uses_a_unique_host_output_and_cleans_up_on_error(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(sb, "ensure", lambda: None)

    def copy_regular(args, **_kwargs):
        pathlib.Path(args[-1]).write_bytes(b"safe bytes")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(sb, "_run", copy_regular)
    first = pathlib.Path(sb.get_file("Downloads/report.txt", str(tmp_path)))
    second = pathlib.Path(sb.get_file("Desktop/report.txt", str(tmp_path)))

    assert first != second
    assert first.name == second.name == "report.txt"
    assert first.read_bytes() == second.read_bytes() == b"safe bytes"

    def fail(_args, **_kwargs):
        raise sb.SandboxError("unsafe sandbox file")

    monkeypatch.setattr(sb, "_run", fail)
    before = set(tmp_path.iterdir())
    with pytest.raises(sb.SandboxError, match="unsafe sandbox file"):
        sb.get_file("Documents/bad.txt", str(tmp_path))
    assert set(tmp_path.iterdir()) == before


def test_list_files_hides_leaf_and_exchange_directory_symlinks(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    downloads = home / "Downloads"
    downloads.mkdir(parents=True)
    (downloads / "report.txt").write_bytes(b"safe")
    os.symlink(downloads / "report.txt", downloads / "linked.txt")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "outside.txt").write_bytes(b"outside")
    os.symlink(outside, home / "Desktop")

    monkeypatch.setattr(sb, "ensure", lambda: None)

    def local_exec(command, **_kwargs):
        return subprocess.run(
            command,
            capture_output=True,
            check=False,
            env={**os.environ, "HOME": str(home)},
        )

    monkeypatch.setattr(sb, "_exec", local_exec)

    listed = sb.list_files()

    assert [item["name"] for item in listed["Downloads"]] == ["report.txt"]
    assert listed["Desktop"] == []
