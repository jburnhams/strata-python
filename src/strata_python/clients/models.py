"""Client for the strata-models service: run a model by role, or hold the GPU for your own kernels. Standard library
only. It replaces the `strata_models_client` package that apps installed from a path or a git URL; the names and
behaviour are the same, plus typed result shapes.

    from strata_python.clients import models as sm

    sm.faces("/x.jpg", app="bright-horizons")                    # the service's JSON, as before
    page = sm.OcrPage.from_json(sm.ocr("/p.png", app="strata-scan"))   # typed: page.lines[0].words[0].box
    with sm.gpu_lease("strata-stills", vram_gb=4): ...

One shortcut per role the service runs (`ROLES`), each with its result shape:

    faces            Faces            face_kind        FaceKinds        ocr              OcrPage
    transcribe(-fast) Transcript      speak            Speech           denoise_audio    CleanAudio
    audio_quality    AudioQuality     generate_image   GeneratedImage   embed            Embeddings
    enhance_video    EnhancedVideo    shot_boundaries  Shots            speaker_embeddings SpeakerEmbeddings
    ask (vision-language, text, judge-text, judge-vision, judge-audio)  Answer

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

from strata_python import client

Json = dict[str, Any]
Priority = Literal["high", "normal", "low"]
POLL_S = 30  # one long-poll request waits this long at most
DEFAULT_URL = "http://127.0.0.1:8366"


class Unavailable(client.Unavailable):
    """The service is not running (or not reachable). A `strata_python.client.Unavailable`, so an app's
    `web.add_error_handlers` answers it as 503 like any other service outage."""


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


def request(method: str, path: str, body: object = None, timeout: float = POLL_S + 15) -> Json:
    """One call to any of the service's endpoints (`clients.faces` uses it for the face store), with this module's
    errors: `Unavailable` when it is down, `JobFailed` for a 400, 404 or 422 with the service's `detail`."""
    return _call(method, path, body, timeout)


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
    """Queue one job and return its row at once (poll with `wait`). `options` changes the model's load options (a
    variant loaded apart: evals). A `high` job (someone is waiting on it) goes to the service's single-job endpoint;
    normal and low work is queued in bulk, as a call of one: the service takes nothing else. An app with many jobs
    should call `submit_many` instead, since only a bulk call of 20 or more gets the batch price."""
    opts = dict(options) if options is not None else None
    if priority == "high":
        return _call(
            "POST",
            "/api/jobs",
            {"role": role, "params": dict(params or {}), "app": app, "model": model, "options": opts},
        )
    (job_id,) = submit_many(role, [params or {}], app, priority, model, options)
    return _call("GET", f"/api/jobs/{job_id}")


def submit_many(
    role: str,
    items: Sequence[Mapping[str, Any] | None],
    app: str,
    priority: Priority = "normal",
    model: str | None = None,
    options: Mapping[str, Any] | None = None,
    batch: bool | None = None,
) -> list[int]:
    """Queue one job per params in `items` in a single call (POST /api/jobs/bulk); returns their ids, in order (wait on
    each with `wait`). On a Claude model a call of 20 or more goes through its batch API, half the price and answered
    within 24 hours, so wait accordingly; `batch=True` batches however few, `batch=False` never. Nothing is queued
    when the role or model is refused."""
    body = {
        "role": role, "items": [dict(p or {}) for p in items], "app": app, "priority": priority, "model": model,
        "options": dict(options) if options is not None else None, "batch": batch,
    }  # fmt: skip
    return [int(i) for i in cast("list[Any]", _call("POST", "/api/jobs/bulk", body)["ids"])]


