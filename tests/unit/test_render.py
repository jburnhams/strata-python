"""render against the real ffmpeg on tiny generated files whose frames say their own number (the picture's brightness is
16 + 2 x frame), so a test reads back exactly which source frames each cut kept.

CI installs ffmpeg and sets STRATA_TEST_FFMPEG=1, so a missing tool fails there; elsewhere these skip and say why."""

import json
import os
import shutil
import subprocess
from fractions import Fraction

import pytest

from strata_python import media, mediatime, render
from strata_python.mediatime import Span

HAVE = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
pytestmark = pytest.mark.skipif(
    not HAVE and not os.environ.get("STRATA_TEST_FFMPEG"),
    reason="ffmpeg is not installed (CI sets STRATA_TEST_FFMPEG=1 so it fails there instead)",
)


def _make(out, *args):
    if not HAVE:
        pytest.fail("ffmpeg and ffprobe must be installed for this test")
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args, str(out)], check=True)
    return str(out)


# 100 frames at 25 fps, each a flat grey of 16 + 2 x its number, a keyframe every 25 frames (so a seek lands between).
_NUMBERED = "geq=lum='16+2*N':cb=128:cr=128"


@pytest.fixture(scope="module")
def sources(tmp_path_factory):
    d = tmp_path_factory.mktemp("render")
    return {
        "a": _make(
            d / "a.mp4",
            "-f", "lavfi", "-i", "color=c=black:size=320x240:rate=25:duration=4",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=4",
            "-vf", _NUMBERED, "-c:v", "libx264", "-qp", "0", "-g", "25", "-pix_fmt", "yuv420p", "-c:a", "aac",
        ),
        "silent": _make(  # no audio, interlaced as a tape is, and a size that is not the output's
            d / "silent.mkv",
            "-f", "lavfi", "-i", "color=c=black:size=720x576:rate=25:duration=2",
            "-vf", _NUMBERED + ",setfield=tff", "-c:v", "libx264", "-qp", "0", "-g", "25", "-pix_fmt", "yuv420p",
        ),
    }  # fmt: skip


def _frame_numbers(path: str) -> list[int]:
    """The source frame number of every frame of `path`, from its mean brightness (ffmpeg's signalstats)."""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-vf", "signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=-",
         "-f", "null", "-"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout  # fmt: skip
    return [round((float(line.split("=")[1]) - 16) / 2) for line in out.splitlines() if "YAVG" in line]


def _samples(path: str) -> int:
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-map", "0:a:0", "-f", "s16le", "-ac", "1", "-"],
        check=True,
        capture_output=True,
    ).stdout
    return len(out) // 2


def test_each_cut_keeps_exactly_its_frames_in_order(sources, tmp_path):
    cuts = [
        render.Cut(sources["a"], Span.of("13/25", "1"), interlace="progressive"),  # frames 13..24, across no keyframe
        render.Cut(sources["a"], Span.of("3", "86/25"), interlace="progressive"),  # 75..85, starting on a keyframe
        render.Cut(sources["a"], Span.of("0", "2/25"), interlace="progressive"),  # 0..1, back to the start
        render.Cut(sources["a"], Span.of("1.01", "1.1"), interlace="progressive"),  # off-frame: frames starting inside
    ]
    made = render.render(cuts, tmp_path / "reel.mp4", output=render.Output(crf=0, preset="ultrafast"))
    expected = [*range(13, 25), *range(75, 86), 0, 1, 26, 27]
    assert _frame_numbers(str(tmp_path / "reel.mp4")) == expected
    assert made.frames == len(expected)
    assert made.duration == Fraction(len(expected), 25)
    assert [c["frames"] for c in made.cuts] == [[13, 25], [75, 86], [0, 2], [26, 28]]
    assert [c["at"] for c in made.cuts] == ["0", "12/25", "23/25", "1"]


