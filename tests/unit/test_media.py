"""media against the real ffmpeg and ffprobe on tiny generated files.

CI installs ffmpeg on every OS and sets STRATA_TEST_FFMPEG=1, so a missing tool fails there; elsewhere the tests that
need it skip and say why."""

import os
import shutil
import subprocess
import sys

import pytest

from strata_python import media

HAVE = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(
    not HAVE and not os.environ.get("STRATA_TEST_FFMPEG"),
    reason="ffmpeg is not installed (CI sets STRATA_TEST_FFMPEG=1 so it fails there instead)",
)


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    """A 2 s video with a stereo tone, a chapter and a title tag."""
    if not HAVE:
        pytest.fail("ffmpeg and ffprobe must be installed for this test")
    d = tmp_path_factory.mktemp("media")
    meta = d / "meta.txt"
    meta.write_text(
        ";FFMETADATA1\ntitle=Demo clip\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1000\ntitle=Opening\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=1000\nEND=2000\ntitle=Close\n"
    )
    out = d / "clip.mkv"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=64x48:rate=25:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=22050:duration=2",
            "-i", str(meta), "-map_metadata", "2", "-map_chapters", "2",
            "-map", "0:v", "-map", "1:a", "-ac", "2", "-c:v", "mpeg4", "-c:a", "pcm_s16le",
            str(out),
        ],
        check=True,
    )  # fmt: skip
    return out


@needs_ffmpeg
def test_probe_reads_streams_chapters_and_tags(clip):
    info = media.probe(clip)
    assert info.duration == pytest.approx(2.0, abs=0.1)
    assert "matroska" in info.format
    assert info.size == os.path.getsize(clip)
    assert info.tags["title"] == "Demo clip"
    v, a = info.video, info.audio
    assert v is not None
    assert a is not None
    assert (v.width, v.height, v.codec) == (64, 48, "mpeg4")
    assert v.fps == pytest.approx(25)
    assert (a.sample_rate, a.channels, a.codec) == (22050, 2, "pcm_s16le")
    assert [c.title for c in info.chapters] == ["Opening", "Close"]
    assert info.chapters[1].start == pytest.approx(1.0)
    assert info.raw["format"]["filename"] == str(clip)
    assert info.first("subtitle") is None
    assert media.duration(str(clip)) == pytest.approx(2.0, abs=0.1)


@needs_ffmpeg
def test_probe_of_a_file_that_is_not_media_says_so(tmp_path):
    bad = tmp_path / "notes.txt"
    bad.write_bytes(b"\x00\x01 not media")
    with pytest.raises(media.MediaError, match=r"ffprobe cannot read .*notes\.txt"):
        media.probe(bad)
    with pytest.raises(media.MediaError, match="cannot read"):
        media.probe(tmp_path / "missing.wav")


@needs_ffmpeg
def test_ffmpeg_writes_output_and_returns_stdout(clip, tmp_path):
    out = tmp_path / "tone.wav"
    assert media.ffmpeg(["-i", clip, "-vn", "-ac", "1", out]) == b""
    assert media.probe(out).audio.channels == 1  # ty: ignore[possibly-missing-attribute]
    raw = media.ffmpeg(["-i", clip, "-vn", "-t", "0.5", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"])
    assert len(raw) == 8000  # 0.5 s x 8000 Hz x 2 bytes
    piped = media.ffmpeg(["-f", "s16le", "-ar", "8000", "-ac", "1", "-i", "-", "-f", "s16le", "-"], input=raw)
    assert piped == raw


@needs_ffmpeg
def test_ffmpeg_failure_carries_its_reason(tmp_path):
    with pytest.raises(
        media.MediaError, match=r"ffmpeg failed \(exit \d+\): .*(No such file|not found|does not exist)"
    ):
        media.ffmpeg(["-i", tmp_path / "missing.mkv", tmp_path / "out.wav"])


@needs_ffmpeg
def test_ffmpeg_progress_reports_seconds_written(clip, tmp_path):
    seen: list[float] = []
    media.ffmpeg(["-i", clip, "-c:v", "mpeg4", tmp_path / "copy.mkv"], progress=seen.append)
    assert seen
    assert seen == sorted(seen)
    assert max(seen) == pytest.approx(2.0, abs=0.2)
    with pytest.raises(media.MediaError, match="ffmpeg failed"):
        media.ffmpeg(["-i", tmp_path / "missing.mkv", tmp_path / "x.wav"], progress=seen.append)
    raw = media.ffmpeg(["-i", clip, "-vn", "-t", "1", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"])
    fed: list[float] = []
    media.ffmpeg(
        ["-f", "s16le", "-ar", "8000", "-ac", "1", "-i", "-", tmp_path / "fed.wav"], input=raw, progress=fed.append
    )
    assert media.duration(tmp_path / "fed.wav") == pytest.approx(1.0, abs=0.05)  # the input reached ffmpeg
    assert fed


@needs_ffmpeg
def test_timeouts(clip, tmp_path):
    endless = ["-f", "lavfi", "-i", "sine=frequency=1000", "-f", "null", "-"]
    with pytest.raises(media.MediaError, match=r"longer than 0\.5 s"):
        media.ffmpeg(endless, timeout=0.5)
    with pytest.raises(media.MediaError, match=r"longer than 0\.5 s"):
        media.ffmpeg(["-re", *endless], timeout=0.5, progress=lambda s: None)
    with pytest.raises(media.MediaError, match="ffprobe took longer"):
        media.probe(clip, run=_raises(subprocess.TimeoutExpired("ffprobe", 1)), timeout=1)


def _raises(e):
    def run(*a, **kw):
        raise e

    return run


def test_tool_lookup_and_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("STRATA_FFMPEG", raising=False)
    monkeypatch.delenv("STRATA_FFPROBE", raising=False)
    with pytest.raises(media.MediaError, match=r"ffmpeg is not installed .* set STRATA_FFMPEG"):
        media.tool("ffmpeg")
    assert media.available() == {"ffmpeg": False, "ffprobe": False}
    monkeypatch.setenv("STRATA_FFPROBE", sys.executable)
    assert media.tool("ffprobe") == sys.executable
    assert media.available() == {"ffmpeg": False, "ffprobe": True}


def test_probe_answers_ffprobe_gives(monkeypatch):
    """The edges real files rarely show: no container duration, a "0/0" rate, junk output, a tool that will not start."""
    monkeypatch.setenv("STRATA_FFPROBE", "ffprobe")

    def answer(stdout, code=0, stderr=""):
        return lambda cmd, **kw: subprocess.CompletedProcess(cmd, code, stdout, stderr)

    out = '{"format": {}, "streams": [{"index": 0, "codec_type": "audio", "duration": "3.5", "r_frame_rate": "0/0", "sample_rate": "x"}]}'
    info = media.probe("a.mp3", run=answer(out, stderr="estimating duration from bitrate\n"))
    assert info.duration == 3.5  # the longest stream's, when the container has none
    assert info.audio.fps is None  # ty: ignore[possibly-missing-attribute]
    assert info.audio.sample_rate is None  # ty: ignore[possibly-missing-attribute]
    assert info.warnings == "estimating duration from bitrate"
    with pytest.raises(media.MediaError, match="no duration"):
        media.duration("a", run=answer('{"format": {}, "streams": []}'))
    with pytest.raises(media.MediaError, match="no JSON"):
        media.probe("a", run=answer("garbage"))
    with pytest.raises(media.MediaError, match="exit 1"):
        media.probe("a", run=answer("", code=1))
    with pytest.raises(media.MediaError, match="could not start"):
        media.probe("a", run=_raises(OSError("exec format error")))
