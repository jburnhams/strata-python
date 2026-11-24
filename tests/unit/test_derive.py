"""derive against the real ffmpeg on tiny generated clips: interlaced PAL-shaped tape, progressive video, a tone.

CI installs ffmpeg on every OS and sets STRATA_TEST_FFMPEG=1, so a missing tool fails there; elsewhere these skip and
say why (as test_media does)."""

import os
import shutil
import subprocess
from fractions import Fraction

import pytest

from strata_python import derive, media

HAVE = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(
    not HAVE and not os.environ.get("STRATA_TEST_FFMPEG"),
    reason="ffmpeg is not installed (CI sets STRATA_TEST_FFMPEG=1 so it fails there instead)",
)


def _make(out, *args):
    if not HAVE:
        pytest.fail("ffmpeg and ffprobe must be installed for this test")
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args, str(out)], check=True)
    return out


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    d = tmp_path_factory.mktemp("derive")
    tape = "testsrc2=size=720x576:rate=50:duration=3"
    return {
        # PAL tape as a capture card writes it: 25 fps woven from 50 fields, 16:15 pixels, a tone, and a stream that
        # says nothing about its fields (setfield=prog), like the digitised VHS
        "silent": _make(
            d / "silent.mkv",
            "-f", "lavfi", "-i", tape,
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3",
            "-vf", "tinterlace=mode=interleave_top,setfield=prog,setsar=16/15",
            "-c:v", "libx264", "-crf", "30", "-c:a", "pcm_s16le",
        ),
        "said": _make(  # the same, with the stream saying top field first
            d / "said.mkv", "-f", "lavfi", "-i", tape,
            "-vf", "tinterlace=mode=interleave_top,setsar=16/15", "-c:v", "libx264", "-crf", "30",
        ),
        "bff": _make(
            d / "bff.mkv", "-f", "lavfi", "-i", tape,
            "-vf", "tinterlace=mode=interleave_bottom,setfield=prog", "-c:v", "libx264", "-crf", "30",
        ),
        "progressive": _make(
            d / "prog.mp4", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=3", "-c:v", "libx264",
        ),
        "fine": _make(  # fine moving detail that idet calls both ways about evenly
            d / "fine.mp4", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=4", "-c:v", "libx264",
        ),
        "tone": _make(  # loud then silent: -6 dBFS square for 1 s, then 1 s of nothing
            d / "tone.wav", "-f", "lavfi", "-i", "aevalsrc='if(lt(t,1),0.5*(2*gt(sin(2*PI*200*t),0)-1),0)':s=8000:d=2",
        ),
    }  # fmt: skip


@needs_ffmpeg
@pytest.mark.parametrize(
    ("clip", "expected"),
    [("silent", "tff"), ("said", "tff"), ("bff", "bff"), ("progressive", "progressive"), ("fine", "progressive")],
)
def test_interlacing_from_the_stream_or_idet(clips, clip, expected):
    assert derive.interlacing(clips[clip]) == expected


@needs_ffmpeg
def test_interlacing_refuses_audio_only(clips):
    with pytest.raises(media.MediaError, match="no video"):
        derive.interlacing(clips["tone"])


@needs_ffmpeg
def test_proxy_keeps_source_time_square_pixels_and_a_short_gop(clips, tmp_path):
    out = tmp_path / "p.mp4"
    seen: list[float] = []
    info = derive.proxy(clips["silent"], out, progress=seen.append)
    v, a = info.video, info.audio
    assert v is not None
    assert a is not None
    # 720x576 at 16:15 displays as 768x576 (4:3); at 540 high that is 720 wide, square pixels
    assert (v.width, v.height, v.codec) == (720, 540, "h264")
    assert v.raw.get("sample_aspect_ratio") in (None, "1:1")
    assert v.fps == pytest.approx(25)  # one frame per frame, not one per field
    assert info.duration == pytest.approx(3.0, abs=0.05)
    assert (a.codec, a.channels) == ("aac", 2)
    assert "mp4" in info.format
    assert seen
    keys = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "frame=key_frame,pts_time", "-of", "csv=p=0",
         str(out)],
        capture_output=True, text=True, check=True,
    ).stdout.split()  # fmt: skip
    key_times = [float(t) for k, t in (line.strip(",").split(",")[:2] for line in keys) if k == "1"]
    assert key_times[:3] == pytest.approx([0.0, 0.48, 0.96])  # every 12 frames at 25 fps: half a second
    assert len(keys) == 75  # every source frame, at its own time
    assert not [n for n in os.listdir(tmp_path) if n != "p.mp4"]  # the temp file is gone


@needs_ffmpeg
def test_proxy_never_upscales_and_works_without_audio(clips, tmp_path):
    info = derive.proxy(clips["progressive"], tmp_path / "p.mp4", height=1080, interlace="progressive")
    assert info.video is not None
    assert (info.video.width, info.video.height) == (320, 240)
    assert info.audio is None


