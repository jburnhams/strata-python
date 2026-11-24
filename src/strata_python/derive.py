"""Files made from footage so an editor can show it: a browser proxy, thumbnails at a fixed step, audio peaks, and
whether the picture is interlaced. Beside `media.probe`, on the same ffmpeg (`media.ffmpeg`, `MediaError`).

    interlacing(path)                          # "tff", "bff" or "progressive": the stream's field order, else idet
    proxy(src, "proxy.mp4", height=540)        # H.264 + AAC MP4, square pixels, a keyframe every half second
    thumbnails(src, "thumbs/", every=10)       # [Thumb(time=Fraction(0), path=".../000000.jpg"), ...]
    frames(src, "keys/", [12, 840, 4001])      # [Frame(number=12, path=".../000012.jpg"), ...]: chosen frames
    peaks(src, per_second=50).to_json()        # {"per_second": 50, "min": [...], "max": [...]}, -1 to 1

Choices that matter to an app:

- **Source time is kept.** The proxy has the source's frames at the source's times (frame rate passed through, the
  deinterlacer sends one frame per frame, not one per field), so a time picked on the proxy is the same time in the
  source and `mediatime` maps both. Every output starts at the source's first timestamp, as ffprobe's duration does.
- **Interlaced footage is deinterlaced for the screen** (bwdif, with the field order found). A digitised tape often
  does not say it is interlaced, so `interlacing` asks ffmpeg's `idet` filter on a stretch of it when the stream is
  silent. This is display only: restoring the picture (a better deinterlacer, chroma, noise) is a model's job and
  happens elsewhere; nothing here changes the source.
- **A short GOP** (a keyframe every `gop` seconds, no scene-cut keyframes) so seeking in a browser lands quickly and
  exactly; square pixels (`setsar=1`, width from the display aspect), since a browser's handling of anamorphic MP4
  differs; `+faststart`, so it plays before it is all downloaded; never larger than the source.
- **Thumbnails** are JPEGs, one per `every` seconds from the start: the first frame at or after each multiple of
  `every` (a `select` on the frame's time, so the last stretch of a file gets its picture, which ffmpeg's `fps` filter
  drops), named by their index; `Thumb.time` is `index * every`, exact, and the picture is at most a frame after it.
- **Frames** are JPEGs of chosen frames, by number (the n-th frame decoded, from 0: the count a shot detector gives,
  and `mediatime.Rate.frame_at` of a time from the file's start on constant-rate footage), named by that number. An
  editor wants one picture per shot (to show it, describe it, compare it with the next); a shot can be shorter than
  the thumbnails' step, so these are picked by number instead. Each run decodes from the start and stops after its
  last frame; the numbers go in runs of `FRAMES_PER_RUN`, so the command line stays short on Windows (a few hundred
  shots on a tape is a run or two).
- **Peaks** are the lowest and highest sample of each slice of the first audio stream mixed to mono, from -1 to 1:
  the pair strata-react's `Waveform` takes. The audio is read as it is decoded (`media.ffmpeg(sink=...)`), never held
  whole, so a two-hour tape costs the same memory as a minute.
- **`RECIPE`** names the version of each recipe. An app puts it in the parameters of the result it stores, so a
  changed recipe here makes its old files stop looking current.

Outputs are written atomically: a proxy through `files.atomic_path`, thumbnails into a temporary folder renamed into
place. Standard library only; the tests run the real ffmpeg on tiny generated files."""

from __future__ import annotations

import array
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Literal

from strata_python import files, media, mediatime

Interlace = Literal["tff", "bff", "progressive"]

#: The version of each recipe: raise one when its output changes for the same arguments.
RECIPE: dict[str, int] = {"proxy": 1, "thumbnails": 1, "peaks": 1, "frames": 1}

