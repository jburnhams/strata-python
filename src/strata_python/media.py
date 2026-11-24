"""ffprobe and ffmpeg, called one way: a typed probe, a duration, and ffmpeg runs that fail with the tool's own words.

strata-360 calls ffprobe in 16 places, listory has three separate wrappers (`process/probe.py`, `store/filegroups.py`,
`details.py`) and strata-models two (`workers/video.py`, `workers/audio.py`). They differ in the `-v` level, the
timeout and what a failure is (None, a `ToolError`, a `RuntimeError`, a `ParamError`), and each picks a few fields out
of the JSON by hand. Here:

    info = probe(path)                       # Probe: duration, format, size, bit_rate, tags, streams, chapters, raw
    info.audio.sample_rate; info.video.fps; info.chapters[0].title; info.warnings   # ffprobe's own complaints
    duration(path)                           # seconds, float
    ffmpeg(["-i", src, "-ac", "1", out])     # -nostdin -hide_banner -v error -y; MediaError with stderr's tail
    ffmpeg([...], progress=lambda s: ...)    # seconds of output written so far, from -progress
    ffmpeg([..., "-f", "s16le", "-"], sink=f) # stdout handed to f in chunks as it is written, never held whole
    available()                              # {"ffmpeg": True, "ffprobe": False}

`MediaError` is a `RuntimeError` for a missing tool, a file ffprobe cannot read, a timeout or a failed ffmpeg run. Its
message ends with the last lines ffmpeg wrote, because that is the only place the reason is.

Durations: the container's, else the longest stream's (some MP3s and raw streams have no container duration). A wrong
VBR header still gives a wrong number; `info.warnings` carries ffprobe's "estimating duration from bitrate" so an app
that cares (listory) can decode and count instead.

The tools are found from the app's own setting when it has one (`configure(ffmpeg=lambda: settings.get("ffmpeg"))`,
once at start), else `STRATA_FFMPEG`/`STRATA_FFPROBE` (a build with more codecs), else the PATH. The setting is read on
every run, so `app set ffmpeg /path` takes effect without a restart, and `available()` asks the same way, so a doctor
row agrees with what runs. Standard library only; `run` is injectable, but tests should prefer the real
tools on a tiny generated file."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import IO, Any, cast

from strata_python import oslib

Json = dict[str, Any]
ENV = {"ffmpeg": "STRATA_FFMPEG", "ffprobe": "STRATA_FFPROBE"}
Path = str | os.PathLike[str]
Where = Path | Callable[[], Path | None] | None
_UNCHANGED: Any = object()
# What the app said, per tool: a path, or a function read on every lookup (a setting the owner can change while the
# server runs). A function that answers None or "" means "not set here", so the environment and the PATH still apply.
_configured: dict[str, Callable[[], Path | None]] = {}


class MediaError(RuntimeError):
    """ffmpeg or ffprobe is missing, could not read the file, ran out of time or failed."""


def configure(*, ffmpeg: Where = _UNCHANGED, ffprobe: Where = _UNCHANGED) -> None:
    """Say where the app's own setting puts the tools, once at start: a path, or a function read on every run
    (`configure(ffmpeg=lambda: settings.get("ffmpeg"))`). A tool not named keeps what it had; `None` forgets it. An
    app with its own setting for the program uses this so the setting, not a second environment variable, decides
    what runs. Tests that call it undo it with `configure(ffmpeg=None, ffprobe=None)`."""
    for name, where in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)):
        if where is _UNCHANGED:
            continue
        if where is None:
            _configured.pop(name, None)
        elif isinstance(where, (str, os.PathLike)):
            _configured[name] = _fixed(cast("Path", where))
        else:
            _configured[name] = where


def _fixed(path: Path) -> Callable[[], Path]:
    return lambda: path


def _from_app(name: str) -> str | None:
    where = _configured.get(name)
    value = where() if where else None
    return os.fspath(value) if value else None


def tool(name: str) -> str:
    """The path of `ffmpeg` or `ffprobe`: the app's `configure`d setting when it gives one, else `STRATA_FFMPEG` /
    `STRATA_FFPROBE` when set, else the PATH. A setting that names no runnable program is a MediaError that names it,
    rather than a quiet fall back to another ffmpeg than the one the owner chose."""
    chosen = _from_app(name)
    if chosen:
        found = shutil.which(chosen)  # a bare name is looked up on the PATH; a path must be an executable file
        if not found:
            raise MediaError(f"{name} is set to {chosen}, which is not a program that can be run: change the setting")
        return found
    found = os.environ.get(ENV.get(name, "")) or shutil.which(name)
    if not found:
        raise MediaError(f"{name} is not installed (or not on the PATH): install ffmpeg, or set {ENV.get(name, name)}")
    return found


def available() -> dict[str, bool]:
    """Which of the tools can be found, for a doctor row; the same lookup `probe` and `ffmpeg` use."""
    out: dict[str, bool] = {}
    for name in ENV:
        try:
            tool(name)
            out[name] = True
        except MediaError:
            out[name] = False
    return out


def _tail(text: str | bytes | None, n: int = 600) -> str:
    if isinstance(text, bytes):
        text = text.decode(errors="replace")
    return (text or "").strip()[-n:]


def _num(v: object, cast_to: Callable[[Any], Any] = float) -> Any:
    try:
        return cast_to(v)
    except (TypeError, ValueError):
        return None


def _rate(v: object) -> float | None:
    """ffprobe's "30000/1001" (or "25") as a number; None for "0/0"."""
    num, _, den = str(v or "").partition("/")
    n, d = _num(num), _num(den or 1)
    return n / d if n and d else None


