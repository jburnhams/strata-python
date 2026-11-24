"""A library of spoken clips: each fixed line an app says (a prompt, a piece of feedback) made once by strata-models'
`speak` role, kept on disk with what made it, a person's recording of a line kept over any made one, and served to
the web app, whose `speak` plays a clip and falls back to the browser's voice for a line with none.

    from strata_python.speech import ClipStore, add_clip_routes, build

    store = ClipStore(os.path.join(SETTINGS.home(), "voice"))
    built = build(store, lines, app="myapp", voice="bf_emma", params={"language": "en-gb", "seed": 0},
                  now=models.role_settings("speak"))                  # the CLI: `myapp voice build`
    add_clip_routes(app, store, "/api/v1/voice", recordable=lambda line: line in SOUNDS)

Browser voices differ by device and accent, and none can say some things (a phonics sound alone), so Flashtap built
this (`voice.py` `Store` and `build`, `server/app.py` `add_voice_routes`; issue #73) and any app on strata-models that
speaks would write it again. What stays in the app: which lines exist, how a sentence is cut into lines (the web app
must cut it the same way to find a clip by its text), and which lines are recorded by a person instead of made.

The store is a folder: `manifest.json` (line -> `Clip`: file, source, voice, model, settings hash, length, when) and
`clips/<sha256[:16]>.wav`, named by content so the browser may cache a clip forever (a remade clip is a new name).
Keep it out of git when it holds recordings: a recording is a person's voice.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from strata_python import files, oslib
from strata_python.clients import models
from strata_python.clients.models import Provenance, RoleSettings
from strata_python.contracts import ApiModel
from strata_python.wav import read_wav

Source = Literal["generated", "recorded"]
RECORDING = "recording"  # the voice of a recorded clip
MAX_RECORDING_S = 10.0  # a line, not a speech: longer is a recording left running
MAX_RECORDING_BYTES = 4_000_000  # 10 s of 16-bit mono at 192 kHz; refused while it streams in, before it is read
IMMUTABLE = "public, max-age=31536000, immutable"


class Clip(BaseModel):
    """One line's clip as the manifest keeps it."""

    file: str  # clips/<sha256[:16]>.wav, relative to the store
    source: Source
    voice: str  # the speak role's voice name, or "recording"
    model: str = ""  # the strata-models model that made it; empty for a recording
    settings_hash: str = ""  # that model's settings as it ran (Provenance.settings_hash): a change remakes the clip
    seconds: float
    made: str  # ISO 8601, UTC

    @property
    def name(self) -> str:
        """The served name (`<hash>.wav`)."""
        return Path(self.file).name


class Manifest(BaseModel):
    version: int = 1
    clips: dict[str, Clip] = {}


