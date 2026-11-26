"""Shared test helpers for the strata apps: scripted stand-ins for the outside world and CLI runners.

Every app carried its own `tests/utils/fakes.py` (five drifted copies of the same code) and the same `cli` fixtures; they
live here once. The pytest plugin exposes the fakes as fixtures (`fake_urlopen`, `fake_popen`, `fake_run`, `no_sleep`),
so most tests never import this module; import it for `http_error`, `FakeResponse` and the CLI runners.

The fakes are a last resort for quick unit tests of many cases at a true external edge (a web API, an external
program). Prefer the real thing: the `http_server` fixture for HTTP, the actual program for subprocesses (AGENTS.md,
Testing policy).

  FakeResponse     what `urlopen` returns: `.read()`, `.status`, `.headers`, a context manager
  FakeUrlopen      a FIFO of scripted replies (bodies, FakeResponses, exceptions); `.calls` records each request
  http_error       an `HTTPError` with a JSON (or bytes) body, to queue as a reply
  FakePopen        `subprocess.Popen` that starts nothing; `.instances` lists every one made, `.returncode` is settable
  FakeRun          `subprocess.run` / `check_output`; `.returns` maps a command-line substring to stdout, `.code` fails
  Sleeper          `time.sleep` that returns at once and records `.delays`
  run_cli          an argparse `main(argv)` in this process -> CliResult; run_module the same as a real `python -m`
  assert_openapi_current  the committed schema file equals what the app answers now (cli.add_openapi writes it)
  sine_wav         a short PCM WAV of a tone (or silence), for an app's upload and `wav.read_wav` tests
  tiny_dng         a small camera raw file (a linear DNG) LibRaw reads, for `images` and an app's picture tests
  FakeModels       a stand-in strata-models service (jobs, leases, registry) for `strata_python.clients.models`; served
                   by the `fake_models` fixture. MODELS_RESULTS holds one reply per role in the service's shapes.
"""

from __future__ import annotations

import contextlib
import io
import json
import math
import os
import random
import struct
import subprocess
import sys
import urllib.error
import urllib.request
import wave
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from email.message import Message
from pathlib import Path
from typing import Any, ClassVar, cast

Body = bytes | str | Any  # bytes and str are sent as they are; anything else as JSON


def _encode(body: Body) -> bytes:
    if isinstance(body, bytes):
        return body
    if isinstance(body, str):
        return body.encode()
    return json.dumps(body).encode()


def _maybe_json(data: object) -> object:
    """A request body as the test wants to assert on it: parsed JSON when it is JSON, else as sent."""
    if not isinstance(data, (bytes, str)):
        return data
    try:
        return json.loads(data)
    except ValueError:
        return data


class FakeResponse:
    """What `urllib.request.urlopen` returns. `body` is bytes, str or anything JSON-serialisable."""

    def __init__(self, body: Body = b"", status: int = 200, headers: Mapping[str, str] | None = None) -> None:
        self.body = _encode(body)
        self._stream = io.BytesIO(
            self.body
        )  # reads consume it, so a chunked read loop reaches EOF as on a real response
        self.status = status
        self.headers: dict[str, str] = dict(headers or {})

    def read(self, n: int = -1) -> bytes:
        return self._stream.read(n)

    def getcode(self) -> int:
        return self.status

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def http_error(code: int, body: Body = None, url: str = "http://fake") -> urllib.error.HTTPError:
    """An `HTTPError` whose `.read()` gives `body` (JSON unless bytes; `{}` when None). Queue it as a reply."""
    return urllib.error.HTTPError(url, code, "error", Message(), io.BytesIO(_encode({} if body is None else body)))


@dataclass
class Call:
    """One request `FakeUrlopen` was asked to make. `call["url"]` works too, as in the apps' old copies."""

    url: str
    method: str | None
    timeout: float | None
    headers: dict[str, str]  # names lower-cased
    body: object  # parsed JSON when it is JSON

    def __getitem__(self, key: str) -> object:
        return getattr(self, key)


