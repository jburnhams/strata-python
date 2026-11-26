# strata-python

Common code for the strata Python web server apps (FastAPI + CLI), the Python counterpart of [strata-react](https://github.com/jburnhams/strata-react). Every app made from [python-app-template](https://github.com/jburnhams/python-app-template) used to carry its own drifting copy of these modules; they live here once. It is opinionated and built for those apps rather than as a general-purpose library, but it is public, MIT-licensed and on PyPI.

| Module | What it gives an app |
|---|---|
| `strata_python.oslib` | Cross-platform file locks, process liveness (`pid_alive`), `kill_tree`, `lower_priority`, `detached_kwargs`, `load_average`, `start_supervised`/`watch_supervisor`/`kill_group` (a child in its own process group that dies with its parent), `venv_python`, `cli_command(module)` |
| `strata_python.settings` | `Settings` / `Setting` / `ListSetting`: one JSON file in `~/.<app>` with `<APP>_*` environment overrides, validation (`absolute_path`, `non_empty`, `number_between`, `positive`, `whole_between`, `iso_date`, `boolean`), typed getters, atomic saves, and `bind(store)` to keep them elsewhere (`strata_python.db.settings.TableStore`) |
| `strata_python.web` | `base_app` (health with the app's own fields and `commit` + typed settings routes), `add_error_handlers` (exceptions to `ErrorBody` responses), `add_system` (`GET /api/system`: folders, disks, services), `source_commit`, `add_web` (built web app or stand-in page at every non-API path), `with_base` (reverse-proxy-safe `<base href>`), `static_file` (path-safe), `serve`, `openapi_text` (the schema as stable text) |
| `strata_python.cli` | `add_doctor` (imports, labels, optional modules and the app's own `Check`s), `add_set`, `add_serve`, `add_openapi` (the schema file for strata-react's `strata-api-types`) for the app's argparse subcommands |
| `strata_python.contracts` | The shapes the apps' APIs share, as pydantic models for `response_model`: `Health`, `SystemInfo`, `ErrorBody`, `Task` (with `TaskState`), `Progress`, `Page[T]`, `FileRef`, `Judgment`; `ApiModel`, a base for the app's own responses whose defaulted fields stay required in the schema |
| `strata_python.client` | `ServiceClient`, the standard-library base for calling a strata service: JSON calls, `Rejected` (4xx, with the reason and payload) and `Unavailable` (down or 5xx), retried reads, a long-poll `wait`, `available()`, and `env_url` |
| `strata_python.tasks` | Work outside a request: `next_state`/`backoff_s`/`RetryLater` (when to retry), `report_progress` and `ProgressReporter` (throttled), `SingleFlight` (one run per key, `Conflict` on a second), `StatusFile` and `Detached` (a process that outlives the server), `Ticker` (a loop that survives its errors, can choose its next wait and be woken early), and with the `[db]` extra `tasks.queue` (`Queue`, `Worker`, `jobs_table`: durable jobs with retry, dedupe, resources, cancel and crash recovery) and `tasks.web.add_jobs` (`/api/jobs`: list, counts, one, cancel, retry, answering the shared `Task`) |
| `strata_python.workflow` | Work in named, versioned stages over one subject: `Pipeline` of `step`s and `gate`s (a person's approval, or `auto` from a policy), each stage cached by a key of its version, the inputs it reads and its needs' answers, so a re-run does only what changed; `advance` resumes where it stopped, runs the rest when one stage fails and stops at a gate; `approve`, `clear` (with its dependents), `status` (done, ready, stale, failed, waiting, blocked per stage); `Store` (a two-method protocol: `MemoryStore`, or the app's own), `Pipeline(key=...)` (the app's own keys) and `Pipeline(json=False)` (stages hand each other in-memory values); `request_key`; `StateMachine` (a row's declared transitions, `InvalidTransition` is a 400). With the `[db]` extra `workflow.store`: `workflow_table`, `DbStore` (append-only records in the app's database), `enqueue` (a queue job per subject) and `move` (a state change as one conditional UPDATE), and `workflow.web.add_workflow` (the same routes for every pipeline: list subjects by state, one subject's `WorkflowRun`, a stage's history, run, approve, clear) |
| `strata_python.files` | Atomic writes (`atomic_path` keeps the extension for ffmpeg, `atomic_open`, `write_json_atomic`, `write_text_atomic`, `write_bytes_atomic`), `read_json(path, default)`, `sha256`/`sha256_hex`, `quick_fingerprint` (size and both ends), `stat_key` (a cache key that follows the file), `inside(root, path)` (refuses `..` and links out), and `DiskCache` (values on disk with an age; `json(key, fetch, max_age_s)`) |
| `strata_python.http` | `Http(user_agent, min_interval=...)`, the polite client for other people's APIs: a gap per host across threads (and processes with `throttle_file`), Retry-After as seconds or a date, a cool-down for a host that keeps saying 429, retries that never replay a write, a size cap, an optional `DiskCache`, and errors returned as a `Reply` (`raise_for_status()`, `get_json()` for code that wants exceptions) |
| `strata_python.media` | `probe(path)` (a typed `Probe`: duration, format, tags, streams, chapters, ffprobe's warnings), `duration`, `ffmpeg(args, progress=...)` (or `sink=` for stdout in chunks) raising `MediaError` with ffmpeg's own reason, `available()`, `configure(ffmpeg=..., ffprobe=...)` (the app's own setting for where the tools are) |
| `strata_python.derive` | Files an editor shows, made from footage: `proxy(src, out)` (H.264 MP4 for the browser, deinterlaced for display, square pixels, a keyframe every half second, the source's frames at the source's times), `thumbnails(src, folder, every=10)` (`Thumb(time, path)`, exact times), `frames(src, folder, [n, ...])` (`Frame(number, path)`, one picture per chosen frame: one per shot), `peaks(src, per_second=50)` (lowest and highest sample per slice, strata-react `Waveform`'s pair), `interlacing(path)` (the stream's field order, else ffmpeg's `idet`), and `RECIPE` versions for an app's cache key |
| `strata_python.mediatime` | Exact time for media: `rational` seconds (Fraction), `Rate` (`frame_at`, `time_of`, `snap`), half-open `Span`, and `TimeMap` from an edit's output time to its sources and back, with exact JSON |
| `strata_python.wav` | `read_wav(data_or_path, max_seconds=...)` checks an uploaded PCM WAV's header with the standard library (mono, 16 or 24-bit, 16 kHz and up by default; `channels`, `widths`, `min_rate`, `min_seconds` change it) and returns a `WavInfo`; anything else is a `WavError` (a `ValueError`, so a 400) with a sentence. strata-react's `useRecorder` writes what it accepts. `measure(data_or_path)` decodes it and returns `WavLevels`: `peak_db`, `clipped` (samples at full scale), `noise_db` (the quietest tenth of 20 ms windows), `speech_db` (the loudest twentieth) and `snr_db`, for judging a take of speech |
| `strata_python.speech` | A library of spoken clips for an app that speaks fixed lines: `ClipStore(root)` (a manifest of line -> `Clip` with its source, voice, model and settings hash, and WAVs named by content), `build(store, lines, app=, voice=, params=, per_line=, now=)` (makes the missing or out-of-date clips with strata-models' `speak` role, never remakes a person's recording, reports each failed line) and `add_clip_routes(app, store, prefix, recordable=)` (the clip list, the clips cached for good, and a person's recordings in and out) |
| `strata_python.wer` | `word_errors(said, heard)` aligns a transcript with the text meant: `.wer`, `.missed` (words meant but not heard as themselves), `.extra`, and `.edits` (`ok`, `substitute`, `delete`, `insert`); `normalise(text)` is how both sides are read (case, punctuation and curly apostrophes out) |
| `strata_python.images` (extra `[images]`) | Reads JPEG, PNG, WebP, AVIF, TIFF, HEIC/HEIF, JPEG XL and camera raw (`extensions()`, `is_raw`, `open_raw`, `size_upright`). `thumbnail(src, px, cache_dir=...)` (an upright JPEG, made once, keyed on the file, never enlarged, the app's own reader through `make=`), `open_upright`, `flatten` (alpha and 16-bit greys to 8-bit RGB), `jpeg_bytes`, `webp_bytes`; `thumbnail(..., fmt="webp")` for a medium copy to keep; `crop(src, box, px, cache_dir=..., pad=1.6)` a cached, padded, square crop of one region (a face) |
| `strata_python.stats` | Pairwise rankings and eval statistics in pure Python: `bradley_terry` (ties, an optional side advantage, Elo-scale `ratings()`), `intervals` (bootstrap), `head_to_head`, `votes_from(judgments)`, `wilson`, `mcnemar`, `holm` |
| `strata_python.clients.models` | The strata-models client, standard library only: `run`, `submit`, `wait`, `role_model`, `run_with_provenance` and `role_settings` (the model and settings a result was made with, for cache keys), `gpu_lease`, a shortcut for every role the service runs (`faces`, `face_kind`, `transcribe`, `ocr`, `speak`, `denoise_audio`, `audio_quality`, `generate_image`, `embed`, `enhance_video`, `shot_boundaries`, and `ask` for the language models) and a typed result for each |
| `strata_python.clients.faces` | strata-models' face store, standard library only: `index` (detect, embed and store a picture's faces; idempotent per app and ref), `search` (every app's faces but opt-in tags, or `tags=` only), `cluster`, identities (`create_identity`, `identity`, `find_identity`, `update_identity`, `set_app_fields`: an app's own fields over the global ones), `assign` and `undo` (batches), `merge`, `unmerge`, `not_same`, `merge_suggestions`, `set_tag(..., opt_in=)`, `crop_url`, `picture_url` |
| `strata_python.http_server` / `live` | A real scripted HTTP server and a real uvicorn server on loopback, for tests that make actual requests |
| pytest plugin | Loaded automatically: hermetic home and data folders (`strata_env` adds the app's own variables), a no-network guard, `http_server` and `live_app`, the `fake_urlopen`, `fake_popen`, `fake_run` and `no_sleep` fixtures, `fake_models` (a stand-in strata-models), and the `integration` marker on everything under `tests/integration` |
| `strata_python.testing` | The fakes behind those fixtures (`FakeResponse`, `http_error`, `FakeUrlopen`, `FakePopen`, `FakeRun`, `Sleeper`) `FakeModels`, the CLI runners `run_cli(main, *args)` and `run_module("myapp", *args, src=...)`, `assert_openapi_current(create_app, path)`, and `sine_wav(seconds, rate=, width=, channels=, amplitude=, lead=, tail=, noise=)` (real WAV bytes for upload tests: a tone with quiet before and after it and a room's noise under it, clipped above amplitude 1), `tiny_dng(path, size, orientation=)` (a small camera raw file LibRaw reads) and `tiny_picture(path, size, colour)` (any format the readers write, HEIC included, encoded in a child process) |
| `strata_python.db` (extra `[db]`) | The shared database: `connect("myapp")` (the app's schema of one shared Postgres, or a SQLite path), a declarative base with the shared `NAMING_CONVENTION` for typed models, Alembic migrations with no per-app `env.py`, the live-database guard, the `strata-db` command, the `db_*` fixtures (SQLite by default, a throwaway Postgres with `--pg`), `strata_python.db.web` (`add_database`, `DbSession`) for a server's records, and `insert_once` and `lock_row` for writes strata-react's `createOutbox` sends and retries, and `strata_python.db.vectors` (`Vector`, `hnsw_index`, `nearest`) for embeddings on Postgres and SQLite alike |
| `strata_python.pages` (extra `[pdf]`) | A scanned page described as blocks, lines, styled spans and figures in scan pixels, drawn as a PDF with real embedded fonts (`render`), and read back as pixels at the scan's dpi (`rasterise`) and as text (`read_text`): for rebuilding scanned books, and for scoring a rebuild with the renderer that prints it |
| `strata-check` | The one gate: lockfile, ruff, `ruff format`, pyright strict, ty, unit tests with coverage |
| `strata-public-check` | Fails on email addresses, private IPv4 addresses, home-folder paths, the repo's own `[tool.strata-public-check] deny` regexes and the owner's `STRATA_PUBLIC_DENY` ones in a tree, sdist or wheel; run before anything public is published. Reads Python, web source (`.ts`, `.tsx`, `.js`, `.css`, `.html`, `.mdx`, `.svg`...) and config text; in a git checkout only what git would commit, so tool output such as `.coverage` is skipped. Silence a false positive with `[tool.strata-public-check] exclude = ["web/package-lock.json"]` (or `--exclude`), or `strata-public-check: ignore` on the line (never applies to `STRATA_PUBLIC_DENY`) |

## Move an app onto it

The goal: an app keeps only its plumbing and business logic, and everything else comes from here. Work through the steps in order, one commit each, running `scripts/check` after every step. Each step says what to delete, so the app gets smaller as it goes. What changed in each release, and how to upgrade, is in [CHANGELOG.md](CHANGELOG.md).

### 1. Depend on the release

```toml
# pyproject.toml
[project]
dependencies = [
    "strata-python>=0.2,<0.3",   # brings FastAPI, uvicorn, pydantic and pytest; while 0.x, cap below the next minor
    # ... only what is the app's own: torch, opencv, yt-dlp ...
]

[dependency-groups]
dev = ["strata-python[dev]"]     # ruff, pyright, ty, httpx and the pytest plugins, at the versions strata-check expects
```

Delete from the app's own lists everything strata-python already brings (fastapi, uvicorn, pydantic, pytest, ruff, pyright, ty, pytest-cov, pytest-xdist, pytest-randomly, pytest-timeout, httpx) and any `[tool.uv.sources] strata-python = { git = ... }` entry, then run `uv lock`. To try an unreleased commit, put a `git` source with `rev = "<sha>"` back for the moment.

The shared versions are decided here: strata-python sets a minimum (what its CI tested) and a cap (the next release that may break) for each, and its own dependabot keeps them current. Each app still has its own `uv.lock`, so one dependabot PR a week per app is the floor; make it one, not one per package:

```yaml
# .github/dependabot.yml in the app
version: 2
updates:
  - package-ecosystem: uv
    directory: /
    schedule: { interval: weekly }
    groups: { python: { patterns: ["*"] } }   # strata-python and everything else in one PR
  - package-ecosystem: github-actions
    directory: /
    schedule: { interval: monthly }
    groups: { actions: { patterns: ["*"] } }
```

Security updates still arrive on their own as soon as they are published; keep them on.

### 2. The gate

`scripts/check` becomes one line, and the app's own `scripts/check.py` goes:

```bash
#!/bin/bash
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR/.." && exec uv run --locked strata-check "$@"
```

It runs the lockfile check, ruff, `ruff format`, pyright strict, ty and the unit tests with coverage. `--fast` skips the tests and `--fix` applies safe ruff fixes. An optional `[tool.strata-check]` sets `paths` (default `src`, `tests`, `scripts`) and `tests` (default `tests/unit`). CI calls the same script. A public app also runs `uv run strata-public-check . dist/*` in CI, with `STRATA_PUBLIC_DENY` as a repository secret.

### 3. Settings

Replace the app's `settings.py` (its key list, getters, file handling and environment lookups) with a declaration:

```python
from strata_python.settings import ListSetting, Setting, Settings, absolute_path, non_empty, whole_between

SETTINGS = Settings(
    "myapp",  # ~/.myapp/settings.json, moved with MYAPP_HOME
    [
        Setting("data_dir", default=lambda: "/srv/myapp", normalise=absolute_path),
        Setting("greeting", default="Hello", normalise=non_empty),
        Setting("workers", default="2", normalise=whole_between(1, 16)),
        Setting("mode", default="mixed", choices=["mixed", "review", "new"]),  # an enum in the API schema
        ListSetting("source_roots", item=absolute_path, env="MYAPP_SOURCES"),  # MYAPP_SOURCES split on os.pathsep
    ],
    prefix="MYAPP",  # only if the app already runs with other variable names
)

SETTINGS.get("greeting")  # MYAPP_GREETING, else the file, else the default
SETTINGS.save({"greeting": "Hi"})  # validated and written atomically; None resets to the default
SETTINGS.folder("data_dir")  # the value as a folder, created if missing
```

Values are canonical strings, and lists of them. Normalisers for the common kinds give one spelling each: `number_between(lo, hi)`, `whole_between(lo, hi)`, `iso_date(allow_blank=True)` and `boolean`, read back with `get_float`, `get_int` and `get_bool`. `hi=None` has no ceiling; `number_between(0, lo_open=True)`, or `positive(hi=None)`, refuses 0 itself ("must be more than 0") for a value with no natural floor, such as a cache size. An existing settings file carries over as it is: a JSON number for a `number_between` or `whole_between` setting, and `true`/`false` for a `boolean` one, are read as their text; any other value that is not text is ignored, with one warning in the log per value. A `ListSetting` is a JSON list in the file (a string there is split, so a hand edit works), split on `sep` in the environment (`os.pathsep` by default, `sep=","` for names), with `item` normalising each entry. `get_list` returns it, and `all()` and `GET /api/settings` return it as JSON. `get` and `set` show it joined, and `save` takes a list or a joined string.

A setting that takes one of a fixed set has `choices=[m.value for m in Mode]` (or `normalise=one_of(...)`): a value is matched without regard to case and stored as the choice is spelled, anything else is refused with the list, `set` shows the choices, and the schema in `/api/openapi.json` has an `enum`, so the web app's generated type needs no narrowing by hand. `Settings(..., numbers=True)` also sends `whole_between` and `number_between` settings as JSON numbers, with `minimum` and `maximum` in the schema; it changes what `GET /api/settings` sends, so turn it on with the web app's change. `PUT /api/settings` takes numbers either way.

To keep the values in the app's database instead of the file, add the table to the models and bind at service start, after the migrations. The file's values are copied in the first time, and the CLI and tests without a database keep using the file:

```python
from strata_python.db.settings import TableStore, settings_table

SETTINGS_TABLE = settings_table(Base.metadata)  # in models.py, so the migrations create it
SETTINGS.bind(TableStore(engine, SETTINGS_TABLE))  # any object with load() and update(changes) is a store
```

### 4. The server

```python
from strata_python import web


def create_app():
    app = web.base_app("myapp", __version__, SETTINGS)  # GET /api/health, GET and PUT /api/settings, /api/docs
    # ... the app's own /api routes ...
    web.add_web(app, STATIC_DIR, LANDING_HTML)  # last: /assets, top-level files, and index.html at every other path
    return app
```

Two more that replace code most servers carry:

```python
web.add_error_handlers(app)  # ValueError 400, PermissionError 403, KeyError and FileNotFoundError 404,
# tasks.Conflict 409, client.Rejected 502, client.Unavailable 503: {detail, payload}
web.add_system(
    app,
    __version__,
    commit=web.source_commit(__file__),
    folders={"data": lambda: SETTINGS.get("data_dir")},
    services={"strata-models": "http://127.0.0.1:8366"},
)  # GET /api/system, a SystemInfo
```

The app's own `/api/system` fields go in `extra=`, a function returning a mapping or a model. Give their model (`extra_model=AppNumbers`, or annotate the function `-> AppNumbers`) and the route's response is `<App>SystemInfo`, `SystemInfo` plus those fields, so the web app's type is `Schema<'MyappSystemInfo'>` with nothing added by hand.

Delete the app's `guard()` (or each route's `try/except` raising `HTTPException`), passing its own exceptions as `add_error_handlers(app, {BookLocked: 423})` (a status that depends on the cause is a function: `{SourceError: lambda e: 502 if source.configured() else 503}`), and its `/api/system` route. Give `base_app` the same `commit=` so `/api/health` says which source the server runs. Declare a route's answer with a shape from `strata_python.contracts` (`response_model=Page[Task]`) where the app already returns that shape, so the web app's types can come from `/api/openapi.json`.

Delete the app's own `/api/health` and `/api/settings` routes, its `/assets` mount, SPA fallback, `with_base` and `static_file` copies and its `uvicorn.run` wrapper. `web.serve(app, argv, default_port=...)` replaces the last of these. For health fields of the app's own, pass `health=lambda: {"gpu": gpu_name()}`: they follow `ok` and `version`, and may set `ok` to false. It runs on every request, so keep it cheap.

### 5. The CLI

```python
import argparse

from strata_python import cli, web


def main(argv=None):
    parser = argparse.ArgumentParser(prog="myapp")
    sub = parser.add_subparsers(dest="cmd", required=True)
    cli.add_doctor(sub, "myapp", __version__, ("fastapi", "uvicorn"))  # versions, and whether each import works
    cli.add_set(sub, SETTINGS)  # myapp set KEY [VALUE|default]
    cli.add_serve(
        sub, lambda host, port: web.serve(create_app(), ["--host", host, "--port", str(port)]), default_port=8000
    )
    # ... the app's own subcommands, each with set_defaults(fn=...) ...
    args = parser.parse_args(argv)
    return args.fn(args)
```

An app with a web front end on strata-react adds `cli.add_openapi(sub, create_app)`: `myapp openapi` writes `web/openapi.json` (or `-o PATH`, `-o -` for stdout) for `strata-api-types`, in a stable form (sorted keys, indent 1, a final newline, nothing from the machine), and `myapp openapi --check` exits 1 when the committed file is out of date. Its unit test is `strata_python.testing.assert_openapi_current(create_app, "web/openapi.json")`. Build response models on `contracts.ApiModel` so a field with a default is not optional (`?`) in the generated types.

Delete the hand-written `doctor`, `set` and `serve` subparsers. Each app's port belongs in its deploy units, not in this library.

A doctor that needs more than imports takes labels, optional modules, checks and its own hint, and prints every app's rows in the same columns:

```python
cli.add_doctor(
    sub,
    "myapp",
    __version__,
    ("fastapi", ("rawpy (DNG decoding)", "rawpy"), cli.Module("ultralytics", optional=True)),
    checks=[cli.Check("ffmpeg", cli.program("ffmpeg", "apt install ffmpeg")), cli.Check("gpu", gpu_check)],
    hint="uv sync (scripts/setup_env)",
)
```

A check returns `(status, text)` with status `ok`, `missing`, `slow` (works, but not as it should; never fails) or `broken`, and an exception counts as `broken`. Doctor exits 1 when a required module or check is missing or broken. A check that may be missing without that being a fault is `Check(..., optional=True)`.

### 5b. The strata-models client (apps that use the model service)

Drop the `strata-models-client` path or git dependency and change the import. The functions, their arguments and the exceptions are the same:

```python
from strata_python.clients import models as sm  # was: import strata_models_client as sm

page = sm.OcrPage.from_json(sm.ocr("/path/page.png", app="strata-scan"))  # typed: page.lines[0].words[0].box
talk = sm.Transcript.from_json(sm.transcribe("/path/talk.wav", app="strata-360"))
with sm.gpu_lease("strata-stills", vram_gb=4):
    run_my_own_cuda_code()
```

The shortcuts still return the service's JSON, so existing code keeps working, and the result classes keep all of it in `.raw`. There is one shortcut for every role the service runs (`sm.ROLES`): `faces`, `face_kind`, `transcribe` (also `role="transcribe-fast"`), `ocr`, `speak`, `denoise_audio`, `audio_quality`, `generate_image`, `embed`, `speaker_embeddings`, `enhance_video` and `shot_boundaries`, plus `ask` for the language-model roles (`vision-language`, `text`, and the judges). Each has a result class (`Answer`, `GeneratedImage`, `Embeddings`, `SpeakerEmbeddings`, `FaceKinds`, `CleanAudio`, `AudioQuality`, `EnhancedVideo`, `Shots` and the four above). In tests, the `fake_models` fixture (section 7) stands in for the service, so the app's tests need neither the service nor a GPU. The roles `models.toml` lists without a runtime yet (sound-events, detect-people, upscale, denoise, nsfw) have none: `run(role, params, app)` reaches them once the service serves them.

An app that speaks fixed lines (prompts, feedback) keeps each one as a clip made once by the `speak` role, with the browser's voice only as the fallback (`strata_python.speech`, issue #73):

```python
from strata_python.clients import models as sm
from strata_python.speech import ClipStore, add_clip_routes, build

store = ClipStore(os.path.join(SETTINGS.home(), "voice"))  # keep it out of git if it holds recordings
built = build(
    store, lines(), app="myapp", voice="bf_emma", params={"language": "en-gb", "seed": 0},
    per_line=lambda line: None if needs_a_person(line) else {},  # None: not made, listed in built.skipped
    now=sm.role_settings("speak"),  # remakes a clip when the role's model or settings change
)
add_clip_routes(app, store, "/api/v1/voice", recordable=needs_a_person)
```

`GET /api/v1/voice` is `ClipsOut` (line -> a relative clip URL for strata-react's clip list), `GET .../clips/<name>` the WAV with an immutable cache header, `PUT .../recordings?line=` a person's WAV (`ClipOut`, kept over any made clip, also when `build` is running at the time) and `DELETE` forgets it. Which lines exist and how a sentence is cut into lines stay in the app.

### 6. OS helpers

Delete the app's `oslib.py` and import from `strata_python.oslib`: `file_lock(f)`, `pid_alive(pid)`, `kill_tree(pid, sig)`, `lower_priority(nice, io=False)` (`io=True` for idle disk priority too; drop the app's `ionice` helper), `venv_python(venv)` and `cli_command(module)`. `cli_command` now takes the module name (`cli_command("myapp")`). Local `alive(pid)`, `_alive` and `lower_priority()` copies go too. `detached_kwargs()` starts a process outside the server's session and `load_average()` replaces 360's copy. listory's `job_process`, `start_job`, `watch_supervisor`, `terminate_job_group` and `kill_job` are `start_supervised(cmd, env=)`, `watch_supervisor()`, `terminate_own_group()` and `kill_group(proc)`; with the queue's `Worker` an app needs none of them directly.

### 6b. Background work

Replace the app's job dicts and loops with `strata_python.tasks`:

- An in-process run per key ("scan this folder") is `SingleFlight`: `FLIGHTS.start(key, fn)` raises `Conflict` (a 409 with `add_error_handlers`) when that key is running; `fn(flight)` checks `flight.stopping`; `FLIGHTS.get(key).as_json()` is the status. Delete the app's `*_JOBS` dicts, their locks and their stop flags.
- Work that must outlive a restart is `Detached(cmd, status_path, log)`: `start()`, `read()`, `stop()`. The child writes its own progress and end state with `StatusFile.from_env().update(...)`. Delete the app's `write_status` copies and its pid bookkeeping.
- A loop every N seconds is `Ticker(every_s, fn, name=...)`. A `fn` that returns a number chooses the next wait in seconds (`None` keeps `every_s`), `ticker.poke()` runs it now, and `ticker.next_at` says when it runs next. Delete the hand-written `while not stop.wait(...)` and `wake.wait(WAITS[why])` threads.
- Progress from deep inside a task is `report_progress(done, total, text)`; the runner binds a `ProgressReporter` (at most one write a second). Retry rules are `next_state(attempts, error, progressed=...)`: delete the app's own backoff.
- Jobs that must survive a restart, retry and be listed are the durable queue (with the db extra). Add `JOBS = jobs_table(Base.metadata)` to the models and a revision, then:

```python
from strata_python.tasks.queue import Job, Queue, Worker

QUEUE = Queue(engine, JOBS, on_fail=cascade)  # on_fail(conn, job, error), in the failing transaction
QUEUE.enqueue("ocr", {"page": 3}, domain="book:12", dedupe=True, resources=["gpu"], timeout_s=600)
WORKER = Worker(
    QUEUE, {"ocr": ocr_page}, concurrency=2
).start()  # a job process per thread, reused; recovers dead workers' jobs
QUEUE.page(state="running", after=cursor)  # Page[Task] for GET /api/jobs
QUEUE.cancel(job_id)
QUEUE.retry(job_id)
QUEUE.prune(older_than_s=30 * 86400)
```

  A handler is a module-level `fn(job) -> result` that reads `job.options` and reports progress. By default jobs run in job processes (`python -m strata_python.tasks.child`, one per worker thread, kept between jobs and importing the handlers by name), so a cancel, a timeout or `WORKER.stop()` kills the job and every tool it started, and jobs die with the server; a quick job costs about half a millisecond more than on a thread (3.5 ms against 2.9 ms a job on SQLite), since only a kill starts a new process. `max_jobs=N` replaces a process after N jobs, for code that leaks. The worker checks at start-up that every handler can be imported. `Worker(lower_priority=True)` runs the jobs at idle CPU and disk priority. `Worker(isolate=False)` runs handlers on the server's threads instead, where they return early when `job.stopping`. In tests, `QUEUE.step(handlers)` claims and runs one job in the test's own thread.

- Work in stages over one subject (a book's ingest, a tape's derived assets, a picture's develop) is a `strata_python.workflow.Pipeline`. Each stage is a function of a `Context` returning JSON, with a `version` (bump it when it would answer differently), the `keys` of the inputs it reads and the stages it `needs`; a `gate` is a person's approval, bound to the answers it saw. Delete the app's stage loop, its "is this stage done" checks, its cache-key function, its approval signatures and its cascade on clear:

```python
from strata_python.workflow import Context, Pipeline, gate, step
from strata_python.workflow.store import DbStore, enqueue, workflow_table

STAGES = workflow_table(Base.metadata)  # models.py, with a revision

INGEST = Pipeline(
    "ingest",
    [
        step("inventory", inventory, version=3, keys=("folder",), resources=("disk",)),
        step("metadata", metadata, needs=("inventory",), keys=("providers",)),
        gate("book", needs=("metadata",), auto=lambda c: policy.passes(c.output("metadata"))),
        step("plan", make_plan, needs=("book", "metadata"), version=2),
    ],
)


def advance_ingest(job: Job) -> dict:  # module level: HANDLERS = {INGEST.kind: advance_ingest}
    book = load(job.options["subject"])
    store = DbStore(engine(), STAGES, INGEST)
    return INGEST.advance(store, job.options["subject"], inputs_of(book), book, stopping=lambda: job.stopping).as_json()


enqueue(QUEUE, INGEST, "book:12")  # one job per subject; holds the stages' resources
INGEST.approve(store, "book:12", "book", inputs, by=user)  # then enqueue again
INGEST.clear(store, "book:12", "metadata")  # it and what depends on it run again
INGEST.status(store, "book:12", inputs).as_json()  # {state, waiting, failed, stages: {name: state}}
```

  A run that lives in memory (stills develops one picture in one go) passes `MemoryStore()` or its own `Store` (any class with `load(subject)` and `save(subject, record)`), `Pipeline(json=False)` so a stage can hand the next one a picture as it is, and `Pipeline(key=fn)` where the app's keys already name its files (`fn(pipeline, stage, inputs, needs)`, which must change when a need's `digest` does).

  The routes are the same in every app, so strata-react can draw them without app glue: `tasks.web.add_jobs(app, lambda: QUEUE)` adds `/api/jobs` (a `Page[Task]` filtered by state, kind and domain; counts; one job; cancel; retry), and `workflow.web.add_workflow(app, INGEST, store=..., inputs=inputs_of, queue=lambda: QUEUE)` adds `/api/workflows/ingest/subjects[/{subject}[/run | /stages/{stage}[/approve | /clear]]]`, answering `WorkflowRun` (each stage's `StageRun`: state, whether it is a gate, its last `StageEvent`; and the `Task` advancing it, found by the job's domain `ingest:<subject>`). `inputs(subject)` raises KeyError for a subject the app does not have (a 404). The queue is how work runs (retry, resources, cancel, progress); the pipeline is what has been done to a subject. A workflow job's progress names the stage it is on, and its result is the `Outcome`.

  A stage that raises is recorded as failed, the stages that do not need it still run, and the error is raised at the end, so the job retries with the queue's backoff and the next attempt starts at the failed stage. A row with a life of its own (a plan proposed, approved, running) is a `StateMachine`; `check(frm, to)` refuses a wrong move with a sentence and `workflow.store.move(conn, MACHINE, table, where, "running")` claims it with one conditional UPDATE.

### 6c. Files, web APIs, media, pictures and rankings

Each of these replaces code that two to six apps wrote for themselves; move to it when you next touch that code.

- **Writes.** Every `tmp = path + ".tmp"; ...; os.replace(tmp, path)` becomes `write_json_atomic(path, data)` (or `write_text_atomic`, `write_bytes_atomic`, `with atomic_open(path, "wb") as f:`). A tool that writes the file itself writes to `with atomic_path(out) as tmp:`; the temp name keeps the extension, so `ffmpeg ... tmp` still picks the format. Reads that tolerate a missing or broken file are `read_json(path, default)`. Delete the app's `_save`, `write_status` and `.part` helpers.
- **Hashes and keys.** `sha256(path)` (bytes, for a `LargeBinary` column) and `sha256_hex(path)` replace listory's two copies, filecat's and stills' scripts'. `quick_fingerprint(path)` is 360's `clips.fingerprint` idea on SHA-256 (an app that stored the old 12-character SHA-1 keeps its own until it re-keys). A cache that must follow a file's changes keys on `stat_key(path, px, version)`.
- **Paths from a request.** `files.inside(root, name)` before opening a file a request names; it raises `ValueError` (400 with `add_error_handlers`) for `..`, an absolute path elsewhere, or a link out of the root.
- **Other people's APIs.** One `Http` per service, at module level so its gap is shared:

```python
from strata_python.files import DiskCache
from strata_python.http import Http

NOMINATIM = Http(
    "myapp/1.0 (reverse geocoding of my own tracks)",
    min_interval=1.1,
    throttle_file=cache_dir / ".nominatim",
    cache=DiskCache(cache_dir),
)
reply = NOMINATIM.get(URL, params={"lat": lat, "lon": lon, "format": "json"}, cache_s=30 * 86400)
if reply.ok:
    place = reply.json()  # reply.status == 0 when unreachable; reply.limited when rate limited
```

  The User-Agent names the app and what it is for, never a person. Delete the app's `_throttle`, `_retry_after`, `_cool_until`, `_open`/`_get` wrappers and its response cache; listory's `providers.base.Http` keeps its own `emit` through `observe=`. A provider result worth keeping (not an HTTP body) is `DiskCache(...).json(key, fetch, max_age_s)`.
- **ffprobe and ffmpeg.** `media.probe(path)` replaces every hand-written `ffprobe -show_entries` call; read `info.duration`, `info.audio.sample_rate`, `info.video.fps`, `info.chapters`, `info.tags`, and `info.raw` for anything else. `media.ffmpeg(args)` adds `-nostdin -hide_banner -v error -y` and raises `MediaError` with ffmpeg's last lines; `progress=` reports seconds written. Map `MediaError` in `add_error_handlers` if the app answers it over HTTP. A doctor row is `cli.program("ffmpeg")`. An app with its own setting for the program (`app set ffmpeg /path`) calls `media.configure(ffmpeg=lambda: settings.get("ffmpeg"))` once at start and drops its own `subprocess` call; the setting is read on every run, and `media.available()` uses the same lookup for its doctor row.
- **Thumbnails** (`strata-python[images]`): `images.thumbnail(src, px, cache_dir=settings.thumbs_dir())` returns the cached JPEG's path for a `FileResponse`. An app with its own reader (stills' raw previews, 360's video frames) passes `make=lambda path, px: ...` returning a Pillow image. Bump `version=` when the way a thumbnail is made changes.
- **Rankings and evals.** `stats.bradley_terry(votes, side=True)` with votes as `(a, b, "a" | "b" | "tie")` (or `stats.votes_from(judgments)`), `.ratings()` for the Elo scale, `stats.intervals(votes)` for how far to trust a gap; `wilson`, `mcnemar` and `holm` for eval write-ups. The numpy bootstraps over items in strata-models' evals stay there until a second app needs them.

### 7. Tests

The pytest plugin loads itself once strata-python is installed. Configure it in `pyproject.toml`:

```toml
[tool.pytest.ini_options]
strata_env_prefix = "MYAPP"                    # MYAPP_HOME and MYAPP_DATA point into a fresh folder for every test
strata_tuning_vars = ["MYAPP_WORKERS"]         # removed for every test: what a developer sets to try things
strata_env = [                                 # set for every test; {env} is that folder
    "MYAPP_SWEEP_SECONDS=0",
    "MYAPP_PROJECTS={env}/projects",
]
```

Then delete these from the app:

- `tests/utils/fakes.py`, or keep only the doubles that are the app's own (a fake model, a fake yt-dlp).
- The `fake_urlopen`, `fake_popen` and `fake_run` fixtures, and any `no_sleep`.
- `pytest_collection_modifyitems`, which added the `integration` marker. The plugin registers the marker and adds it under `tests/integration`.
- The `isolated_env` (or `isolated_home`) and `no_network` fixtures. The app's extra variables move to `strata_env`. Keep a small autouse fixture only for what is not a variable, such as patching a module default.
- The integration `cli` and `cli_subprocess` fixtures. They become short fixtures in `tests/integration/conftest.py`:

```python
from pathlib import Path

import pytest

from strata_python.testing import run_cli, run_module

SRC = str(Path(__file__).parents[2] / "src")


@pytest.fixture
def cli():
    from myapp import cli as app_cli

    return lambda *args, check=True: run_cli(app_cli.main, *args, check=check)


@pytest.fixture
def cli_subprocess(isolated_env):
    return lambda *args, check=True: run_module("myapp", *args, src=SRC, check=check)
```

What every test gets:

| Fixture | Use it for |
|---|---|
| `isolated_env` (autouse) | Home, app home and data in a fresh folder, tuning variables removed, `strata_env` set. Returns the folder |
| `no_network` (autouse) | An outbound connection fails the test; loopback is fine |
| `http_server` | **First choice for HTTP**: a real server on loopback. `http_server.route("GET", "/items/1", {"id": 1})`, `once=True` for a sequence, then `http_server.requests`. A request no route answered fails the test |
| `live_app` | `with live_app(create_app()) as base:` runs the app on a real port for a client under test |
| `fake_urlopen` | Many quick cases at a web API's edge: `fake_urlopen.reply(http_error(429), {"ok": 1})`, then `fake_urlopen.calls[0].url`, `.method`, `.headers["user-agent"]`, `.body` (parsed JSON) |
| `fake_popen` | Code that starts a long-running process: nothing starts; `fake_popen.instances[-1].cmd` and `.kw`; set `.returncode = 0` to finish it |
| `fake_run` | Code that runs a program: `fake_run.returns["ffprobe"] = b"..."`, `fake_run.code = 1`, then `fake_run.calls` |
| `no_sleep` | Retries and backoff without waiting: `no_sleep.delays` |
| `fake_models` | An app that calls strata-models: a stand-in service on a real port with `STRATA_MODELS_URL` set, so `strata_python.clients.models` runs unchanged. `fake_models.results["ocr"] = {...}` (or a function of the params), `.fail("CUDA out of memory", role="faces")`, `.busy = True` (leases wait), `.down = True` (503s), then `.jobs[0]["params"]` |

`strata_python.testing` holds the classes behind them (`FakeResponse`, `http_error`, `FakeUrlopen`, `FakePopen`, `FakeRun`, `Sleeper`, `FakeModels` and its example replies `MODELS_RESULTS`) for a test that needs one directly. Prefer the real thing: `http_server` over `fake_urlopen`, the actual program over `fake_run`. A fake is for a true external edge where speed matters.

### 8. Database (apps with one)

Replaces the git-pinned database package, with the same behaviour under new names.

```toml
# pyproject.toml
dependencies = ["strata-python[db]>=0.2,<0.3"]   # SQLAlchemy 2, Alembic, psycopg 3

[tool.strata-db]                 # was the old package's own table: rename it, the keys are the same
app = "myapp"                    # owner role and schema (lower case, digits, _)
models = "myapp.models:Base"
migrations = "migrations"        # the app's Alembic revisions, unchanged
# engine = "postgres"            # only for an app that uses Postgres-only features
```

- Imports: the old package's names come from `strata_python.db` (`connect`, `session`, `NAMING_CONVENTION`, `Project`, `upgrade`, `drift`, `check_migrations`, `LiveDatabaseRefused`); the standard tests from `strata_python.db.testing`. The naming convention is byte-identical, so existing revisions and databases do not drift.
- Models: write the base class out, not `Base = make_base()`, which ty rejects as a base (`unsupported-base`) and so fails the gate:

  ```python
  from sqlalchemy import MetaData
  from sqlalchemy.orm import DeclarativeBase, MappedAsDataclass
  from strata_python.db import NAMING_CONVENTION


  class Base(MappedAsDataclass, DeclarativeBase):
      metadata = MetaData(naming_convention=NAMING_CONVENTION)
  ```
- Environment: `STRATA_DB_LIVE=1` in the service unit (next to the old variable until every app has moved), and `STRATA_DB_DATABASE` naming the shared database. It has no built-in name any more, because that name is the owner's and this package is public; set it once where the services get their environment. `STRATA_DB_URL` and `<APP>_DATABASE_URL` work as before. Tests and CI: `STRATA_DB_TEST_ADMIN_URL`, `STRATA_DB_TEST=postgres`.
- Commands: `strata-db revision -m "..."`, `strata-db check`, `strata-db migrate --live`, `strata-db current --live`, `strata-db new-app`, `strata-db init` (or `python -m strata_python.db`).
- At service start (only there): `Project.load(__file__).upgrade(connect("myapp"))`, or `Project.load(__file__).open(default=local_url(SETTINGS))`, which connects and migrates, falling back to `<settings home>/myapp.db` when no database is configured (the folder is made).
- An app installed as a wheel (a service from `uv tool install`) has no pyproject.toml beside its code. `strata-db init --package src/myapp` declares the keys in `src/myapp/strata-db.toml` instead, with `migrations/` beside it, and puts `[tool.strata-db] file = "src/myapp/strata-db.toml"` in pyproject.toml. Both ship in the wheel, `Project.load(__file__)` finds the file by walking up from the module, and strata-db and the pytest plugin follow the pointer, so there is one declaration and no drift test. To move an existing app, move the keys and the `migrations` folder into the package and leave `file = ...` in the table.
- A FastAPI server's records:

  ```python
  from strata_python.db import Project, local_url
  from strata_python.db.web import DbSession, add_database

  add_database(app, Project.load(__file__), default=lambda: local_url(SETTINGS))


  @app.get("/api/notes")
  def notes(db: DbSession) -> list[NoteOut]: ...
  ```

  The engine is opened and migrated on the first request that needs one (a server nobody records on never creates a database), kept on `app.state` and disposed when the server stops, after the app's own lifespan. A failure to open is that request's error and is tried again on the next. Delete the app's `db.py` fallback, its session dependency and the conftest that disposed the engine.
- Writes strata-react's `createOutbox` sends (issue #74): each carries a client-made id and is retried until the server answers, so take it once:

  ```python
  from strata_python.db import insert_once, lock_row

  lock_row(db, Learner, a.learner_id)  # serialise one learner's writes (FOR UPDATE on Postgres, the write lock on SQLite)
  row, duplicate = insert_once(
      db, Attempt, a.event_id,
      make=lambda: judge(db, a),  # runs only for an id not seen yet
      same=lambda old: (old.choice, utc(old.at)) == (a.choice, a.at),  # what the client sent
  )
  if not duplicate:
      db.commit()
  ```

  A retry with the same contents gets the stored row back (`duplicate=True`, nothing written, the lock let go); one with other contents is `ConflictingWrite`, a 409 through `add_error_handlers`. `lock_row` returns the row read under the lock, or raises `KeyError` (404). Replace a hand-written duplicate check and no-op `UPDATE`.
- Tests: the plugin is registered with every install and does nothing until the extra is installed and the project has `[tool.strata-db]` (or `strata_db = true` in `[tool.pytest.ini_options]`; `strata_db = false` turns it off). So an app can take a 0.2.x release before it moves its database, even with the old database package still installed. Then `db_session`, `db_engine`, `db_url` (a URL for code that builds its own engine), `db_env` (points the app's own `connect("myapp")` at the test database; make it autouse in `conftest.py` for an app that connects by itself), `db_backend`, `db_project`. Migrations run once into a template and each test gets a copy, so a test costs a file copy. `pytest --pg` (or `--strata-pg`, which no other plugin can take) runs the same tests on a private Postgres the run starts for itself (needs the PostgreSQL server binaries), or on `STRATA_DB_TEST_ADMIN_URL`. Add the standard tests once:

  ```python
  # tests/unit/test_database.py
  from strata_python.db.testing import test_migrations, test_schema_matches_models  # noqa: F401
  ```

  Never build a test schema with `Base.metadata.create_all()`: the tests would pass with broken migrations. And never write a guard of your own: under pytest every psycopg connection to a database that is not a throwaway `strata_db_*` one fails with `LiveDatabaseRefused`, in the test process and its subprocesses.
- CI: run the tests with `--pg` as well for an app that deploys on Postgres, since SQLite hides Postgres-only behaviour (types, defaults, locking). The fixtures need the PostgreSQL server binaries on the runner (`sudo apt-get install -y postgresql` if they are missing; this repo's `ci.yml` shows the step), or `STRATA_DB_TEST_ADMIN_URL` pointing at a service container.

Delete: the git source for the old package, its `[tool.*]` table, any home-made "unset the URL" or "block the database" fixture, and any `create_all()` in tests.

### Done when

- `grep -rn "def fake_urlopen\|def no_network\|def isolated_env\|pytest_collection_modifyitems\|class FakeRun\|uvicorn.run\|/api/health" src tests` finds nothing the app still needs.
- The app has no `oslib.py`, `settings.py` with file handling, `fakes.py` copy or `scripts/check.py`.
- `scripts/check` and the integration tests pass, and the app's changelog names the strata-python version it moved to.

### Keep it current

Read the `Unreleased` section and each new version in [CHANGELOG.md](CHANGELOG.md) when dependabot raises a bump. A `### Breaking` line ends with what to change. When the app finds code it shares with another app, it belongs here: open a PR on this repo instead of copying it.

## Develop

`uv sync`, then `scripts/check` (add `--fast` to skip the tests, `--fix` for safe ruff fixes). Integration tests (`uv run pytest tests/integration`) run the gate on a tiny project, `serve` in a separate process and `strata-db` as a command. The database tests use a throwaway Postgres: the private server needs the PostgreSQL server binaries on the machine (initdb refuses to run as root), or point `STRATA_DB_TEST_ADMIN_URL` at a throwaway server. Read [AGENTS.md](AGENTS.md) before changing anything; every change gets a [CHANGELOG.md](CHANGELOG.md) line for the apps and a [DEVLOG.md](DEVLOG.md) entry for us. Merging to main releases: `release.yml` bumps the version through its own PR, tags it and creates the GitHub release, and `pypi-publish.yml` uploads it to PyPI.