def run_many(
    role: str,
    items: Sequence[Mapping[str, Any] | None],
    app: str,
    priority: Priority = "normal",
    model: str | None = None,
    options: Mapping[str, Any] | None = None,
    batch: bool | None = None,
    timeout: float | None = None,
) -> list[Json | JobFailed]:
    """`submit_many`, then each job's result in order; a job that failed gives its `JobFailed` in its place, so one
    bad item does not lose the rest. `timeout` is per job (a batch can take up to 24 hours)."""
    out: list[Json | JobFailed] = []
    for job_id in submit_many(role, items, app, priority, model, options, batch):
        try:
            out.append(wait(job_id, timeout))
        except JobFailed as e:
            out.append(e)
    return out


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


@dataclass(frozen=True)
class Provenance:
    """What a run job's output was made with, from its row: the model key, its load options as the job ran (the
    service's models.toml options merged with the job's own) and `settings_hash`, which changes when the model's
    runtime, file or options do even under the same key. Key a cache on (model, settings_hash). The options and hash
    are None on rows from a service older than the one that records them."""

    model: str | None
    options: Json | None
    settings_hash: str | None
    job_id: int

    @classmethod
    def from_job(cls, job: Json) -> Provenance:
        options = job.get("options")
        return cls(
            _opt_str(job, "model"), cast("Json", options) if isinstance(options, dict) else None,
            _opt_str(job, "settings_hash"), int(job["id"]),
        )  # fmt: skip


def run_with_provenance(
    role: str,
    params: Mapping[str, Any] | None,
    app: str,
    priority: Priority = "normal",
    model: str | None = None,
    timeout: float | None = None,
) -> tuple[Json, Provenance]:
    """`run`, plus what the result was made with (`Provenance`): for an app that stores each result with its model
    and settings, and keys its cache on them."""
    job_id = int(submit(role, params, app, priority, model)["id"])
    result = wait(job_id, timeout)
    return result, Provenance.from_job(_call("GET", f"/api/jobs/{job_id}"))


ROLES = (
    "faces",
    "face-kind",
    "transcribe",
    "transcribe-fast",
    "ocr",
    "speak",
    "denoise-audio",
    "audio-quality",
    "speaker-embedding",
    "image-generate",
    "image-embed",
    "video-enhance",
    "shot-boundaries",
    "vision-language",
    "text",
    "judge-text",
    "judge-vision",
    "judge-audio",
)
"""The roles the service runs (each has a shortcut here; `ask` covers the language-model ones). `models.toml` also
lists sound-events, detect-people, upscale, denoise and nsfw, but no runtime serves them yet: apps
still load those models themselves. `run(role, params, app)` reaches any role, including one added after this list."""


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


def _abs(params: dict[str, Any], *keys: str) -> None:
    """Make the file paths under `keys` absolute (one path or a list): the service resolves them from its own folder."""
    for k in keys:
        v = params.get(k)
        if isinstance(v, str) and v:
            params[k] = os.path.abspath(v)
        elif isinstance(v, list | tuple):
            params[k] = [os.path.abspath(str(x)) for x in cast("Sequence[Any]", v)]


ASK_ROLES = ("vision-language", "text", "judge-text", "judge-vision", "judge-audio")


def ask(
    prompt: str | None,
    app: str,
    role: str = "vision-language",
    priority: Priority = "normal",
    model: str | None = None,
    **params: Any,
) -> Json:
    """A language model (`Answer.from_json`), local or cloud, by role: `vision-language` (questions about pictures),
    `text` (text in, text out), and the evals' judges `judge-text`, `judge-vision`, `judge-audio`. params: image /
    images, audio / audios (paths), preset + context (a prompt kept in the service; then `prompt` may be None),
    system or system_preset, schema (a JSON schema the answer must follow; its parsed answer is `json`), max_tokens,
    temperature, seed, think, effort (cloud models)."""
    if prompt is not None:
        params["prompt"] = prompt
    _abs(params, "image", "images", "audio", "audios")
    return run(role, params, app, priority=priority, model=model)


def generate_image(
    prompt: str, app: str, priority: Priority = "normal", model: str | None = None, **params: Any
) -> Json:
    """A picture from a prompt (role `image-generate`, `GeneratedImage.from_json`): params negative_prompt, width,
    height (multiples of 16, default 1024), steps, cfg_scale, sampler, seed (random unless given; always returned),
    ref_images (paths to edit from or refer to)."""
    params["prompt"] = prompt
    _abs(params, "ref_images")
    return run("image-generate", params, app, priority=priority, model=model)