class FakeUrlopen:
    """A scripted `urlopen`. Queue replies in order, run the code, then assert on `.calls`:

        fake_urlopen.reply(http_error(429), {"ok": 1})   # the retry sees a 429, then a body

    A reply is a body, a `FakeResponse` or an exception (raised). A request with nothing queued fails the test rather
    than reaching the network."""

    def __init__(self) -> None:
        self.replies: list[object] = []
        self.calls: list[Call] = []

    def reply(self, *items: object) -> FakeUrlopen:
        self.replies.extend(items)
        return self

    def __call__(
        self, req: str | urllib.request.Request, data: bytes | None = None, timeout: float | None = None, **kw: object
    ) -> FakeResponse:
        if isinstance(req, str):
            call = Call(req, None, timeout, {}, _maybe_json(data))
        else:
            headers = {k.lower(): v for k, v in req.header_items()}
            call = Call(req.full_url, req.get_method(), timeout, headers, _maybe_json(req.data))
        self.calls.append(call)
        if not self.replies:
            raise AssertionError(f"unexpected HTTP request to {call.url} (no reply queued)")
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply if isinstance(reply, FakeResponse) else FakeResponse(reply)


class FakePopen:
    """Stands in for `subprocess.Popen`: records the command and keyword arguments, never starts anything.

    The `fake_popen` fixture hands each test its own subclass, so `.instances` starts empty and never leaks between
    tests. A test finishes a process by setting `fake_popen.instances[-1].returncode = 0`. File objects passed as
    stdin/stdout/stderr are recorded by name, not kept: holding them open would stop a test deleting its folder on
    Windows."""

    instances: ClassVar[list[FakePopen]] = []

    def __init__(self, cmd: Sequence[object], **kw: object) -> None:
        self.cmd = [str(c) for c in cmd]
        self.kw = {k: getattr(v, "name", v) if hasattr(v, "fileno") else v for k, v in kw.items()}
        self.pid = 4242 + len(type(self).instances)
        self.returncode: int | None = None
        type(self).instances.append(self)

    @classmethod
    def fresh(cls) -> type[FakePopen]:
        """A subclass with its own empty `.instances`."""
        return type("FakePopen", (cls,), {"instances": []})

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        return 0 if self.returncode is None else self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    def __enter__(self) -> FakePopen:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class FakeRun:
    """Stands in for `subprocess.run` and `check_output`: `.calls` holds each command as a list of strings;
    `.returns["ffprobe"] = b"..."` gives the stdout of any command whose line contains that text (default `b""`,
    returned as the test set it, bytes or str); `.code = 1` makes every command fail (raising only with `check=True`,
    as `subprocess.run` does; `check_output` always checks)."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.returns: dict[str, bytes | str] = {}
        self.code = 0

    def _out(self, cmd: Sequence[object]) -> bytes | str:
        line = " ".join(map(str, cmd))
        return next((v for k, v in self.returns.items() if k in line), b"")

    def __call__(self, cmd: Sequence[object], *a: object, **kw: object) -> subprocess.CompletedProcess[Any]:
        self.calls.append([str(c) for c in cmd])
        out = self._out(cmd)
        if self.code and kw.get("check"):
            raise subprocess.CalledProcessError(self.code, self.calls[-1], out)
        return subprocess.CompletedProcess(self.calls[-1], self.code, out, b"")

    def check_output(self, cmd: Sequence[object], *a: object, **kw: object) -> bytes | str:
        self.calls.append([str(c) for c in cmd])
        out = self._out(cmd)
        if self.code:
            raise subprocess.CalledProcessError(self.code, self.calls[-1], out)
        return out


@dataclass
class Sleeper:
    """`time.sleep` that returns at once; `.delays` records what was asked, so a test can assert on a backoff."""

    delays: list[float] = field(default_factory=lambda: [])

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


@dataclass
class CliResult:
    stdout: str
    stderr: str
    code: int

    @property
    def out(self) -> str:
        return self.stdout + self.stderr


def run_cli(main: Callable[[list[str]], int | None], *args: object, check: bool = True) -> CliResult:
    """Run an argparse-style `main(argv)` in this process, capturing its output and exit code (a `SystemExit` counts).
    Raises AssertionError with the output on a non-zero exit unless `check=False`."""
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main([str(a) for a in args]) or 0
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (1 if e.code else 0)
            if isinstance(e.code, str):
                err.write(e.code + "\n")
    result = CliResult(out.getvalue(), err.getvalue(), code)
    if check and code:
        raise AssertionError(f"{' '.join(map(str, args))} exited {code}:\n{result.out}")
    return result


def run_module(
    module: str, *args: object, src: str | Path | None = None, check: bool = True, timeout: float = 300
) -> CliResult:
    """The same through a real `python -m module` process, for entry-point smoke tests and anything that must not
    share this process's state. `src` is put on PYTHONPATH (an app's `src` folder when it is not installed)."""
    env = dict(os.environ, PYTHONWARNINGS="ignore")
    if src is not None:
        env["PYTHONPATH"] = os.pathsep.join([str(src), *filter(None, [os.environ.get("PYTHONPATH")])])
    p = subprocess.run(  # noqa: S603 - the interpreter running the tests, on the app's own module
        [sys.executable, "-m", module, *map(str, args)], capture_output=True, text=True, env=env, timeout=timeout
    )
    result = CliResult(p.stdout, p.stderr, p.returncode)
    if check and p.returncode:
        raise AssertionError(f"python -m {module} exited {p.returncode}:\n{result.out}")
    return result


# --- a stand-in strata-models ---------------------------------------------------------------------------------------

Json = dict[str, Any]
Result = Json | Callable[[Json], Json]

MODELS_RESULTS: dict[str, Json] = {
    "ocr": {
        "width": 1200, "height": 1800, "text": "CHAPTER I\nIt was a dark night.",
        "lines": [
            {"text": "CHAPTER I", "box": [100, 80, 300, 40], "conf": 96.5, "block": 1,
             "words": [{"text": "CHAPTER", "box": [100, 80, 200, 40], "conf": 97.0}, {"text": "I", "box": [320, 80, 20, 40], "conf": 96.0}]},
            {"text": "It was a dark night.", "box": [100, 160, 600, 30], "conf": 91.2, "block": 2, "words": []},
        ],
        "engine": "tesseract 5.5", "lang": "eng", "psm": 3, "clean": [], "seconds": 1.2, "model": "tesseract", "pdf": "/out/p.pdf",
    },
    "transcribe": {
        "language": "en", "language_probability": 0.99, "duration": 3.5,
        "segments": [{"start": 0.0, "end": 1.5, "text": " Hello", "avg_logprob": -0.2, "no_speech_prob": 0.01,
                      "words": [{"start": 0.0, "end": 1.5, "word": " Hello", "probability": 0.9}]},
                     {"start": 1.5, "end": 3.5, "text": " world.", "avg_logprob": -0.3, "no_speech_prob": 0.02}],
    },
    "faces": {"width": 640, "height": 480, "faces": [{"bbox": [1, 2, 30, 40], "kps": [[5, 6]] * 5, "det_score": 0.87, "embedding": [0.1] * 4, "pose": [1, 2, 3]}, {"bbox": [9, 9, 20, 20]}]},
    "speak": {"path": "/out/s.wav", "duration_s": 2.5, "sample_rate": 24000, "seconds": 0.8, "engine": "kokoro", "voice": "af_heart", "seed": 1, "speech_s": 2.1},
    "vision-language": {"text": '{"scene": "beach"}', "json": {"scene": "beach"}, "tokens_in": 812, "tokens_out": 9, "seconds": 1.4, "model": "qwen3.5-9b-vl", "peak_vram_mb": 7100},
    "image-generate": {"path": "/out/g.png", "width": 1024, "height": 768, "seed": 1234, "steps": 4, "seconds": 9.5, "peak_vram_mb": 9000, "model": "flux3-klein"},
    "image-embed": {"images": [[0.6, 0.8]], "texts": [[1.0, 0.0], [0.0, 1.0]], "dim": 2},
    "face-kind": {"faces": [{"real": True, "kind": "person", "probs": {"person": 0.97, "statue": 0.03}}, {"real": False, "kind": "statue", "probs": {}}], "head": "trained head.npz"},
    "denoise-audio": {"path": "/out/c.wav", "duration_s": 4.0, "sample_rate": 48000, "seconds": 0.7, "engine": "dfn3", "model": "deepfilternet3", "lufs_in": -31.2, "lufs_out": -14.0, "removed_db": 6.5},
    "audio-quality": {"dnsmos": {"sig": 3.4, "bak": 3.9, "ovrl": 3.0}, "squim": {"stoi": 0.93, "pesq": 2.8, "si_sdr": 14.2}, "duration_s": 4.0},
    "video-enhance": {
        "path": "/out/v.mp4", "width": 1440, "height": 1080, "fps": 50.0, "frames": 400, "seconds": 61.0, "engine": "ffmpeg", "model": "video-lanczos",
        "crops": [{"id": "face", "label": "Gran", "box": [100, 50, 80, 80], "path": "/out/v-face.mp4", "stills": ["/out/v-face-3.png"]}],
        "stills": ["/out/v-1.png"], "enhance_s": 40.0, "quality": {"ssim": 0.91, "skipped": "no numpy"},
    },
    "shot-boundaries": {
        "cuts": [{"time": 4.0, "frame": 100, "score": 0.82}, {"time": 9.36, "frame": 234, "score": 0.41}],
        "shots": [{"start": 0.0, "end": 4.0}, {"start": 4.0, "end": 9.36}, {"start": 9.36, "end": 12.0}],
        "black": [{"start": 3.6, "end": 4.0}], "fps": 25.0, "frames": 300, "duration": 12.0, "threshold": 0.3,
        "min_shot_s": 0.6, "width": 160, "deinterlace": True, "engine": "ffmpeg 7.1", "seconds": 0.4, "model": "ffmpeg-scene-score",
    },
}  # fmt: skip
"""One reply per role in the service's own shapes (taken from its runtimes), so `Shape.from_json` reads each one."""
for _role in ("transcribe-fast",):
    MODELS_RESULTS[_role] = MODELS_RESULTS["transcribe"]