# Only an interlaced field order is taken at its word: "progressive" is what many encoders write for whatever they
# were given, interlaced tape included, so it is checked like a stream that says nothing.
_FIELDS: dict[str, Interlace] = {"tt": "tff", "tb": "tff", "bb": "bff", "bt": "bff"}
_IDET = re.compile(rb"lavfi\.idet\.multiple\.(tff|bff|progressive)=(\d+)")


def _video(info: media.Probe) -> media.Stream:
    v = info.video
    if v is None:
        raise media.MediaError(f"{info.path} has no video stream")
    return v


def interlacing(
    path: str | os.PathLike[str],
    *,
    info: media.Probe | None = None,
    frames: int = 500,
    timeout: float | None = 300,
) -> Interlace:
    """How the picture is scanned: the stream's `field_order` when it says interlaced, else what ffmpeg's `idet` filter finds on
    `frames` frames from a quarter of the way in (capped at five minutes, past a tape's leader and black). Interlaced
    when one field order is found on more frames than progressive and at least twice as often as the other. `info` is the
    file's probe when the caller has it (it is not read again)."""
    info = info or media.probe(path)
    said = _FIELDS.get(str(_video(info).raw.get("field_order", "")))
    if said is not None:
        return said
    start = min((info.duration or 0.0) / 4, 300.0)
    out = media.ffmpeg(
        [
            "-ss", f"{start:.3f}", "-i", path, "-map", "0:v:0", "-frames:v", str(frames),
            "-vf", "idet,metadata=mode=print:file=-", "-f", "null", "-",
        ],
        timeout=timeout,
    )  # fmt: skip
    counts = {"tff": 0, "bff": 0, "progressive": 0}
    for kind, n in _IDET.findall(out):  # cumulative per frame: the last of each is the total
        counts[kind.decode()] = int(n)
    order: Interlace = "tff" if counts["tff"] >= counts["bff"] else "bff"
    lead, other = counts[order], counts["bff" if order == "tff" else "tff"]
    # Interlaced footage is lopsided (one field order nearly every frame); fine detail in progressive footage fools
    # idet into calling frames both ways about evenly, so an even split is not an answer.
    if lead > counts["progressive"] and lead >= 2 * other:
        return order
    return "progressive"


def _deinterlace(fields: Interlace) -> list[str]:
    if fields == "progressive":
        return []
    return [f"bwdif=mode=send_frame:parity={fields}:deint=all"]


def _scale(height: int) -> str:
    # Width from the display aspect (dar includes the pixel aspect), both even for yuv420p, never above the source.
    return f"scale=w=trunc(oh*dar/2)*2:h=trunc(min({height}\\,ih)/2)*2,setsar=1"


def proxy(
    src: str | os.PathLike[str],
    out: str | os.PathLike[str],
    *,
    height: int = 540,
    gop: mediatime.Number = Fraction(1, 2),
    crf: int = 23,
    interlace: Interlace | None = None,
    info: media.Probe | None = None,
    progress: Callable[[float], None] | None = None,
    timeout: float | None = None,
) -> media.Probe:
    """Write a browser proxy of `src` to `out` (an MP4: H.264 at `crf`, AAC stereo from the first audio stream if
    there is one) and return its probe. `interlace` None finds it (`interlacing`); `progress(seconds)` as it is
    written."""
    info = info or media.probe(src)
    v = _video(info)
    fields = interlace or interlacing(src, info=info)
    rate = v.fps or 25.0
    keyint = str(max(1, round(rate * mediatime.rational(gop))))
    vf = ",".join([*_deinterlace(fields), _scale(height), "format=yuv420p"])
    with files.atomic_path(out) as tmp:
        media.ffmpeg(
            [
                "-i", src, "-map", "0:v:0", "-map", "0:a:0?", "-vf", vf, "-fps_mode", "passthrough",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
                "-g", keyint, "-keyint_min", keyint, "-sc_threshold", "0",
                "-c:a", "aac", "-b:a", "128k", "-ac", "2",
                "-movflags", "+faststart", "-f", "mp4", tmp,
            ],
            progress=progress,
            timeout=timeout,
        )  # fmt: skip
    return media.probe(out)


