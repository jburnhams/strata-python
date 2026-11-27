"""An edit rendered to one file: stretches of source files cut on their frames and joined with no gap, by one ffmpeg run.
Beside `derive` (files made to show footage) on the same ffmpeg (`media.ffmpeg`, `MediaError`).

    cuts = [Cut("tape.mkv", Span.of("13/25", "12")), Cut("tape.mkv", Span.of(40, "86/2"), gain_db=-3)]
    made = render(cuts, "reel.mp4")              # Rendered: probe, frames, duration, the exact command, versions, sha256
    made.manifest()                              # the same as JSON: what an app stores beside the file (spec S8)

Choices that matter to an app:

- **Frame exact, from the source's own frames.** A cut `[start, end)` keeps the frames whose start lies in it
  (`mediatime.Span.frames`), no more and no fewer, so a cut found on a frame renders on that frame. Each cut is its own
  input, sought to half a frame before its first frame: ffmpeg's input seek decodes from the keyframe before and drops
  what comes before the seek point, so the first frame kept is the cut's first frame and a two-hour tape is not decoded
  from its start for a cut near its end. `trim` then keeps the cut's frame count. Constant frame rate is assumed (a
  tape, a camera file); a variable-rate phone file would need its timestamps mapped first.
- **Sound to the sample, never drifting.** Each cut's audio is resampled to `Output.audio_rate`, padded with silence
  where the file has less (or none), and trimmed to the samples between its edit-time start and end rounded once from
  the total, so the sound stays with the picture across any number of cuts. `gain_db` is applied per cut.
- **One look for every cut.** Interlaced footage is deinterlaced (bwdif, the field order `derive.interlacing` finds, as
  the proxy is) and the picture is the first file's display shape at the output height, square pixels; a file of
  another shape is fitted inside it with bars. The frame rate is the output's (`fps`, which only does anything when a
  source's rate differs).
- **Deterministic.** The same cuts, files, settings and ffmpeg build give the same bytes: bitexact flags, no metadata,
  and a fixed encoder thread count (x264's output depends on it). `Rendered` names the ffmpeg build, the libx264 and
  the full command, and the file's SHA-256, so an app can show what made a file and check that a render repeats.
- **`RECIPE`** is the version of this recipe; an app puts it in its render's parameters so a changed recipe is a new
  render.

The output is written atomically (`files.atomic_path`). The filter graph is passed on the command line; at about 300
characters a cut that stays under Windows' 32 767 until about a hundred cuts."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from strata_python import derive, files, media, mediatime

#: The version of the recipe: raise it when the output changes for the same arguments.
RECIPE = 1

# x264's output depends on its thread count, so it is fixed: the same file on a 4-core laptop and a 32-core server.
_X264_THREADS = 4


@dataclass(frozen=True)
class Cut:
    """A stretch `[span.start, span.end)` of a file, in its own time. `interlace` None finds it (`derive.interlacing`)."""

    path: str
    span: mediatime.Span
    gain_db: float = 0.0
    interlace: derive.Interlace | None = None


@dataclass(frozen=True)
class Output:
    """What the file is. `height` and `rate` None take the first cut's file's (never above it)."""

    height: int | None = None
    rate: mediatime.Rate | None = None
    crf: int = 18
    preset: str = "medium"
    audio_rate: int = 48000
    audio_bitrate: str = "192k"
    #: A keyframe every this many seconds, so the file seeks well in a browser.
    gop: Fraction = Fraction(2)

    def to_json(self) -> dict[str, Any]:
        return {
            "height": self.height,
            "rate": None if self.rate is None else str(self.rate),
            "crf": self.crf,
            "preset": self.preset,
            "audio_rate": self.audio_rate,
            "audio_bitrate": self.audio_bitrate,
            "gop": mediatime.text(self.gop),
        }


@dataclass(frozen=True)
class Rendered:
    """What `render` made: the file's probe, its frame count and exact length, how it was made and its SHA-256."""

    probe: media.Probe
    frames: int
    duration: Fraction
    rate: mediatime.Rate
    width: int
    height: int
    command: list[str]
    ffmpeg: str
    sha256: str
    bytes: int
    cuts: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    output: dict[str, Any] = field(default_factory=dict[str, Any])

    def manifest(self) -> dict[str, Any]:
        """What made the file, as JSON (spec S8's render manifest, less the app's own part: which edit and revision)."""
        return {
            "recipe": RECIPE,
            "ffmpeg": self.ffmpeg,
            "command": self.command,
            "output": self.output,
            "cuts": self.cuts,
            "file": {
                "sha256": self.sha256,
                "bytes": self.bytes,
                "frames": self.frames,
                "duration": mediatime.text(self.duration),
                "rate": str(self.rate),
                "width": self.width,
                "height": self.height,
            },
        }