@dataclass
class Stream:
    index: int
    kind: str  # video | audio | subtitle | data | attachment
    codec: str
    duration: float | None
    bit_rate: int | None
    width: int | None = None
    height: int | None = None
    fps: float | None = None  # avg_frame_rate, else r_frame_rate
    frames: int | None = None
    pix_fmt: str | None = None
    sample_rate: int | None = None
    channels: int | None = None
    channel_layout: str | None = None
    tags: dict[str, str] = field(default_factory=lambda: {})  # keys lower-cased
    raw: Json = field(default_factory=lambda: {})

    @classmethod
    def from_json(cls, s: Json) -> Stream:
        return cls(
            index=int(s.get("index", 0)),
            kind=str(s.get("codec_type", "")),
            codec=str(s.get("codec_name", "")),
            duration=_num(s.get("duration")),
            bit_rate=_num(s.get("bit_rate"), int),
            width=_num(s.get("width"), int),
            height=_num(s.get("height"), int),
            fps=_rate(s.get("avg_frame_rate")) or _rate(s.get("r_frame_rate")),
            frames=_num(s.get("nb_frames"), int),
            pix_fmt=s.get("pix_fmt"),
            sample_rate=_num(s.get("sample_rate"), int),
            channels=_num(s.get("channels"), int),
            channel_layout=s.get("channel_layout"),
            tags={str(k).lower(): str(v) for k, v in cast("Json", s.get("tags") or {}).items()},
            raw=s,
        )


@dataclass
class Chapter:
    start: float
    end: float
    title: str