@dataclass(frozen=True)
class Thumb:
    time: Fraction  # seconds from the start, exact: index * every
    path: str


def thumbnails(
    src: str | os.PathLike[str],
    folder: str | os.PathLike[str],
    *,
    every: mediatime.Number = 10,
    height: int = 180,
    quality: int = 5,
    interlace: Interlace | None = None,
    info: media.Probe | None = None,
    progress: Callable[[float], None] | None = None,
    timeout: float | None = None,
) -> list[Thumb]:
    """Write one JPEG per `every` seconds of `src` into `folder` (replaced if it is there) and return them in time
    order. `quality` is ffmpeg's `-q:v`, 2 (best) to 31."""
    step = mediatime.rational(every)
    if step <= 0:
        raise ValueError(f"thumbnails need a step above zero, not {every}")
    info = info or media.probe(src)
    fields = interlace or interlacing(src, info=info)
    rate = _video(info).fps
    if rate and step < Fraction(1) / mediatime.rational(rate):
        raise ValueError(f"thumbnails {every} s apart are closer than the frames ({rate:g} a second)")
    slot = f"floor(t*{step.denominator}/{step.numerator})"  # which multiple of `every` a frame is in
    pick = f"select='isnan(prev_t)+gt({slot}\\,floor(prev_t*{step.denominator}/{step.numerator}))'"
    vf = ",".join([*_deinterlace(fields), pick, _scale(height)])
    named: str = os.fspath(folder)
    target = os.path.abspath(named)
    parent, name = os.path.split(target)
    os.makedirs(parent, exist_ok=True)
    part = tempfile.mkdtemp(prefix=f".{name}.", suffix=".part", dir=parent)  # one per call: two runs never share one
    try:
        media.ffmpeg(
            [
                "-i", src, "-map", "0:v:0", "-vf", vf, "-fps_mode", "passthrough", "-q:v", str(quality),
                "-start_number", "0",
                os.path.join(part, "%06d.jpg"),
            ],
            progress=progress,
            timeout=timeout,
        )  # fmt: skip
        shutil.rmtree(target, ignore_errors=True)
        os.replace(part, target)
    except BaseException:
        shutil.rmtree(part, ignore_errors=True)
        raise
    names = sorted(n for n in os.listdir(target) if n.endswith(".jpg"))
    return [Thumb(time=int(n[:-4]) * step, path=os.path.join(target, n)) for n in names]


@dataclass(frozen=True)
class Frame:
    number: int  # the n-th frame decoded, from 0
    path: str


#: How many frames one ffmpeg run picks: each costs about 16 characters of filter, and Windows caps a command line at
#: 32,767.
FRAMES_PER_RUN = 400