def _seconds(t: Fraction) -> str:
    # ffmpeg reads decimal seconds; nine places is far inside a frame (and a sample at 48 kHz is 21 µs).
    return f"{float(t):.9f}"


def _display_aspect(v: media.Stream) -> Fraction:
    sar = str(v.raw.get("sample_aspect_ratio", "1:1"))
    num, _, den = sar.partition(":")
    pixel = Fraction(int(num), int(den)) if num.isdigit() and den.isdigit() and int(num) and int(den) else Fraction(1)
    return Fraction(v.width or 1, v.height or 1) * pixel


def _rate_of(info: media.Probe) -> mediatime.Rate:
    v = derive._video(info)  # pyright: ignore[reportPrivateUsage]
    for key in ("r_frame_rate", "avg_frame_rate"):
        text = str(v.raw.get(key, ""))
        if text and text not in ("0/0", "0"):
            return mediatime.Rate.parse(text)
    raise media.MediaError(f"{info.path} does not say its frame rate")


_VERSION = re.compile(rb"ffmpeg version (\S+)")
_X264 = re.compile(rb"x264 - core (\d+)[^\n]*")


def ffmpeg_version() -> str:
    """The ffmpeg build's version, as `ffmpeg -version` says it ("6.1.1-3ubuntu5")."""
    out = media.ffmpeg(["-version"], level="quiet")
    m = _VERSION.search(out)
    return m.group(1).decode() if m else out.split(b"\n", 1)[0].decode(errors="replace")