for _role in ("text", "judge-text", "judge-vision", "judge-audio"):
    MODELS_RESULTS[_role] = MODELS_RESULTS["vision-language"]


def assert_openapi_current(create_app: Callable[[], Any], path: str | os.PathLike[str]) -> None:
    """Fail, with the command to run, when the committed OpenAPI file at `path` is not what `create_app()` answers
    now: the unit test every app with generated web types writes."""
    from strata_python.web import openapi_text  # FastAPI only when asked

    file = Path(path)
    current = file.read_text(encoding="utf-8") if file.is_file() else None
    if current != openapi_text(create_app()):
        state = "missing" if current is None else "out of date"
        raise AssertionError(f"{file} is {state}: run the app's `openapi` command and commit the file")


class FakeModels:
    """A stand-in strata-models service for an app's tests, over real HTTP: the `fake_models` fixture serves `.app` on
    a loopback port and points `STRATA_MODELS_URL` at it, so the app's own calls through `strata_python.clients.models`
    run unchanged. The job and lease endpoints follow the service's steps (queued, running, then done or failed;
    long-poll `?wait=` is accepted and answered at once), so a test sees what the app does with each outcome.

        fake_models.results["ocr"] = {...}            # or a function of the job's params
        fake_models.fail("CUDA out of memory")        # the next job fails (fail(..., role="faces"): the next faces job)
        fake_models.busy = True                       # leases stay queued: the GPU is someone else's
        fake_models.down = True                       # every request is a 503
        assert fake_models.jobs[0]["params"]["lang"] == "eng"

    `running_polls` is how many polls a job answers `running` before it finishes (default 1, as the real service
    usually is when the first poll arrives). A role with no result is refused with a 422, as the service refuses a role
    it does not know. The registry answers each role's model from `.models` (role -> model name). A job records the
    settings it ran with when it starts, as the service does: `options` (`.options[role]`, the role's load options,
    merged with the job's own) and `settings_hash` (a hash of the model and those options, also on the registry's roles),
    so changing `.options` mid-test shows what an app's cache does when the service's settings change."""

    def __init__(self, results: Mapping[str, Result] | None = None, *, running_polls: int = 1) -> None:
        from fastapi import FastAPI, HTTPException, Request  # here, so importing the fakes does not load FastAPI

        self.results: dict[str, Result] = dict(MODELS_RESULTS if results is None else results)
        self.models: dict[str, str] = {}
        self.options: dict[str, Json] = {}  # role -> the model's load options (the service's models.toml)
        self.running_polls = running_polls
        self.jobs: list[Json] = []  # every job submitted, in order, as the service stored it
        self.leases: list[Json] = []
        self.heartbeats: list[int] = []
        self.released: list[int] = []
        self.busy = False
        self.refuse = False  # a lease request ends at once, without the GPU
        self.down = False
        self._failures: list[tuple[str | None, str]] = []
        self._polls: dict[int, int] = {}
        self._asked: dict[int, Json] = {}  # job id -> the load options its submit asked for
        app = FastAPI()
        self.app = app

        @app.middleware("http")
        async def unavailable(request: Request, call_next: Callable[[Request], Any]) -> Any:  # pyright: ignore[reportUnusedFunction]
            if self.down:
                from fastapi.responses import JSONResponse

                return JSONResponse({"detail": "starting"}, status_code=503)
            return await call_next(request)

        @app.get("/api/health")
        def health() -> Json:  # pyright: ignore[reportUnusedFunction]
            return {"ok": True, "version": "0-fake", "gpu": None}

        @app.get("/api/registry")
        def registry() -> Json:  # pyright: ignore[reportUnusedFunction]
            return {
                "roles": [{"key": r, "model": self._model(r), "settings_hash": self._hash(r)} for r in self.results]
            }

        def add(body: Json) -> Json:
            job: Json = {
                **body,
                "id": len(self.jobs) + 1,
                "status": "queued",
                "result": None,
                "error": None,
                "options": None,  # what it runs with, recorded when it starts; the variant asked for is kept apart
                "settings_hash": None,
            }
            self._asked[job["id"]] = cast("Json", body.get("options") or {})
            self.jobs.append(job)
            return job

        def known(role: str) -> None:
            if role not in self.results:
                raise HTTPException(422, f"unknown role {role!r}")

        @app.post("/api/jobs")
        def submit(body: Json) -> Json:  # pyright: ignore[reportUnusedFunction]
            known(str(body.get("role")))
            if body.get("priority", "high") != "high":  # as the service: one job on its own is someone waiting on it
                raise HTTPException(
                    400, "a single job is high priority: queue normal and low work with POST /api/jobs/bulk"
                )
            return dict(add({**body, "priority": "high"}))

        @app.post("/api/jobs/bulk")
        def submit_bulk(body: Json) -> Json:  # pyright: ignore[reportUnusedFunction]
            known(str(body.get("role")))
            items = cast("list[Json]", body.get("items") or [])
            rest = {k: v for k, v in body.items() if k not in ("items", "batch")}
            batch = body.get("batch")
            return {
                "ids": [
                    add({**rest, "params": p, "priority": rest.get("priority") or "normal", "batch": batch})["id"]
                    for p in items
                ]
            }

        @app.get("/api/jobs/{job_id}")
        def poll(job_id: int, wait: float | None = None) -> Json:  # pyright: ignore[reportUnusedFunction]
            job = self._job(job_id, HTTPException)
            if wait is not None and job["status"] in ("queued", "running"):  # a look without `wait` moves nothing on
                n = self._polls[job_id] = self._polls.get(job_id, 0) + 1
                if job["status"] == "queued":
                    self._start(job)
                if n <= self.running_polls:
                    job["status"] = "running"
                else:
                    self._finish(job)
            return dict(job)

        @app.post("/api/leases")
        def lease(body: Json) -> Json:  # pyright: ignore[reportUnusedFunction]
            row: Json = {**body, "id": len(self.leases) + 1, "status": "cancelled" if self.refuse else "queued"}
            self.leases.append(row)
            return dict(row)

        @app.get("/api/leases/{lease_id}")
        def lease_poll(lease_id: int, wait: float = 0) -> Json:  # pyright: ignore[reportUnusedFunction]
            row = self._lease(lease_id, HTTPException)
            if row["status"] == "queued" and not self.busy:
                row["status"] = "running"
            return dict(row)

        @app.post("/api/leases/{lease_id}/heartbeat")
        def beat(lease_id: int) -> Json:  # pyright: ignore[reportUnusedFunction]
            self._lease(lease_id, HTTPException)
            self.heartbeats.append(lease_id)
            return {"ok": True}

        @app.delete("/api/leases/{lease_id}")
        def release(lease_id: int) -> Json:  # pyright: ignore[reportUnusedFunction]
            self._lease(lease_id, HTTPException)["status"] = "done"
            self.released.append(lease_id)
            return {"ok": True}

    def fail(self, error: str = "the model failed", *, role: str | None = None) -> FakeModels:
        """The next job (of `role`, when given) fails with `error`. Calls queue up: each failure is used once."""
        self._failures.append((role, error))
        return self

    def _model(self, role: str, model: object = None) -> str:
        return str(model) if model else self.models.get(role, f"fake-{role}")

    def _hash(self, role: str, model: object = None, asked: Json | None = None) -> str:
        import hashlib

        options = {**self.options.get(role, {}), **(asked or {})}
        text = json.dumps([self._model(role, model), options], sort_keys=True, default=str)
        return hashlib.sha256(text.encode()).hexdigest()[:12]

    def _start(self, job: Json) -> None:
        role, asked = str(job["role"]), self._asked.get(int(job["id"]), {})
        job.update(
            model=self._model(role, job.get("model")),
            options={**self.options.get(role, {}), **asked},
            settings_hash=self._hash(role, job.get("model"), asked),
        )

    def _finish(self, job: Json) -> None:
        for i, (role, error) in enumerate(self._failures):
            if role is None or role == job["role"]:
                del self._failures[i]
                job.update(status="failed", error=error)
                return
        result = self.results[job["role"]]
        job.update(status="done", result=result(dict(job["params"] or {})) if callable(result) else result)

    def _job(self, job_id: int, error: Callable[[int, str], Exception]) -> Json:
        if not 1 <= job_id <= len(self.jobs):
            raise error(404, f"no job {job_id}")
        return self.jobs[job_id - 1]

    def _lease(self, lease_id: int, error: Callable[[int, str], Exception]) -> Json:
        if not 1 <= lease_id <= len(self.leases):
            raise error(404, f"no lease {lease_id}")
        return self.leases[lease_id - 1]