class ClipStore:
    """The clips of one app in `root`. Every edit of the manifest is a read, change and atomic write under a file lock,
    so `build` in one process and a recording through the server in another never lose each other's lines."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    def read(self) -> Manifest:
        try:
            return Manifest.model_validate_json(self.manifest_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return Manifest()

    def clip(self, line: str) -> Clip | None:
        return self.read().clips.get(line)

    def path_of(self, name: str) -> Path | None:
        """The file of a clip by its served name, or None for any name the manifest does not hold: a name from a
        request never reaches the file system, so `../` gets nowhere."""
        held = {c.name: c.file for c in self.read().clips.values()}
        return self.root / held[name] if name in held else None

    def urls(self, prefix: str) -> dict[str, str]:
        """Line -> the clip's URL under `add_clip_routes`' `prefix`, relative (no leading `/`) so the web app puts its
        base first (strata-react's `withBase`) behind a reverse proxy."""
        return {line: clip_url(prefix, c) for line, c in self.read().clips.items()}

    def put(
        self,
        line: str,
        wav: bytes,
        *,
        source: Source,
        voice: str,
        model: str = "",
        settings_hash: str = "",
        max_seconds: float | None = None,
    ) -> Clip:
        """Keep `wav` as the clip of `line` and return the clip the line has now. The WAV is checked first
        (`read_wav`: a `WavError`, so a 400, for one that is not a mono PCM WAV or is longer than `max_seconds`).

        A made clip never replaces a recording: that is decided under the lock, so a recording kept while `build`
        runs is not undone by it, and the recording is what is returned (compare its `source`)."""
        info = read_wav(wav, max_seconds=max_seconds)
        name = f"clips/{hashlib.sha256(wav).hexdigest()[:16]}.wav"
        target = self.root / name
        if not target.exists():  # the same bytes under the same name: two lines may share a file
            target.parent.mkdir(parents=True, exist_ok=True)
            files.write_bytes_atomic(target, wav)
        clip = Clip(
            file=name,
            source=source,
            voice=voice,
            model=model,
            settings_hash=settings_hash,
            seconds=round(info.seconds, 3),
            made=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        kept = clip

        def change(m: Manifest) -> None:
            nonlocal kept
            now = m.clips.get(line)
            if source == "generated" and now is not None and now.source == "recorded":
                kept = now
                return
            m.clips[line] = clip

        self._edit(change)
        return kept

    def remove(self, line: str) -> bool:
        """Forget the clip of `line` (its file stays: another line may use it, and a cached URL keeps working)."""
        removed = False

        def change(m: Manifest) -> None:
            nonlocal removed
            removed = m.clips.pop(line, None) is not None

        self._edit(change)
        return removed

    def _edit(self, change: Callable[[Manifest], None]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / ".lock").open("a+") as lock, oslib.file_lock(lock):
            m = self.read()
            change(m)
            files.write_text_atomic(self.manifest_path, m.model_dump_json(indent=1))


def clip_url(prefix: str, clip: Clip) -> str:
    return f"{prefix.strip('/')}/clips/{clip.name}"


# ---- making clips ---------------------------------------------------------------------------------------------------


class Built(BaseModel):
    """What `build` did with each line."""

    made: list[str] = []
    kept: list[str] = []  # already there in this voice, model and settings, or recorded
    skipped: list[str] = []  # lines the app said are not made (`per_line` gave None): a person records them
    failed: dict[str, str] = {}  # line -> why


class Speak(Protocol):
    """How `build` makes one line: `clients.models.run_with_provenance("speak", ...)` by default; a test or a script
    with its own engine passes another. Returns the job's result (its `path` is the WAV) and what made it."""

    def __call__(
        self, line: str, params: Mapping[str, Any], model: str | None
    ) -> tuple[Mapping[str, Any], Provenance]: ...


def speak_with_models(app: str) -> Speak:
    """`build`'s default: the line made by strata-models' `speak` role, filed under `app`."""

    def speak(line: str, params: Mapping[str, Any], model: str | None) -> tuple[Mapping[str, Any], Provenance]:
        return models.run_with_provenance("speak", {**params, "text": line}, app, model=model)

    return speak


def stale(clip: Clip, now: RoleSettings | None) -> bool:
    """Whether a made clip came from another model, or the same one under other settings, than the role has `now`. A
    hash only one side knows (a clip from before the service recorded it, or a model named by hand, whose settings the
    registry does not report) compares the model alone; without `now` nothing is stale."""
    if now is None:
        return False
    if clip.model != now.model:
        return True
    return bool(clip.settings_hash and now.settings_hash and clip.settings_hash != now.settings_hash)


def build(
    store: ClipStore,
    lines: Iterable[str],
    *,
    app: str,
    voice: str,
    params: Mapping[str, Any] | None = None,
    per_line: Callable[[str], Mapping[str, Any] | None] | None = None,
    model: str | None = None,
    force: bool = False,
    now: RoleSettings | None = None,
    speak: Speak | None = None,
    say: Callable[[str], None] | None = None,
) -> Built:
    """Make the missing clips of `lines` with the speak role, in `voice`, and keep them in `store`.

    `params` go with every line (`language`, `seed`: a fixed seed makes a remake sound the same); `per_line(line)`
    adds a line's own (Flashtap: `phonemes` for a sound), or returns None for a line not to make at all (a sound
    a person records), which is listed in `skipped`. A clip is remade when it is in another voice, from another model
    or other settings than `now` (`clients.models.role_settings("speak")`: pass it, or only the voice is compared), or
    with `force`; a recording never is. One line failing (a refused parameter, a missing file) is reported in `failed`
    and the rest are still made. `say` gets a line of progress per line."""
    out = Built()
    speak = speak or speak_with_models(app)
    have = store.read().clips
    for line in lines:
        old = have.get(line)
        if old is not None and old.source == "recorded":
            out.kept.append(line)
            continue
        extra: Mapping[str, Any] | None = {} if per_line is None else per_line(line)
        if extra is None:
            out.skipped.append(line)
            continue
        if old is not None and old.voice == voice and not force and not stale(old, now):
            out.kept.append(line)
            continue
        try:
            result, prov = speak(line, {**(params or {}), **extra, "voice": voice}, model)
            got = store.put(
                line,
                Path(str(result["path"])).read_bytes(),
                source="generated",
                voice=str(result.get("voice") or voice),
                model=prov.model or "",
                settings_hash=prov.settings_hash or "",
            )
        except Exception as e:  # one line's failure must not lose the others: it is reported, not swallowed
            out.failed[line] = str(e) or type(e).__name__
            _say(say, f"FAILED {line}: {out.failed[line]}")
            continue
        if got.source == "recorded":  # recorded while this line was being made: the recording stands
            out.kept.append(line)
            _say(say, f"kept  {line} (recorded meanwhile)")
        else:
            out.made.append(line)
            _say(say, f"made  {line}")
    return out


def _say(say: Callable[[str], None] | None, text: str) -> None:
    if say is not None:
        say(text)


# ---- the routes -----------------------------------------------------------------------------------------------------


class ClipsOut(ApiModel):
    """`GET {prefix}`: every line with a clip, and the clip's URL (relative: put it through `withBase`)."""

    clips: dict[str, str]


class ClipOut(ApiModel):
    """The clip a line has after a recording is kept."""

    line: str
    url: str
    source: Source
    seconds: float


def add_clip_routes(
    app: FastAPI,
    store: ClipStore,
    prefix: str,
    *,
    recordable: Callable[[str], bool],
    max_seconds: float = MAX_RECORDING_S,
    max_bytes: int = MAX_RECORDING_BYTES,
) -> None:
    """The routes a web app speaks from and records through:

      GET    {prefix}                      ClipsOut: line -> clip URL
      GET    {prefix}/clips/{name}         the WAV, cached for good (named by its content)
      PUT    {prefix}/recordings?line=...  a WAV body: a person's recording of `line`, kept over any made clip (ClipOut)
      DELETE {prefix}/recordings?line=...  forget the recording (204; 404 when the line has none)

    `recordable(line)` says which lines a person may record (400 for another); a recording is refused past
    `max_bytes` while it arrives and past `max_seconds` once its header is read. Errors are raised for
    `web.add_error_handlers` to answer, with the sentence as `detail`."""
    base = "/" + prefix.strip("/")

    def check(line: str) -> str:
        if not recordable(line):
            raise ValueError(f"{line!r} is not a line that is recorded here.")
        return line

    @app.get(base, response_model=ClipsOut)
    def get_clips() -> ClipsOut:  # pyright: ignore[reportUnusedFunction]
        return ClipsOut(clips=store.urls(prefix))

    @app.get(f"{base}/clips/{{name}}", response_class=FileResponse)
    def get_clip(name: str) -> FileResponse:  # pyright: ignore[reportUnusedFunction]
        path = store.path_of(name)
        if path is None or not path.is_file():
            raise HTTPException(404, f"No clip {name}.")
        return FileResponse(path, media_type="audio/wav", headers={"Cache-Control": IMMUTABLE})

    @app.put(f"{base}/recordings", response_model=ClipOut)
    async def put_recording(line: str, request: Request) -> ClipOut:  # pyright: ignore[reportUnusedFunction]
        check(line)
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > max_bytes:
                raise ValueError(f"A recording is at most {max_seconds:g} seconds.")
        c = store.put(line, bytes(body), source="recorded", voice=RECORDING, max_seconds=max_seconds)
        return ClipOut(line=line, url=clip_url(prefix, c), source=c.source, seconds=c.seconds)

    @app.delete(f"{base}/recordings", status_code=204)
    def delete_recording(line: str) -> Response:  # pyright: ignore[reportUnusedFunction]
        c = store.clip(check(line))
        if c is None or c.source != "recorded":
            raise HTTPException(404, f"No recording of {line}.")
        store.remove(line)
        return Response(status_code=204)
