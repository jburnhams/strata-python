"""Client for the strata-models service: run a model by role, or hold the GPU for your own kernels. Standard library
only. It replaces the `strata_models_client` package that apps installed from a path or a git URL; the names and
behaviour are the same, plus typed result shapes.

    from strata_python.clients import models as sm

    sm.faces("/x.jpg", app="bright-horizons")                    # the service's JSON, as before
    page = sm.OcrPage.from_json(sm.ocr("/p.png", app="strata-scan"))   # typed: page.lines[0].words[0].box
    with sm.gpu_lease("strata-stills", vram_gb=4): ...

The service URL is STRATA_MODELS_URL (default http://127.0.0.1:8366). Inputs are file paths: the service runs on the
same machine. strata-models tests this module against its real API in its own integration suite; this repo tests it
against a stand-in service over real HTTP.

The result shapes read what the service sends and keep all of it in `raw`, so a field the service adds later is
there before this module learns its name. A field the service leaves out (a face without `pose` when `fields` did not
ask for it) is None or empty, never an error.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
import warnings
from collections.abc import Generator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, cast

Json = dict[str, Any]
Priority = Literal["high", "normal", "low"]
POLL_S = 30  # one long-poll request waits this long at most
DEFAULT_URL = "http://127.0.0.1:8366"


class Unavailable(RuntimeError):
    """The service is not running (or not reachable)."""


class JobFailed(RuntimeError):
    """The service ran the job and it failed (or refused it); `.job` is the job row when the service sent one."""

    def __init__(self, msg: str, job: Json | None = None) -> None:
        super().__init__(msg)
        self.job = job


def url() -> str:
    return os.environ.get("STRATA_MODELS_URL", DEFAULT_URL).rstrip("/")


def _call(method: str, path: str, body: object = None, timeout: float = POLL_S + 15) -> Json:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(  # noqa: S310 - the URL is the service's, from STRATA_MODELS_URL
        url() + path, data=data, method=method, headers={"Content-Type": "application/json"} if data else {}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            return cast("Json", json.load(r))
    except urllib.error.HTTPError as e:
        try:
            d = cast("Json", json.load(e))
        except ValueError:
            d = {}
        if e.code in (400, 404, 422):
            raise JobFailed(str(d.get("detail") or e), d.get("job")) from None
        raise Unavailable(f"strata-models answered {e.code}: {d.get('detail') or e.reason}") from None
    except (urllib.error.URLError, OSError) as e:
        raise Unavailable(f"strata-models is not reachable at {url()}: {e}") from None


def available(timeout: float = 2.0) -> bool:
    """Is the service running?"""
    try:
        return bool(_call("GET", "/api/health", timeout=timeout).get("ok"))
    except (Unavailable, JobFailed):
        return False


def submit(
    role: str,
    params: Mapping[str, Any] | None,
    app: str,
    priority: Priority = "normal",
    model: str | None = None,
    options: Mapping[str, Any] | None = None,
) -> Json:
    """Queue a job and return its row at once (poll with `wait`). `options` changes the model's load options (a
    variant loaded apart: evals)."""
    body = {"role": role, "params": dict(params or {}), "app": app, "priority": priority, "model": model}
    return _call("POST", "/api/jobs", {**body, "options": dict(options) if options is not None else None})


def wait_job(job_id: int) -> Json:
    """The job's row once it has finished (done, failed or cancelled), with result, error and timings; never raises
    for a failed job."""
    while True:
        j = _call("GET", f"/api/jobs/{int(job_id)}?wait={POLL_S}")
        if j["status"] not in ("queued", "running"):
            return j


def wait(job_id: int, timeout: float | None = None) -> Json:
    """The job's result once it is done; raises JobFailed if it failed or was cancelled, TimeoutError after
    `timeout` seconds."""
    end = None if timeout is None else time.monotonic() + timeout
    while True:
        step = POLL_S if end is None else max(min(POLL_S, end - time.monotonic()), 0)
        j = _call("GET", f"/api/jobs/{int(job_id)}?wait={step:.1f}")
        if j["status"] == "done":
            return cast("Json", j["result"])
        if j["status"] in ("failed", "cancelled"):
            raise JobFailed(j.get("error") or f"job {job_id} was {j['status']}", j)
        if end is not None and time.monotonic() >= end:
            raise TimeoutError(f"job {job_id} is still {j['status']}")


def run(
    role: str,
    params: Mapping[str, Any] | None,
    app: str,
    priority: Priority = "normal",
    model: str | None = None,
    timeout: float | None = None,
) -> Json:
    """Run a model job and return its result (queued behind the GPU's other work; priority `high` goes ahead of
    `normal` (the default), which goes ahead of `low`)."""
    return wait(int(submit(role, params, app, priority, model)["id"]), timeout)


def faces(path: str | None, app: str, priority: Priority = "normal", model: str | None = None, **params: Any) -> Json:
    """Faces in one image (`Faces.from_json`): params max_side, det_thresh, fields. Several images: path=None and
    `paths=[...]`, giving {'images': [...]}, one `Faces` each."""
    if path is not None:
        params["path"] = os.path.abspath(path)
    if "paths" in params:
        params["paths"] = [os.path.abspath(p) for p in params["paths"]]
    return run("faces", params, app, priority=priority, model=model)


def transcribe(
    path: str,
    app: str,
    role: str = "transcribe",
    priority: Priority = "normal",
    model: str | None = None,
    **params: Any,
) -> Json:
    """Whisper on an audio or video file (`Transcript.from_json`): params language, task, word_timestamps,
    vad_filter, beam_size, initial_prompt, clip=(start, end)."""
    params["path"] = os.path.abspath(path)
    if "clip" in params:
        params["clip"] = list(params["clip"])
    return run(role, params, app, priority=priority, model=model)


def ocr(path: str, app: str, priority: Priority = "normal", model: str | None = None, **params: Any) -> Json:
    """Printed text from a page picture (role `ocr`, `OcrPage.from_json`): params lang ('eng', 'eng+fra'), psm
    (Tesseract's page segmentation), dpi, clean (steps that clean the page first, such as "flatten,sharpen"; boxes are
    then in the cleaned picture's pixels), pdf (a .pdf path to write as well: the picture with its text searchable)."""
    params["path"] = os.path.abspath(path)
    if params.get("pdf"):
        params["pdf"] = os.path.abspath(params["pdf"])
    return run("ocr", params, app, priority=priority, model=model)


def speak(text: str, app: str, priority: Priority = "normal", model: str | None = None, **params: Any) -> Json:
    """Speech from text (role `speak`, `Speech.from_json`): params voice (a preset name, or a reference recording's
    path for the cloning models), speed, instruct, language, seed. The WAV at `path` is the service's copy: copy it if
    it must last."""
    if params.get("voice") and os.path.exists(str(params["voice"])):
        params["voice"] = os.path.abspath(params["voice"])
    return run("speak", {**params, "text": text}, app, priority=priority, model=model)


def role_model(role: str) -> str | None:
    """The model that serves `role` now (from the service's registry), or None for an unknown role. Apps key their
    caches on it, so output made by an earlier model is made again when an eval changes the role's model."""
    roles = cast("list[Json]", _call("GET", "/api/registry").get("roles", []))
    return next((cast("str", r["model"]) for r in roles if r["key"] == role), None)


class Lease:
    """A held lease: `.id`, `.job` (the row when granted). Heartbeats run on a daemon thread until `release()`."""

    def __init__(self, job: Json, ttl_s: float) -> None:
        self.id: int = int(job["id"])
        self.job = job
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._beat, args=(max(ttl_s / 3, 1.0),), daemon=True)
        self._t.start()

    def _beat(self, every: float) -> None:
        while not self._stop.wait(every):
            with contextlib.suppress(Exception):
                _call("POST", f"/api/leases/{self.id}/heartbeat", timeout=10)

    def release(self) -> None:
        self._stop.set()
        with contextlib.suppress(Exception):
            _call("DELETE", f"/api/leases/{self.id}", timeout=10)