@needs_ffmpeg
def test_a_failed_proxy_leaves_nothing(clips, tmp_path):
    junk = tmp_path / "junk.mkv"
    junk.write_bytes(b"not footage" * 100)
    info = media.probe(clips["silent"])  # what it claimed to be when it was added
    with pytest.raises(media.MediaError, match="ffmpeg failed"):
        derive.proxy(junk, tmp_path / "p.mp4", info=info, interlace="tff")
    assert sorted(os.listdir(tmp_path)) == ["junk.mkv"]


@needs_ffmpeg
def test_thumbnails_one_per_step_with_exact_times(clips, tmp_path):
    folder = tmp_path / "thumbs"
    thumbs = derive.thumbnails(clips["silent"], folder, every=Fraction(1, 2), height=90)
    assert [t.time for t in thumbs] == [Fraction(i, 2) for i in range(6)]
    assert all(os.path.dirname(t.path) == str(folder) for t in thumbs)
    probe = media.probe(thumbs[0].path)
    assert probe.video is not None
    assert (probe.video.width, probe.video.height) == (120, 90)
    again = derive.thumbnails(clips["silent"], folder, every=1, interlace="tff")  # replaces the folder
    assert len(again) == 3
    assert sorted(os.listdir(folder)) == ["000000.jpg", "000001.jpg", "000002.jpg"]
    assert sorted(os.listdir(tmp_path)) == ["thumbs"]


def test_thumbnails_need_a_positive_step(tmp_path):
    with pytest.raises(ValueError, match="above zero"):
        derive.thumbnails(tmp_path / "x.mkv", tmp_path / "t", every=0)


@needs_ffmpeg
def test_a_failed_thumbnail_run_keeps_the_old_folder(clips, tmp_path):
    folder = tmp_path / "thumbs"
    folder.mkdir()
    (folder / "000000.jpg").write_bytes(b"old")
    junk = tmp_path / "junk.mkv"
    junk.write_bytes(b"not footage" * 100)
    with pytest.raises(media.MediaError, match="ffmpeg failed"):
        derive.thumbnails(junk, folder, info=media.probe(clips["silent"]), interlace="tff")
    assert (folder / "000000.jpg").read_bytes() == b"old"
    assert sorted(os.listdir(tmp_path)) == ["junk.mkv", "thumbs"]


@needs_ffmpeg
def test_peaks_are_the_lowest_and_highest_of_each_slice(clips):
    seen: list[float] = []
    p = derive.peaks(clips["tone"], per_second=10, progress=seen.append)
    assert p.per_second == 10
    assert len(p.min) == len(p.max) == 20
    assert p.seconds == 2
    assert all(hi == pytest.approx(0.5, abs=0.06) for hi in p.max[1:9])  # resampling rings a little at the edges
    assert all(lo == pytest.approx(-0.5, abs=0.06) for lo in p.min[1:9])
    assert all(abs(x) < 0.01 for x in p.max[11:] + p.min[11:])
    assert seen
    assert seen[-1] <= 2
    j = p.to_json(digits=2)
    assert j["per_second"] == 10
    assert len(j["min"]) == 20
    assert all(round(x, 2) == x for x in j["max"])


@needs_ffmpeg
def test_peaks_need_audio(clips):
    with pytest.raises(media.MediaError):
        derive.peaks(clips["progressive"])


def test_peaks_need_a_rate_that_divides_into_slices(tmp_path):
    with pytest.raises(ValueError, match="whole number"):
        derive.peaks(tmp_path / "x.wav", per_second=3, rate=8000)
    with pytest.raises(ValueError, match="slices of 1/0 s"):
        derive.peaks(tmp_path / "x.wav", per_second=0)


def test_reduce_takes_a_short_last_slice():
    p = derive.Peaks(2)
    data = b"".join(v.to_bytes(2, "little", signed=True) for v in [-100, 200, 300, -32768, 32767])
    derive._reduce(data, 2, p)
    assert p.min == [-100 / 32768, -1.0, 32767 / 32768]
    assert p.max == [200 / 32768, 300 / 32768, 32767 / 32768]


def test_every_recipe_has_a_version():
    assert set(derive.RECIPE) == {"proxy", "thumbnails", "peaks"}


@needs_ffmpeg
def test_thumbnails_reach_the_last_stretch_and_not_closer_than_frames(clips, tmp_path):
    # 3 s at a 2 s step: the second picture is in the last second, which ffmpeg's fps filter leaves out
    assert [t.time for t in derive.thumbnails(clips["progressive"], tmp_path / "t", every=2)] == [0, 2]
    with pytest.raises(ValueError, match="closer than the frames"):
        derive.thumbnails(clips["progressive"], tmp_path / "t", every=Fraction(1, 50))


@needs_ffmpeg
def test_two_thumbnail_runs_into_one_folder_do_not_share_a_staging_folder(clips, tmp_path):
    import threading

    folder = tmp_path / "thumbs"
    errors: list[BaseException] = []

    def run() -> None:
        try:
            derive.thumbnails(clips["silent"], folder, every=1, interlace="tff")
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=run) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(os.listdir(folder)) == ["000000.jpg", "000001.jpg", "000002.jpg"]
    assert sorted(os.listdir(tmp_path)) == ["thumbs"]
    assert all(isinstance(e, OSError) for e in errors)  # a loser of the final rename, never a mixed folder