def embed(
    app: str,
    paths: Sequence[str] = (),
    texts: Sequence[str] = (),
    priority: Priority = "normal",
    model: str | None = None,
    **params: Any,
) -> Json:
    """CLIP vectors, L2-normalised, for pictures and prompts (role `image-embed`, `Embeddings.from_json`): a dot
    product is a cosine similarity. params: array ({path, key?, bgr?}: images in an .npy/.npz), resize ('crop' or
    'square'), out (an .npz to write instead of answering with the numbers, for thousands of pictures)."""
    if paths:
        params["paths"] = list(paths)
    if texts:
        params["texts"] = list(texts)
    _abs(params, "paths", "out")
    if isinstance(params.get("array"), dict):
        params["array"] = {**params["array"], "path": os.path.abspath(params["array"]["path"])}
    return run("image-embed", params, app, priority=priority, model=model)


def face_kind(
    path: str | None,
    app: str,
    boxes: Sequence[Sequence[float]] = (),
    priority: Priority = "normal",
    model: str | None = None,
    **params: Any,
) -> Json:
    """Whether each face is a real person or a statue, picture, toy or animal (role `face-kind`,
    `FaceKinds.from_json`): an image `path` and the `boxes` the faces role gave ([x1, y1, x2, y2]), or path=None and
    array={path, key?, bgr?} of face crops already cut."""
    if path is not None:
        params["path"] = os.path.abspath(path)
        params["boxes"] = [list(b) for b in boxes]
    if isinstance(params.get("array"), dict):
        params["array"] = {**params["array"], "path": os.path.abspath(params["array"]["path"])}
    return run("face-kind", params, app, priority=priority, model=model)


def denoise_audio(path: str, app: str, priority: Priority = "normal", model: str | None = None, **params: Any) -> Json:
    """Noise out of recorded sound, speech kept clear (role `denoise-audio`, `CleanAudio.from_json`): params clip
    (start, end), highpass (Hz), atten_lim_db, finish ("strata360" or "none"), lufs (None keeps the level), and the
    engine's own. The WAV at `path` is the service's copy."""
    params["path"] = os.path.abspath(path)
    if "clip" in params:
        params["clip"] = list(params["clip"])
    return run("denoise-audio", params, app, priority=priority, model=model)


def audio_quality(
    path: str,
    app: str,
    reference: str | None = None,
    metrics: Sequence[str] | None = None,
    priority: Priority = "normal",
    model: str | None = None,
) -> Json:
    """How good a speech recording sounds, measured (role `audio-quality`, `AudioQuality.from_json`): DNSMOS and
    SQUIM without a reference, si_sdr, pesq and estoi against a clean `reference`. `metrics` picks among "dnsmos",
    "squim" and "intrusive" (default all that apply)."""
    params: dict[str, Any] = {"path": os.path.abspath(path)}
    if reference is not None:
        params["reference"] = os.path.abspath(reference)
    if metrics is not None:
        params["metrics"] = list(metrics)
    return run("audio-quality", params, app, priority=priority, model=model)


def speaker_embeddings(
    items: Sequence[tuple[str, tuple[float, float] | None]],
    app: str,
    priority: Priority = "normal",
    model: str | None = None,
) -> Json:
    """One voice vector per stretch of speech, to tell speakers apart (role `speaker-embedding`,
    `SpeakerEmbeddings.from_json`): `items` are (path, (start, end) seconds or None for the whole file), answered in
    that order. A file named by several items is decoded once, so give every sentence of one recording in one call."""
    asked: list[Json] = []
    for path, clip in items:
        item: Json = {"path": os.path.abspath(path)}
        if clip is not None:
            item["clip"] = [float(clip[0]), float(clip[1])]
        asked.append(item)
    return run("speaker-embedding", {"items": asked}, app, priority=priority, model=model)