def acquire(
    app: str,
    vram_gb: float | None = None,
    ttl_s: float = 120,
    priority: Priority = "normal",
    note: str | None = None,
    timeout: float | None = None,
) -> Lease:
    """Ask for the GPU and wait until it is granted; returns a Lease (call `.release()`), TimeoutError after
    `timeout` (the request is withdrawn)."""
    body = {"app": app, "vram_gb": vram_gb, "ttl_s": ttl_s, "priority": priority, "note": note, "pid": os.getpid()}
    j = _call("POST", "/api/leases", body)
    end = None if timeout is None else time.monotonic() + timeout
    last_beat = time.monotonic()
    while j["status"] == "queued":
        if end is not None and time.monotonic() >= end:
            with contextlib.suppress(Exception):
                _call("DELETE", f"/api/leases/{j['id']}", timeout=10)
            raise TimeoutError(f"the GPU was not free within {timeout} s")
        if time.monotonic() - last_beat > ttl_s / 3:  # a queued request is kept alive too
            _call("POST", f"/api/leases/{j['id']}/heartbeat", timeout=10)
            last_beat = time.monotonic()
        step = min(POLL_S, ttl_s / 3) if end is None else max(min(POLL_S, ttl_s / 3, end - time.monotonic()), 0)
        j = _call("GET", f"/api/leases/{j['id']}?wait={step:.1f}")
    if j["status"] != "running":
        raise JobFailed(f"lease {j['id']} was {j['status']}", j)
    return Lease(j, ttl_s)