def test_sound_is_exactly_as_long_as_the_picture_with_silence_for_a_file_without_it(sources, tmp_path):
    cuts = [
        render.Cut(sources["a"], Span.of("0.4", "1.24")),
        render.Cut(sources["silent"], Span.of("0", "0.48")),
        render.Cut(sources["a"], Span.of("3.6", "4"), gain_db=-6),
    ]
    made = render.render(cuts, tmp_path / "reel.mp4", output=render.Output(preset="ultrafast"))
    assert made.duration == Fraction(21 + 12 + 10, 25)
    # AAC adds priming samples at the start; decoded, the file is the edit's length to within one AAC frame.
    assert abs(_samples(str(tmp_path / "reel.mp4")) - made.duration * 48000) <= 1024
    # The first file's shape (320x240) is the output's; the 720x576 file (5:4, square pixels) is fitted inside it.
    assert made.probe.video is not None
    assert (made.probe.video.width, made.probe.video.height) == (320, 240)
    assert made.cuts[1]["interlace"] == "tff"


def test_the_same_edit_renders_the_same_bytes_and_says_how(sources, tmp_path):
    cuts = [render.Cut(sources["a"], Span.of("1", "2")), render.Cut(sources["a"], Span.of("0", "0.5"))]
    one = render.render(cuts, tmp_path / "one.mp4", output=render.Output(preset="ultrafast"))
    two = render.render(cuts, tmp_path / "two.mp4", output=render.Output(preset="ultrafast"))
    assert one.sha256 == two.sha256
    manifest = one.manifest()
    json.dumps(manifest)
    assert manifest["recipe"] == render.RECIPE
    assert manifest["file"]["sha256"] == one.sha256
    assert manifest["file"]["frames"] == 38
    assert manifest["output"]["crf"] == 18
    assert "x264 core" in manifest["ffmpeg"]
    assert str(tmp_path / "one.mp4") in manifest["command"]
    assert one.ffmpeg.startswith(render.ffmpeg_version())


def test_output_height_is_never_above_the_source_and_the_rate_can_be_changed(sources, tmp_path):
    made = render.render(
        [render.Cut(sources["silent"], Span.of("0", "1"))],
        tmp_path / "small.mp4",
        output=render.Output(height=1080, rate=mediatime.Rate.parse("50"), preset="ultrafast"),
    )
    assert made.height == 576
    assert made.frames == 50
    assert made.probe.video is not None
    assert made.probe.video.height == 576


def test_an_empty_edit_or_a_cut_with_no_frame_is_refused(sources, tmp_path):
    with pytest.raises(ValueError, match="no cuts"):
        render.render([], tmp_path / "x.mp4")
    with pytest.raises(ValueError, match="keeps no frame"):
        render.render([render.Cut(sources["a"], Span.of("1.01", "1.02"))], tmp_path / "x.mp4")
    assert not (tmp_path / "x.mp4").exists()


def test_a_failed_render_leaves_nothing(sources, tmp_path):
    with pytest.raises(media.MediaError):
        render.render(
            [render.Cut(sources["a"], Span.of("0", "1"))], tmp_path / "x.mp4", output=render.Output(preset="no-such")
        )
    assert not (tmp_path / "x.mp4").exists()


def test_sound_starts_with_the_cut_s_first_frame_not_at_the_seek(sources, tmp_path):
    # Silence, then a tone from exactly 1 s (frame 25). The cut starts on that frame, so its sound starts at once: the
    # half frame the input is sought early (0.98 s) is trimmed from the sound as it is from the picture.
    tone = _make(
        tmp_path / "tone.mkv",
        "-f", "lavfi", "-i", "color=c=black:size=320x240:rate=25:duration=2",
        "-f", "lavfi", "-i", "aevalsrc='if(gte(t,1),0.8*sin(2*PI*1000*t),0)':s=48000:d=2",
        "-c:v", "libx264", "-g", "25", "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le",
    )  # fmt: skip
    render.render([render.Cut(tone, Span.of("1", "1.4"), interlace="progressive")], tmp_path / "reel.mp4")
    pcm = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(tmp_path / "reel.mp4"), "-map", "0:a:0", "-f", "s16le", "-ac", "1", "-"],
        check=True,
        capture_output=True,
    ).stdout
    loud = next(
        i for i in range(len(pcm) // 2) if abs(int.from_bytes(pcm[2 * i : 2 * i + 2], "little", signed=True)) > 8000
    )
    # 20 ms (960 samples) late before the trim; AAC smears an onset by a few dozen samples at most.
    assert loud < 200