def enhance_video(path: str, app: str, priority: Priority = "normal", model: str | None = None, **params: Any) -> Json:
    """Old video made better: deinterlaced, upscaled and restored into a playable MP4 (role `video-enhance`,
    `EnhancedVideo.from_json`): params deinterlace, field_order, crop [x, y, w, h], prep_size and size [w, h],
    regions [{id, label, box}], stills, crf, seed, cleanup, quality. Long clips take minutes: give `timeout` to
    `run` instead when the app must not wait that long."""
    params["path"] = os.path.abspath(path)
    return run("video-enhance", params, app, priority=priority, model=model)


def shot_boundaries(
    path: str, app: str, priority: Priority = "normal", model: str | None = None, **params: Any
) -> Json:
    """Where a video cuts from one shot to the next, and its black stretches (role `shot-boundaries`,
    `Shots.from_json`): params clip [start, end] seconds, threshold, min_shot_s, width, deinterlace, black (default
    true), black_min_s, black_pixel, scores (every frame's score too: a long list). A tape takes minutes on the CPU:
    give `timeout` to `run` instead when the app must not wait that long."""
    params["path"] = os.path.abspath(path)
    return run("shot-boundaries", params, app, priority=priority, model=model)


def role_model(role: str) -> str | None:
    """The model that serves `role` now (from the service's registry), or None for an unknown role. Apps key their
    caches on it, so output made by an earlier model is made again when an eval changes the role's model."""
    roles = cast("list[Json]", _call("GET", "/api/registry").get("roles", []))
    return next((cast("str", r["model"]) for r in roles if r["key"] == role), None)


@dataclass(frozen=True)
class RoleSettings:
    """The model that serves a role now and its `settings_hash` (see `Provenance`); the hash is None from a service
    older than the one that reports it."""

    model: str
    settings_hash: str | None


def role_settings(role: str) -> RoleSettings | None:
    """`role_model` with the settings hash: a cache keyed on both is made again when the role's model changes, and
    also when that model's options change under the same key (a threshold in the service's models.toml). None for an
    unknown role."""
    roles = cast("list[Json]", _call("GET", "/api/registry").get("roles", []))
    r = next((r for r in roles if r["key"] == role), None)
    return None if r is None else RoleSettings(str(r["model"]), _opt_str(r, "settings_hash"))


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


def _opt_str(d: Json, key: str) -> str | None:
    v = d.get(key)
    return None if v is None else str(v)


def _opt_int(d: Json, key: str) -> int | None:
    v = d.get(key)
    return None if v is None else int(v)


def _floats_map(v: object) -> dict[str, float]:
    return {str(k): float(x) for k, x in cast("dict[str, Any]", v).items()} if isinstance(v, dict) else {}


@dataclass(frozen=True)
class Answer:
    """`ask`'s roles: the model's text, and with a schema its parsed answer in `json` (None when it did not parse)."""

    text: str
    json: Any = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    seconds: float | None = None
    model: str | None = None
    cost_usd: float | None = None  # cloud models
    provider: str | None = None  # cloud models
    raw: Json = field(default_factory=lambda: {}, repr=False, compare=False)

    @classmethod
    def from_json(cls, d: Json) -> Answer:
        return cls(
            str(d.get("text", "")), d.get("json"), _opt_int(d, "tokens_in"), _opt_int(d, "tokens_out"),
            _opt_float(d, "seconds"), _opt_str(d, "model"), _opt_float(d, "cost_usd"), _opt_str(d, "provider"), d,
        )  # fmt: skip


@dataclass(frozen=True)
class GeneratedImage:
    """Role `image-generate`: the picture on this machine (the service's copy) and the seed that makes it again."""

    path: str
    width: int
    height: int
    seed: int
    steps: int | None = None
    seconds: float | None = None
    model: str | None = None
    raw: Json = field(default_factory=lambda: {}, repr=False, compare=False)

    @classmethod
    def from_json(cls, d: Json) -> GeneratedImage:
        return cls(
            str(d["path"]), int(d["width"]), int(d["height"]), int(d["seed"]), _opt_int(d, "steps"),
            _opt_float(d, "seconds"), _opt_str(d, "model"), d,
        )  # fmt: skip