@contextlib.contextmanager
def gpu_lease(
    app: str,
    vram_gb: float | None = None,
    ttl_s: float = 120,
    priority: Priority = "normal",
    note: str | None = None,
    timeout: float | None = None,
    required: bool = False,
) -> Generator[Lease | None]:
    """`with gpu_lease('strata-stills', vram_gb=4):` 4 GB of the GPU are this app's for the block, and other work goes
    on in the rest (give the most this block will use); with vram_gb=None the whole GPU, and nothing else runs.
    Without the service: yields None and runs the block anyway, with a warning (or raises Unavailable when
    `required`)."""
    try:
        lease = acquire(app, vram_gb, ttl_s, priority, note, timeout)
    except Unavailable:
        if required:
            raise
        warnings.warn("strata-models is not running: using the GPU without a lease", RuntimeWarning, stacklevel=3)
        lease = None
    try:
        yield lease
    finally:
        if lease is not None:
            lease.release()


# --- typed results: what each role returns -------------------------------------------------------------------------


def _floats(v: object) -> list[float]:
    return [float(x) for x in cast("Sequence[Any]", v)] if isinstance(v, list) else []


def _box(v: object) -> tuple[int, int, int, int]:
    x, y, w, h = (int(a) for a in cast("Sequence[Any]", v))
    return (x, y, w, h)


def _opt_float(d: Json, key: str) -> float | None:
    v = d.get(key)
    return None if v is None else float(v)


@dataclass(frozen=True)
class OcrWord:
    text: str
    box: tuple[int, int, int, int]  # x, y, w, h in the (cleaned) picture's pixels
    conf: float  # 0-100

    @classmethod
    def from_json(cls, d: Json) -> OcrWord:
        return cls(str(d["text"]), _box(d["box"]), float(d.get("conf", 0)))


@dataclass(frozen=True)
class OcrLine:
    text: str
    box: tuple[int, int, int, int]
    conf: float
    block: int
    words: list[OcrWord]

    @classmethod
    def from_json(cls, d: Json) -> OcrLine:
        words = [OcrWord.from_json(w) for w in cast("list[Json]", d.get("words", []))]
        return cls(str(d["text"]), _box(d["box"]), float(d.get("conf", 0)), int(d.get("block", 0)), words)