def sine_wav(
    seconds: float = 1.0,
    *,
    rate: int = 16_000,
    width: int = 2,
    channels: int = 1,
    hz: float = 220.0,
    amplitude: float = 0.3,
    lead: float = 0.0,
    tail: float = 0.0,
    noise: float = 0.0,
) -> bytes:
    """A PCM WAV of a tone at `amplitude` (0 to 1; 0 is silence, above 1 is cut at full scale, as an overdriven
    input clips): real bytes an app's upload tests send. `lead` and `tail` are seconds of quiet before and after the
    tone, and `noise` the peak of a white noise under all of it (a room), the same noise on every call."""
    full = (1 << (8 * width - 1)) - 1
    rng = random.Random(0)  # noqa: S311 - a test signal, not a secret
    frames = bytearray()
    start, end = round(lead * rate), round((lead + seconds) * rate)
    for i in range(end + round(tail * rate)):
        tone = amplitude * math.sin(2 * math.pi * hz * (i - start) / rate) if start <= i < end else 0.0
        v = round(full * max(-1.0, min(1.0, tone + (noise * rng.uniform(-1, 1) if noise else 0.0))))
        sample = struct.pack("<i", v)[:width] if width > 1 else struct.pack("<B", v + 128)
        frames += sample * channels
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return out.getvalue()