@dataclass(frozen=True)
class Embeddings:
    """Role `image-embed`: one vector per picture and per prompt, in the order sent. With `out=`, the vectors are in
    that .npz file and `images` and `texts` are empty (`raw` has their counts)."""

    images: list[list[float]]
    texts: list[list[float]]
    dim: int
    out: str | None = None
    raw: Json = field(default_factory=lambda: {}, repr=False, compare=False)

    @classmethod
    def from_json(cls, d: Json) -> Embeddings:
        def vectors(v: object) -> list[list[float]]:
            return [_floats(x) for x in cast("list[Any]", v)] if isinstance(v, list) else []

        return cls(vectors(d.get("images")), vectors(d.get("texts")), int(d["dim"]), _opt_str(d, "out"), d)


@dataclass(frozen=True)
class FaceKind:
    real: bool
    kind: str  # person, statue, picture, toy, animal
    probs: dict[str, float]


@dataclass(frozen=True)
class FaceKinds:
    """Role `face-kind`: one verdict per box (or crop), in order; `head` says whether a trained head or CLIP's
    zero-shot similarity decided."""

    faces: list[FaceKind]
    head: str | None = None
    raw: Json = field(default_factory=lambda: {}, repr=False, compare=False)

    @classmethod
    def from_json(cls, d: Json) -> FaceKinds:
        kinds = [
            FaceKind(bool(f["real"]), str(f["kind"]), _floats_map(f.get("probs")))
            for f in cast("list[Json]", d.get("faces", []))
        ]
        return cls(kinds, _opt_str(d, "head"), d)


@dataclass(frozen=True)
class CleanAudio:
    """Role `denoise-audio`: a 48 kHz mono WAV (the service's copy), its loudness before and after, and how much the
    denoiser took out."""

    path: str
    duration_s: float
    sample_rate: int
    engine: str | None = None
    model: str | None = None
    lufs_in: float | None = None
    lufs_out: float | None = None
    removed_db: float | None = None
    seconds: float | None = None
    raw: Json = field(default_factory=lambda: {}, repr=False, compare=False)

    @classmethod
    def from_json(cls, d: Json) -> CleanAudio:
        return cls(
            str(d["path"]), float(d["duration_s"]), int(d["sample_rate"]), _opt_str(d, "engine"), _opt_str(d, "model"),
            _opt_float(d, "lufs_in"), _opt_float(d, "lufs_out"), _opt_float(d, "removed_db"), _opt_float(d, "seconds"), d,
        )  # fmt: skip


@dataclass(frozen=True)
class AudioQuality:
    """Role `audio-quality`: each group only when it was asked for and could be measured, else empty.
    dnsmos: sig, bak, ovrl (1-5); squim: stoi (0-1), pesq (1-4.5), si_sdr (dB); intrusive: si_sdr, pesq, estoi."""

    dnsmos: dict[str, float]
    squim: dict[str, float]
    intrusive: dict[str, float]
    duration_s: float | None = None
    raw: Json = field(default_factory=lambda: {}, repr=False, compare=False)

    @classmethod
    def from_json(cls, d: Json) -> AudioQuality:
        return cls(
            _floats_map(d.get("dnsmos")), _floats_map(d.get("squim")), _floats_map(d.get("intrusive")),
            _opt_float(d, "duration_s"), d,
        )  # fmt: skip