def render(
    cuts: Sequence[Cut],
    out: str | os.PathLike[str],
    *,
    output: Output = Output(),
    progress: Callable[[float], None] | None = None,
    timeout: float | None = None,
) -> Rendered:
    """Write `cuts`, one after another with no gap, to `out` (an MP4: H.264 at `output.crf`, AAC stereo), and say what was
    made and how. `progress(seconds)` as the output is written. ValueError for an empty edit or a cut with no frames."""
    if not cuts:
        raise ValueError("an edit with no cuts renders nothing")
    infos = {c.path: media.probe(c.path) for c in cuts}
    first = infos[cuts[0].path]
    rate = output.rate or _rate_of(first)
    v0 = derive._video(first)  # pyright: ignore[reportPrivateUsage]
    if not v0.height or not v0.width:
        raise ValueError(f"{cuts[0].path} does not say its picture size")
    height = min(output.height or v0.height, v0.height)
    height -= height % 2
    # The first file's display shape (its pixel aspect included) is the output's; any other file is fitted inside it.
    width = 2 * round(height * _display_aspect(v0) / 2)

    args: list[str] = []
    graph: list[str] = []
    labels: list[str] = []
    described: list[dict[str, Any]] = []
    fields: dict[str, derive.Interlace] = {}
    at = Fraction(0)
    sample = 0
    total_frames = 0
    for k, c in enumerate(cuts):
        info = infos[c.path]
        own = _rate_of(info)
        kept = c.span.frames(own)
        if len(kept) == 0:
            raise ValueError(f"a cut of {c.path} keeps no frame: [{c.span.start}, {c.span.end})")
        first_frame = own.time_of(kept.start)
        # Half a frame early: the seek keeps frames from there, so the frame before the cut is dropped and its first kept.
        seek = max(Fraction(0), first_frame - own.frame_s / 2)
        args += ["-ss", _seconds(seek), "-i", c.path]
        # A cut's own interlace wins; otherwise each file is measured once (it decodes up to 500 frames), not per cut.
        if c.interlace is not None:
            fi = c.interlace
        else:
            if c.path not in fields:
                fields[c.path] = derive.interlacing(c.path, info=info)
            fi = fields[c.path]
        length = Fraction(len(kept), 1) / own.fps
        frames_out = round(length * rate.fps)
        video = [
            *derive._deinterlace(fi),  # pyright: ignore[reportPrivateUsage]
            f"trim=end_frame={len(kept)}",
            f"setpts=N/({own.fps.numerator}/{own.fps.denominator})/TB",
            f"fps={rate.fps.numerator}/{rate.fps.denominator}",
            f"trim=end_frame={frames_out}",
            # Display shape first (square pixels), then fitted into the output's box, letterboxed if it differs.
            "scale=w=trunc(ih*dar/2)*2:h=ih,setsar=1",
            f"scale=w={width}:h={height}:force_original_aspect_ratio=decrease:force_divisible_by=2",
            f"pad=w={width}:h={height}:x=(ow-iw)/2:y=(oh-ih)/2,setsar=1",
            "format=yuv420p",
        ]
        graph.append(f"[{k}:v:0]{','.join(video)}[v{k}]")
        # The samples this cut owns in the edit, rounded once from the edit's start so no error adds up.
        end_sample = round((at + Fraction(frames_out) / rate.fps) * output.audio_rate)
        samples = end_sample - sample
        if info.audio is not None:
            # The seek keeps sound from the seek point, half a frame before the first frame kept: trimmed here, or the
            # sound would run that much early against the picture.
            preroll = round((first_frame - seek) * output.audio_rate)
            audio = [
                f"aresample={output.audio_rate}",
                "aformat=channel_layouts=stereo",
                *([f"atrim=start_sample={preroll}"] if preroll else []),
                "asetpts=N/SR/TB",
                *([f"volume={c.gain_db}dB"] if c.gain_db else []),
                "apad",
                f"atrim=end_sample={samples}",
            ]
            graph.append(f"[{k}:a:0]{','.join(audio)}[a{k}]")
        else:
            graph.append(f"anullsrc=r={output.audio_rate}:cl=stereo,atrim=end_sample={samples}[a{k}]")
        labels.append(f"[v{k}][a{k}]")
        described.append(
            {
                "path": c.path,
                "span": c.span.to_json(),
                "frames": [kept.start, kept.stop],
                "gain_db": c.gain_db,
                "interlace": fi,
                "at": mediatime.text(at),
            }
        )
        at += Fraction(frames_out) / rate.fps
        sample = end_sample
        total_frames += frames_out
    graph.append(f"{''.join(labels)}concat=n={len(cuts)}:v=1:a=1[v][a]")

    keyint = str(max(1, round(rate.fps * output.gop)))
    with files.atomic_path(out) as tmp:
        command = [
            *args,
            "-filter_complex", ";".join(graph),
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", output.preset, "-crf", str(output.crf),
            "-g", keyint, "-keyint_min", keyint, "-sc_threshold", "0", "-threads", str(_X264_THREADS),
            "-c:a", "aac", "-b:a", output.audio_bitrate, "-ar", str(output.audio_rate),
            "-map_metadata", "-1", "-map_chapters", "-1",
            "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact",
            "-movflags", "+faststart", "-f", "mp4", tmp,
        ]  # fmt: skip
        media.ffmpeg(command, progress=progress, timeout=timeout)
        with open(tmp, "rb") as f:
            head = f.read(1 << 20)
        x264 = _X264.search(head)
    probe = media.probe(out)
    shown = [os.fspath(out) if a == tmp else a for a in command]
    return Rendered(
        probe=probe,
        frames=total_frames,
        duration=at,
        rate=rate,
        width=width,
        height=height,
        command=shown,
        ffmpeg=ffmpeg_version() + (f" (x264 core {x264.group(1).decode()})" if x264 else ""),
        sha256=files.sha256_hex(out),
        bytes=os.path.getsize(out),
        cuts=described,
        output=output.to_json(),
    )