@dataclass
class Probe:
    path: str
    duration: float | None
    format: str  # format_name, e.g. "mov,mp4,m4a,3gp,3g2,mj2"
    size: int | None
    bit_rate: int | None
    tags: dict[str, str]  # container tags, keys lower-cased
    streams: list[Stream]
    chapters: list[Chapter]
    warnings: str  # what ffprobe wrote on stderr at -v warning
    raw: Json  # the whole ffprobe answer

    def first(self, kind: str) -> Stream | None:
        return next((s for s in self.streams if s.kind == kind), None)

    @property
    def video(self) -> Stream | None:
        return self.first("video")

    @property
    def audio(self) -> Stream | None:
        return self.first("audio")

    @classmethod
    def from_json(cls, path: str, d: Json, warnings: str = "") -> Probe:
        fmt = cast("Json", d.get("format") or {})
        streams = [Stream.from_json(s) for s in cast("list[Json]", d.get("streams") or [])]
        dur = _num(fmt.get("duration")) or max((s.duration or 0.0 for s in streams), default=0.0) or None
        chapters = [
            Chapter(
                float(c.get("start_time", 0)),
                float(c.get("end_time", 0)),
                str(cast("Json", c.get("tags") or {}).get("title", "")),
            )
            for c in cast("list[Json]", d.get("chapters") or [])
        ]
        return cls(
            path=path,
            duration=dur,
            format=str(fmt.get("format_name", "")),
            size=_num(fmt.get("size"), int),
            bit_rate=_num(fmt.get("bit_rate"), int),
            tags={str(k).lower(): str(v) for k, v in cast("Json", fmt.get("tags") or {}).items()},
            streams=streams,
            chapters=chapters,
            warnings=warnings.strip(),
            raw=d,
        )


Run = Callable[..., "subprocess.CompletedProcess[Any]"]


def probe(path: str | os.PathLike[str], *, timeout: float = 120, run: Run = subprocess.run) -> Probe:
    """Everything ffprobe says about a file: format, every stream, chapters and tags. MediaError when it cannot."""
    p = os.fspath(path)
    cmd = [
        tool("ffprobe"),
        "-v",
        "warning",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        "-show_chapters",
        p,
    ]
    try:
        r = run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise MediaError(f"ffprobe took longer than {timeout:g} s on {p}") from None
    except OSError as e:
        raise MediaError(f"ffprobe could not start: {e}") from None
    if r.returncode or not (r.stdout or "").strip():
        raise MediaError(f"ffprobe cannot read {p}: {_tail(r.stderr) or f'exit {r.returncode}'}")
    try:
        d = json.loads(r.stdout)
    except ValueError:
        raise MediaError(f"ffprobe gave no JSON for {p}: {_tail(r.stdout, 200)}") from None
    return Probe.from_json(p, d, r.stderr or "")


def duration(path: str | os.PathLike[str], *, timeout: float = 120, run: Run = subprocess.run) -> float:
    """The file's length in seconds; MediaError when ffprobe cannot tell."""
    d = probe(path, timeout=timeout, run=run).duration
    if d is None:
        raise MediaError(f"ffprobe found no duration in {os.fspath(path)}")
    return d


def ffmpeg(
    args: Sequence[str | os.PathLike[str]],
    *,
    input: bytes | None = None,
    timeout: float | None = None,
    progress: Callable[[float], None] | None = None,
    sink: Callable[[bytes], None] | None = None,
    level: str = "error",
    cwd: str | None = None,
) -> bytes:
    """Run `ffmpeg -nostdin -hide_banner -v <level> -y <args>` and return its stdout (for `-f ... -` output).
    `progress(seconds)` is called as output is written (from `-progress`); then stdout is not returned. `sink(chunk)`
    is given stdout as it arrives instead (decoded audio of a two-hour tape is too big to hold); not with `progress`,
    which needs stdout for itself. MediaError with the tail of stderr when it fails or passes `timeout`."""
    if progress is not None and sink is not None:
        raise ValueError("ffmpeg's stdout carries either -progress or the output: pass progress or sink, not both")
    exe = tool("ffmpeg")
    base = [exe, "-hide_banner", "-v", level, "-y"]
    if input is None:
        base.insert(1, "-nostdin")
    cmd = [*base, *(os.fspath(a) for a in args)]
    if sink is not None:
        return _streamed(cmd, sink, timeout, cwd, input)
    if progress is None:
        proc = subprocess.Popen(  # noqa: S603 - our own ffmpeg command
            cmd,
            stdin=subprocess.DEVNULL if input is None else subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
        )
        try:
            out, err = proc.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill(proc)
            raise MediaError(f"ffmpeg took longer than {timeout:g} s") from None
        if proc.returncode:
            raise MediaError(f"ffmpeg failed (exit {proc.returncode}): {_tail(err)}")
        return out
    return _with_progress([*base, "-progress", "pipe:1", "-nostats", *cmd[len(base) :]], progress, timeout, cwd, input)