@dataclass(frozen=True)
class SpeakerEmbeddings:
    """Role `speaker-embedding`: one L2-normalised vector per item asked for, in order (a dot product is the cosine
    similarity: near 1 for one voice, near 0 for two), None for a stretch too short to place; `seconds` is how much of
    each was measured (a long one is measured on its middle)."""

    embeddings: list[list[float] | None]
    seconds: list[float]
    dim: int
    raw: Json = field(default_factory=lambda: {}, repr=False, compare=False)

    @classmethod
    def from_json(cls, d: Json) -> SpeakerEmbeddings:
        vectors = cast("list[Any]", d.get("embeddings") or [])
        return cls(
            [None if v is None else _floats(v) for v in vectors], _floats(d.get("seconds")), int(d.get("dim") or 0), d
        )


@dataclass(frozen=True)
class VideoCrop:
    id: str
    label: str
    box: tuple[int, int, int, int]  # in source pixels
    path: str
    stills: list[str]


@dataclass(frozen=True)
class EnhancedVideo:
    """Role `video-enhance`: the MP4 (the service's copy), enlarged crops of the asked regions, still frames, and the
    quality readings that could be taken (ssim, psnr, flicker, sharpness, noise...)."""

    path: str
    width: int
    height: int
    fps: float | None
    frames: int | None
    crops: list[VideoCrop]
    stills: list[str]
    quality: dict[str, Any]
    engine: str | None = None
    model: str | None = None
    seconds: float | None = None
    raw: Json = field(default_factory=lambda: {}, repr=False, compare=False)

    @classmethod
    def from_json(cls, d: Json) -> EnhancedVideo:
        crops = [
            VideoCrop(
                str(c.get("id", "")),
                str(c.get("label", "")),
                _box(c["box"]),
                str(c["path"]),
                [str(p) for p in cast("list[Any]", c.get("stills", []))],
            )
            for c in cast("list[Json]", d.get("crops", []))
        ]
        stills = [str(p) for p in cast("list[Any]", d.get("stills", []))]
        quality = cast("dict[str, Any]", d.get("quality") or {})
        return cls(
            str(d["path"]), int(d["width"]), int(d["height"]), _opt_float(d, "fps"), _opt_int(d, "frames"), crops,
            stills, quality, _opt_str(d, "engine"), _opt_str(d, "model"), _opt_float(d, "seconds"), d,
        )  # fmt: skip


@dataclass(frozen=True)
class Cut:
    time: float  # seconds in the file: the first frame of the new shot
    frame: int  # that frame, counted from the first frame looked at (from 0)
    score: float  # how unlike the frame before it is, 0 to 1


@dataclass(frozen=True)
class Stretch:
    start: float
    end: float


@dataclass(frozen=True)
class Shots:
    """Role `shot-boundaries`: the cuts, the shots they make (end to end over the part looked at) and the stretches
    of black, with the frame rate and frame count read, and the threshold and minimum shot length it ran with.
    `scores` is every frame's score when it was asked for, else empty."""

    cuts: list[Cut]
    shots: list[Stretch]
    black: list[Stretch]
    fps: float | None
    frames: int | None
    duration: float | None
    threshold: float | None = None
    min_shot_s: float | None = None
    scores: list[float] = field(default_factory=lambda: [])
    engine: str | None = None
    model: str | None = None
    seconds: float | None = None
    raw: Json = field(default_factory=lambda: {}, repr=False, compare=False)

    @classmethod
    def from_json(cls, d: Json) -> Shots:
        def stretches(key: str) -> list[Stretch]:
            return [Stretch(float(x["start"]), float(x["end"])) for x in cast("list[Json]", d.get(key) or [])]

        cuts = [
            Cut(float(c["time"]), int(c["frame"]), float(c["score"])) for c in cast("list[Json]", d.get("cuts") or [])
        ]
        return cls(
            cuts, stretches("shots"), stretches("black"), _opt_float(d, "fps"), _opt_int(d, "frames"),
            _opt_float(d, "duration"), _opt_float(d, "threshold"), _opt_float(d, "min_shot_s"), _floats(d.get("scores") or []),
            _opt_str(d, "engine"), _opt_str(d, "model"), _opt_float(d, "seconds"), d,
        )  # fmt: skip