def frames(
    src: str | os.PathLike[str],
    folder: str | os.PathLike[str],
    numbers: Sequence[int],
    *,
    height: int = 360,
    quality: int = 3,
    interlace: Interlace | None = None,
    info: media.Probe | None = None,
    progress: Callable[[float], None] | None = None,
    timeout: float | None = None,
) -> list[Frame]:
    """Write a JPEG of each frame in `numbers` (counted from 0, repeats ignored) into `folder` (replaced if it is
    there) and return them in order. ValueError for a negative number; MediaError when a number is past the last
    frame. `progress(seconds)` as the file is read."""
    wanted = sorted(set(numbers))
    if wanted and wanted[0] < 0:
        raise ValueError(f"frames are counted from 0, not {wanted[0]}")
    info = info or media.probe(src)
    fields = interlace or interlacing(src, info=info)
    named: str = os.fspath(folder)
    target = os.path.abspath(named)
    parent, name = os.path.split(target)
    os.makedirs(parent, exist_ok=True)
    part = tempfile.mkdtemp(prefix=f".{name}.", suffix=".part", dir=parent)
    try:
        for i in range(0, len(wanted), FRAMES_PER_RUN):
            run = wanted[i : i + FRAMES_PER_RUN]
            pick = "select='" + "+".join(f"eq(n\\,{n})" for n in run) + "'"
            # deinterlaced before the pick, as the deinterlacer reads the frames either side
            vf = ",".join([*_deinterlace(fields), pick, _scale(height)])
            staged = os.path.join(part, f"run{i // FRAMES_PER_RUN}")
            os.mkdir(staged)
            media.ffmpeg(
                [
                    "-i", src, "-map", "0:v:0", "-vf", vf, "-fps_mode", "passthrough", "-frames:v", str(len(run)),
                    "-q:v", str(quality), "-start_number", "0", os.path.join(staged, "%06d.jpg"),
                ],
                progress=progress,
                timeout=timeout,
            )  # fmt: skip
            made = sorted(os.listdir(staged))
            if len(made) < len(run):
                raise media.MediaError(f"{os.fspath(src)} has no frame {run[len(made)]}: it ends before it")
            for k, n in enumerate(run):
                os.replace(os.path.join(staged, made[k]), os.path.join(part, f"{n:06d}.jpg"))
            os.rmdir(staged)
        shutil.rmtree(target, ignore_errors=True)
        os.replace(part, target)
    except BaseException:
        shutil.rmtree(part, ignore_errors=True)
        raise
    return [Frame(number=n, path=os.path.join(target, f"{n:06d}.jpg")) for n in wanted]


@dataclass
class Peaks:
    """The lowest and highest sample of each 1/`per_second` slice, -1 to 1; the last slice may be shorter."""

    per_second: int
    min: list[float] = field(default_factory=lambda: [])
    max: list[float] = field(default_factory=lambda: [])

    @property
    def seconds(self) -> Fraction:
        return Fraction(len(self.min), self.per_second)

    def to_json(self, digits: int = 4) -> dict[str, Any]:
        return {
            "per_second": self.per_second,
            "min": [round(x, digits) for x in self.min],
            "max": [round(x, digits) for x in self.max],
        }


def peaks(
    src: str | os.PathLike[str],
    *,
    per_second: int = 50,
    rate: int = 8000,
    progress: Callable[[float], None] | None = None,
    timeout: float | None = None,
) -> Peaks:
    """The peaks of `src`'s first audio stream, mixed to mono and resampled to `rate` Hz (a multiple of
    `per_second`). MediaError when there is no audio. `progress(seconds)` as it is read."""
    if per_second <= 0 or rate % per_second:
        raise ValueError(f"the sample rate ({rate} Hz) must be a whole number of slices of 1/{per_second} s")
    n = rate // per_second
    out = Peaks(per_second)
    held = bytearray()  # a slice's bytes split across two chunks
    read = 0

    def take(chunk: bytes) -> None:
        nonlocal read
        held.extend(chunk)
        whole = len(held) - len(held) % (n * 2)
        if whole:
            _reduce(bytes(held[:whole]), n, out)
            del held[:whole]
            read += whole // 2
            if progress is not None:
                progress(read / rate)

    media.ffmpeg(
        ["-i", src, "-map", "0:a:0", "-ac", "1", "-ar", str(rate), "-c:a", "pcm_s16le", "-f", "s16le", "-"],
        sink=take,
        timeout=timeout,
    )
    usable = len(held) - len(held) % 2
    if usable:
        _reduce(bytes(held[:usable]), n, out)
    return out


def _reduce(data: bytes, n: int, out: Peaks) -> None:
    samples = array.array("h")
    samples.frombytes(data)
    if sys.byteorder == "big":  # pragma: no cover - s16le on a big-endian machine
        samples.byteswap()
    for i in range(0, len(samples), n):
        s = samples[i : i + n]
        out.min.append(min(s) / 32768)
        out.max.append(max(s) / 32768)