def tiny_dng(
    path: str | os.PathLike[str],
    size: tuple[int, int] = (64, 48),
    *,
    orientation: int = 1,
    top: tuple[int, int, int] = (65535, 0, 0),
    bottom: tuple[int, int, int] = (0, 0, 65535),
) -> str:
    """Write a camera raw file no camera made: a linear-raw DNG (16-bit RGB, one strip, the tags LibRaw asks for)
    whose top half is `top` and bottom half `bottom`, stored with the EXIF `orientation` given. Real raw files are
    tens of megabytes and their makers' formats closed; this is enough to see a raw file read, developed and turned."""
    import struct

    w, h = size

    def row(c: tuple[int, int, int]) -> bytes:
        return struct.pack("<3H", *c) * w

    data = row(top) * (h // 2) + row(bottom) * (h - h // 2)
    name = b"strata test\0"
    short, long_, byte, ascii_, srational = 3, 4, 1, 2, 10
    # (tag, type, count, value): an int, bytes kept in the entry or after it, or a placeholder filled in below.
    tags: list[tuple[int, int, int, int | bytes]] = [
        (254, long_, 1, 0), (256, long_, 1, w), (257, long_, 1, h), (258, short, 3, struct.pack("<3H", 16, 16, 16)),
        (259, short, 1, 1), (262, short, 1, 34892), (271, ascii_, len(name), name), (272, ascii_, len(name), name),
        (273, long_, 1, 0), (274, short, 1, orientation), (277, short, 1, 3), (278, long_, 1, h),
        (279, long_, 1, len(data)), (284, short, 1, 1), (50706, byte, 4, bytes([1, 4, 0, 0])),
        (50708, ascii_, len(name), name), (50717, long_, 1, 65535),
        (50721, srational, 9, struct.pack("<18i", *[v for i in range(9) for v in ((1, 1) if i % 4 == 0 else (0, 1))])),
    ]  # fmt: skip
    after = 8 + 2 + len(tags) * 12 + 4
    big = sum(len(v) + len(v) % 2 for _, _, _, v in tags if isinstance(v, bytes) and len(v) > 4)
    tags = [(t, ty, n, after + big if t == 273 else v) for t, ty, n, v in tags]  # the strip follows the long values
    blobs, entries = b"", b""
    for tag, typ, count, value in tags:
        if isinstance(value, bytes) and len(value) > 4:
            entries += struct.pack("<HHII", tag, typ, count, after + len(blobs))
            blobs += value + b"\0" * (len(value) % 2)
        elif isinstance(value, bytes):
            entries += struct.pack("<HHI", tag, typ, count) + value.ljust(4, b"\0")
        elif typ == short:
            entries += struct.pack("<HHIHH", tag, typ, count, value, 0)
        else:
            entries += struct.pack("<HHII", tag, typ, count, value)
    out = b"II*\0" + struct.pack("<IH", 8, len(tags)) + entries + struct.pack("<I", 0) + blobs + data
    with open(path, "wb") as f:
        f.write(out)
    return os.fspath(path)