@dataclass(frozen=True)
class OcrPage:
    """Role `ocr`: the page's text in reading order, its lines and words with boxes."""

    width: int
    height: int
    text: str
    lines: list[OcrLine]
    engine: str | None = None
    lang: str | None = None
    seconds: float | None = None
    pdf: str | None = None  # the searchable PDF, when `pdf=` asked for one
    raw: Json = field(default_factory=lambda: {}, repr=False, compare=False)

    @classmethod
    def from_json(cls, d: Json) -> OcrPage:
        lines = [OcrLine.from_json(ln) for ln in cast("list[Json]", d.get("lines", []))]
        return cls(
            int(d["width"]), int(d["height"]), str(d.get("text", "")), lines, d.get("engine"), d.get("lang"),
            _opt_float(d, "seconds"), d.get("pdf"), d,
        )  # fmt: skip


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    word: str
    probability: float | None = None


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str
    words: list[Word]
    avg_logprob: float | None = None
    no_speech_prob: float | None = None


@dataclass(frozen=True)
class Transcript:
    """Roles `transcribe` and `transcribe-fast`: segments in time order, with words when word_timestamps is on."""

    language: str | None
    duration: float | None
    segments: list[Segment]
    language_probability: float | None = None
    raw: Json = field(default_factory=lambda: {}, repr=False, compare=False)

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.segments).strip()

    @classmethod
    def from_json(cls, d: Json) -> Transcript:
        segments = [
            Segment(
                float(s["start"]),
                float(s["end"]),
                str(s.get("text", "")),
                [
                    Word(float(w["start"]), float(w["end"]), str(w["word"]), _opt_float(w, "probability"))
                    for w in cast("list[Json]", s.get("words", []))
                ],
                _opt_float(s, "avg_logprob"),
                _opt_float(s, "no_speech_prob"),
            )
            for s in cast("list[Json]", d.get("segments", []))
        ]
        return cls(d.get("language"), _opt_float(d, "duration"), segments, _opt_float(d, "language_probability"), d)


@dataclass(frozen=True)
class Face:
    """One face, in the original image's pixels. Each field is there only when `fields` asked for it."""

    bbox: list[float]  # x1, y1, x2, y2
    kps: list[list[float]]  # five landmarks, [x, y] each
    det_score: float | None
    embedding: list[float]  # normed, 512 for buffalo_l
    pose: list[float]  # pitch, yaw, roll

    @classmethod
    def from_json(cls, d: Json) -> Face:
        kps = [_floats(p) for p in cast("list[Any]", d.get("kps", []))]
        return cls(
            _floats(d.get("bbox")), kps, _opt_float(d, "det_score"), _floats(d.get("embedding")), _floats(d.get("pose"))
        )


@dataclass(frozen=True)
class Faces:
    """Role `faces` for one image (with `paths=`, the result's `images` are one each)."""

    width: int
    height: int
    faces: list[Face]
    raw: Json = field(default_factory=lambda: {}, repr=False, compare=False)

    @classmethod
    def from_json(cls, d: Json) -> Faces:
        return cls(
            int(d["width"]), int(d["height"]), [Face.from_json(f) for f in cast("list[Json]", d.get("faces", []))], d
        )

    @classmethod
    def many(cls, d: Json) -> list[Faces]:
        """The `images` of a several-image result, in the order of `paths`."""
        return [cls.from_json(i) for i in cast("list[Json]", d.get("images", []))]


@dataclass(frozen=True)
class Speech:
    """Role `speak`: a WAV on this machine (the service's copy) and how it was made."""

    path: str
    duration_s: float
    sample_rate: int
    engine: str | None = None
    voice: str | None = None
    seconds: float | None = None
    raw: Json = field(default_factory=lambda: {}, repr=False, compare=False)

    @classmethod
    def from_json(cls, d: Json) -> Speech:
        voice = d.get("voice")
        return cls(
            str(d["path"]), float(d["duration_s"]), int(d["sample_rate"]), d.get("engine"),
            None if voice is None else str(voice), _opt_float(d, "seconds"), d,
        )  # fmt: skip