def _kill(proc: subprocess.Popen[bytes], reap: bool = True) -> None:
    """Stop ffmpeg and anything it started. On Windows a package manager's `ffmpeg.exe` is often a shim that starts the
    real one; killing only the shim leaves the real one holding the pipes, and the read waits for ever."""
    oslib.kill_tree(proc.pid, 9)
    with contextlib.suppress(OSError):
        proc.kill()
    if reap:  # the progress reader's threads read the pipes themselves
        with contextlib.suppress(subprocess.TimeoutExpired, OSError, ValueError):
            proc.communicate(timeout=10)


def _with_progress(
    cmd: list[str], progress: Callable[[float], None], timeout: float | None, cwd: str | None, data: bytes | None
) -> bytes:
    """Read `-progress pipe:1` lines (`out_time_us=...`) and report seconds."""

    def lines(out: IO[bytes]) -> None:
        for raw in out:
            key, _, value = raw.decode(errors="replace").strip().partition("=")
            if key in ("out_time_us", "out_time_ms"):  # both are microseconds (out_time_ms is misnamed by ffmpeg)
                us = _num(value, int)
                if us is not None and us >= 0:
                    progress(us / 1e6)

    return _piped(cmd, lines, timeout, cwd, data)


def _streamed(
    cmd: list[str], sink: Callable[[bytes], None], timeout: float | None, cwd: str | None, data: bytes | None
) -> bytes:
    def chunks(out: IO[bytes]) -> None:
        while chunk := out.read(1 << 16):
            sink(chunk)

    return _piped(cmd, chunks, timeout, cwd, data)


def _piped(
    cmd: list[str], read: Callable[[IO[bytes]], None], timeout: float | None, cwd: str | None, data: bytes | None
) -> bytes:
    """Run ffmpeg with `read` consuming its stdout as it is written; stderr is drained on a thread so a chatty ffmpeg
    cannot fill its pipe and stall. A `read` that raises stops ffmpeg and the error is raised as it is."""
    proc = subprocess.Popen(  # noqa: S603 - our own ffmpeg command
        cmd,
        stdin=subprocess.DEVNULL if data is None else subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
    )  # bytes, so the input can be written as it is
    out, errs = proc.stdout, proc.stderr
    if out is None or errs is None:  # pragma: no cover - both are pipes above
        raise MediaError("ffmpeg's output pipes did not open")
    err: list[str] = []
    drain = threading.Thread(target=lambda: err.append(errs.read().decode(errors="replace")), daemon=True)
    drain.start()
    if data is not None and proc.stdin is not None:  # fed on a thread: ffmpeg reads input while it writes progress
        feed_pipe = proc.stdin

        def feed() -> None:
            with contextlib.suppress(BrokenPipeError, OSError, ValueError):
                feed_pipe.write(data)
            with contextlib.suppress(OSError):
                feed_pipe.close()

        threading.Thread(target=feed, daemon=True).start()
    fired = threading.Event()

    def kill() -> None:
        fired.set()
        _kill(proc, reap=False)

    timer = threading.Timer(timeout, kill) if timeout else None
    if timer:
        timer.start()
    try:
        try:
            read(out)
        except BaseException:
            _kill(proc, reap=False)
            proc.wait()
            raise
        code = proc.wait()
    finally:
        if timer:
            timer.cancel()
        drain.join(5)
    if fired.is_set():
        raise MediaError(f"ffmpeg took longer than {timeout:g} s")
    if code:
        raise MediaError(f"ffmpeg failed (exit {code}): {_tail(''.join(err))}")
    return b""
