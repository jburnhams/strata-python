# Changelog

What each release adds or changes for an app that depends on strata-python, newest first. Every breaking change says what to do in one line. Versions follow [semver](https://semver.org); while the version is 0.x a minor release may break, so depend on `strata-python>=0.N,<0.N+1`. Why each change was made is in [DEVLOG.md](DEVLOG.md).

## Unreleased

## 0.2.14 (2026-11-24)

### Added

- `wav.measure(data_or_path)` returns `WavLevels` (`peak_db`, `clipped`, `noise_db`, `speech_db`, `snr_db`, `seconds`) of a 16- or 24-bit PCM WAV: the levels a studio judges a take of speech by. Replace an app's own sample loop.
- `strata_python.wer`: `word_errors(said, heard)` gives a transcript's word error rate against the text meant, with the words missed and the alignment; `normalise(text)` is how both are read. Use it to check a recording against its prompt.
- `testing.sine_wav(..., lead=, tail=, noise=)`: quiet before and after the tone and a seeded noise under it; an amplitude above 1 is cut at full scale (a clipped take).

## 0.2.13 (2026-11-24)

### Added

- `media.configure(ffmpeg=..., ffprobe=...)`: an app with its own setting for the tools says where they are, as a path or a function read on every run (`media.configure(ffmpeg=lambda: settings.get("ffmpeg"))`), so `probe`, `duration`, `ffmpeg`, `derive` and `available()` run the program the setting names; when it gives nothing the environment and the PATH apply as before, and a setting that names no runnable program is a `MediaError` that says so (#60). Replace an app's own `subprocess` call kept for its ffmpeg setting with `media.ffmpeg`.

## 0.2.12 (2026-11-24)

## 0.2.11 (2026-11-24)

### Added

- `strata_python.derive`: the files an editor shows, made from footage with ffmpeg. `proxy(src, out, *, height=540, gop=1/2, crf=23, interlace=None, info=None, progress=None, timeout=None)` writes an H.264/AAC MP4 for the browser and returns its `Probe`: interlaced footage deinterlaced for display (bwdif, one frame per frame), square pixels, a keyframe every `gop` seconds, the source's frame rate and times, never larger than the source. `thumbnails(src, folder, *, every=10, height=180, ...)` returns `Thumb(time, path)` JPEGs, the first frame at or after each multiple of `every` (`time` is that multiple, exact), the last stretch of the file included. `peaks(src, *, per_second=50, rate=8000)` returns `Peaks` (`min`, `max` from -1 to 1, `to_json()`), the pair strata-react's `Waveform` takes, read as it is decoded. `interlacing(path)` is "tff", "bff" or "progressive", from the stream's field order or ffmpeg's `idet` when the stream does not say interlaced. `RECIPE` holds each recipe's version for an app's cache key.
- `media.ffmpeg(args, sink=callable)` hands stdout to `sink` in chunks as ffmpeg writes it, for output too big to hold (decoded audio of a long tape).

## 0.2.10 (2026-11-23)

### Added

- `settings.positive(hi=None)` and `number_between(lo, hi=None, lo_open=False)`: a number that must be more than 0 with no natural floor (a cache size, a half-life) says "must be more than 0" and is `exclusiveMinimum` in the schema with `numbers=True` (#53). `hi` is optional on `number_between` and `whole_between` too, for a limit with no ceiling. Replace a hand-made `number_between` subclass with `positive()`.
- `tasks.Ticker`: a `fn` that returns a number chooses the wait before its next run (`None`, or no return, keeps `every_s`); `poke()` runs it now; `next_at` is the `time.time()` of the next run (#54). Replace a hand-written `while not stopping: wake.wait(WAITS[step()])` loop.
- `web.add_error_handlers`: a status may be a function of the exception, `{SourceError: lambda e: 502 if source.configured() else 503}`, answered with the same `ErrorBody` (#55). Replace handlers added with `app.add_exception_handler` for that.

### Fixed

- `Settings`: a JSON number in the settings file (or the store) is read for a `number_between` or `whole_between` setting, and a JSON `true`/`false` for a `boolean` one, so a file an app wrote before it moved onto `Settings` keeps its values (#52). A stored or environment value that is ignored is now logged once as a warning. Drop a `JsonFileStore` subclass that turned numbers into text.

## 0.2.9 (2026-11-23)

## 0.2.8 (2026-11-23)

### Added

- `strata_python.wav`: `read_wav(data_or_path, *, channels=1, widths=(2, 3), min_rate=16_000, min_seconds=0.0, max_seconds=None)` returns a `WavInfo` (`sample_rate`, `sample_width`, `channels`, `frames`, `seconds`) or raises `WavError` (a `ValueError`, answered as a 400) with a sentence, for an app that takes recorded speech (strata-react's `useRecorder` uploads). Replace the app's own `wave` header check.
- `strata_python.testing.sine_wav(seconds, *, rate, width, channels, hz, amplitude)`: real PCM WAV bytes for an app's upload tests.
- `clients.models.run_with_provenance(...)` returns `(result, Provenance)`: the job's `model`, the load `options` it ran with, its `settings_hash` and `job_id`; `Provenance.from_job(row)` reads the same from any job row. `clients.models.role_settings(role)` returns `RoleSettings(model, settings_hash)`. Key a result cache on `(model, settings_hash)` so it is made again when strata-models changes a model's options under the same key (strata-models #103; the hash and options are None from a service older than that). `FakeModels.options[role]` sets the stand-in's load options, and its jobs and registry carry the same fields.

## 0.2.7 (2026-11-23)

### Added

- `strata_python.mediatime`: exact time for media. `rational(...)` (seconds as a `Fraction` from an int, "p/q" or decimal text, a Decimal, or a float by its shortest repr), `Rate` (`parse("30000/1001")`, `frame_at`, `time_of`, `snap`), `Span` (half-open `[start, end)`: `duration`, `in`, `&`, `overlaps` (an empty span overlaps nothing), `shift`, `frames(rate)`), `Piece` and `TimeMap` (an edit's output time to its sources and back: `sequence(ranges)`, `to_source(t)`, `to_output(source, t)`), all with exact JSON (`text`, `to_json`, `from_json`).
- `web.add_system(..., extra_model=AppNumbers)` (or `extra` annotated `-> AppNumbers`, which may return the model): the route's response is `<App>SystemInfo`, `SystemInfo` plus the app's fields, so they are in `/api/openapi.json` and the generated types (#46). Replace the hand-written `Schema<'SystemInfo'> & {...}` with `Schema<'<App>SystemInfo'>`. Without a model, `extra` works as before.

### Changed

- The response contracts (`Page`, `Health`, `SystemInfo`, `Folder`, `ServiceStatus`, `ErrorBody`, `Task`, `Progress`) are built on `ApiModel`, so a field with a default is required in the response schema: `Page.next` is `string | null` in the generated types, not `next?` (#45). Drop the app's `page.next ?? null` and similar fill-ins. An app that takes one of them as a request body gets `-Input` and `-Output` schemas for it.

## 0.2.6 (2026-11-23)

### Added

- `strata-public-check` reads web source (`.ts`, `.tsx`, `.mts`, `.cts`, `.js`, `.jsx`, `.mjs`, `.cjs`, `.mdx`, `.css`, `.html`, `.svg`, `.vue`, `.env.example`) and skips `storybook-static/` and `baselines/` (#37). Remove the app's own privacy scan of its front end.
- `[tool.strata-public-check] deny = ["\\bdate of birth\\b", {pattern = "...", kind = "age"}]`: case-insensitive patterns committed in the repo, reported under their kind with the match shown, skipped by an exclude and by `strata-public-check: ignore` like the built-in ones (#38). A bad entry exits 2.
- `cli.add_openapi(sub, create_app)`: `myapp openapi [-o PATH|-] [--check]` writes `web/openapi.json` for strata-react's `strata-api-types`, via `web.openapi_text(app)` (sorted keys, indent 1, final newline); `testing.assert_openapi_current(create_app, path)` is the unit test; `contracts.ApiModel` keeps defaulted response fields required in the schema (#36).
- `Setting(..., choices=[...])` and the `one_of(choices)` normaliser: case-insensitive, stored as the choice is spelled, refused with the list; `set` shows the choices and `/api/openapi.json` types the setting as an `enum`. `Settings(..., numbers=True)` sends `whole_between`/`number_between` settings as JSON numbers with `minimum`/`maximum` in the schema (off by default). `PUT /api/settings` takes numbers. `Settings.allowed(key)` (#41).
- `strata_python.db`: a `strata-db.toml` inside the package (`strata-db init --package src/myapp`, with `[tool.strata-db] file = ...` pointing at it) so `Project.load(__file__)` works from an installed wheel (#40); `local_url(SETTINGS)` and `url_for(app, default=...)` for a SQLite fallback when nothing is configured; `Project.open(url=None, default=...)` connects and migrates; `connect` makes a SQLite file's folder (#39).
- `strata_python.db.web`: `add_database(app, project, default=...)` opens and migrates the engine on the first request that needs it and disposes it when the server stops; `DbSession` is the per-request session dependency (#39).

### Changed

- `strata-public-check` on a folder inside a git checkout reads only the files git would commit (`git ls-files -co --exclude-standard`), so ignored tool output such as `.coverage` is not scanned; a folder git ignores (`dist`) is still read in full when named. A file with no suffix that holds a NUL byte is skipped as binary (#37).
- `whole_between` and `number_between` are now small callable classes with `lo`/`hi` (they used to be closures); calling them is unchanged.

## 0.2.5 (2026-11-23)

### Added

- `strata_python.files`: `atomic_path(path)` (a temp path beside the file, keeping its extension so ffmpeg can write to it, replaced on success and removed on error), `atomic_open`, `write_text_atomic`, `write_bytes_atomic`, `write_json_atomic(path, data, indent=1)`, `read_json(path, default)` (missing or broken file: the default), `sha256`/`sha256_hex`, `quick_fingerprint` (size and both ends of a big file), `stat_key(path, *extra)`, `inside(root, path)` (a path from a request, refused with `ValueError` if it leaves the root) and `DiskCache` (bytes or `json(key, fetch, max_age_s)` on disk with an age). Replace the app's `tmp` + `os.replace` writes, its sha256 helpers and its response caches.
- `strata_python.http.Http(user_agent, ...)`: the polite client for public web APIs. A gap per host across threads (`min_interval`) and processes (`throttle_file`), 429 and 503 waiting for Retry-After (seconds or a date, capped at `max_wait`), a `cool_down` for a host still saying 429, retries of 5xx and network errors only for idempotent requests (`idempotent=True` for a read sent as POST), `max_bytes`, `https_only`, an optional `DiskCache` (`cache_s=`; 200 and 404 kept), and `observe(reply, attempt)`. Errors come back as a `Reply` (`ok`, `status` 0 when unreachable, `error`, `limited`, `cached`, `json()`, `text()`); `raise_for_status()` and `get_json()` raise `HttpError`. Replace the app's own throttle, Retry-After and cache code.
- `strata_python.media`: `probe(path)` returns a `Probe` (duration, format, size, bit rate, tags, `streams` as `Stream`, `chapters`, `video`/`audio`, ffprobe's `warnings`, `raw`); `duration(path)`; `ffmpeg(args, input=, timeout=, progress=)` with `-nostdin -hide_banner -v error -y`, raising `MediaError` with the tail of ffmpeg's stderr; `tool(name)` honours `STRATA_FFMPEG` and `STRATA_FFPROBE`; `available()`. Replace the app's ffprobe and ffmpeg wrappers.
- `strata_python.images` and the `[images]` extra (Pillow): `thumbnail(src, px, cache_dir=, quality=, version=, make=)` returns the path of a cached upright JPEG (keyed on the file, the size, the quality and `version`; never enlarged; one maker per file at a time and `slots` at once); `open_upright`, `flatten`, `jpeg_bytes`. Replace the app's thumbnail cache.
- `strata_python.stats`: `bradley_terry(votes, side=)` (a `Fit` with `strengths`, the side advantage, `ratings()` on the Elo scale and `p_beats`), `intervals` (bootstrap over votes), `head_to_head`, `votes_from(judgments)`, `wilson`, `mcnemar`, `holm`. Pure Python. Replace the arena's and the evals' copies.

### Changed

- `tasks.write_json_atomic` is now `files.write_json_atomic` (the same import still works). It also fsyncs before the replace, writes non-ASCII characters as they are instead of `\u` escapes, and takes `indent=`.

## 0.2.4 (2026-11-23)

### Fixed

- `oslib.lower_priority(nice)` sets the nice value to `nice` (never lower than the current one) instead of adding it, so `lower_priority(19)` gives 19 from a process that starts below 0. An app that worked around it with its own `setpriority` can drop that.
- `pages.render.rasterise(pdf, dpi=page.dpi)` comes back exactly the scan's size: the page's points times `dpi / 72`, rounded. pdfium rounded up, so some sizes came back a pixel wider or taller. An app that padded or cropped the raster to the scan's size can drop that.

## 0.2.3 (2026-11-14)

## 0.2.2 (2026-11-14)

### Added

- `strata_python.contracts`: the shapes the apps' APIs share, as pydantic models to declare as a route's `response_model` (so `/api/openapi.json` describes them and a web app's types can be generated): `Health`, `SystemInfo` (with `Folder`, `ServiceStatus`), `ErrorBody`, `Task` with `TaskState` and `FINISHED`, `Progress`, `Page[T]` (cursor paging), `FileRef`, `Judgment`, and `utc(epoch)`. Times are aware datetimes, sent as ISO 8601.
- `strata_python.client.ServiceClient(base_url, app=...)`: the standard-library base for calling a strata service: `get`/`post`/`put`/`delete` returning parsed JSON, `Rejected` for a 4xx (`.status`, `.detail`, `.payload`) and `Unavailable` for an outage or a 5xx, reads retried with a doubling pause (POSTs never), `wait(path, done)` long-polling with `?wait=`, `health()` and `available()`, and `env_url("MYAPP_URL", default)` reading the URL on each call. `X-Strata-App` and the User-Agent name the calling app. `clients.models.Unavailable` is now a `client.Unavailable`.
- `web.add_error_handlers(app, {MyError: 423})`: the app's exceptions answered as `ErrorBody` responses (`{detail, payload}`); the defaults map `ValueError` to 400, `PermissionError` 403, `KeyError` and `FileNotFoundError` 404, `tasks.Conflict` 409, `client.Rejected` 502 and `client.Unavailable` 503. Delete the app's `guard()`.
- `web.add_system(app, version, commit=, folders=, services=, extra=)`: `GET /api/system` as a `SystemInfo` (version, commit, Python, platform, start time, each folder with its disk's free and total bytes, each service's health probed in parallel). Delete the app's own route.
- `base_app(..., commit=web.source_commit(__file__))`: `/api/health` answers `commit`, read from `.git` without running git (worktrees and packed refs included).
- `Settings.model()`: the settings as a pydantic model (`<App>Settings`), which `base_app` declares as the settings routes' response, so `/api/openapi.json` now names each setting and its type.
- `strata_python.tasks`: `next_state`, `backoff_s`, `RetryLater` and `PROGRAMMING_ERRORS` (retry rules: bugs fail at once, outside failures back off, progress resets the count); `report_progress` with `ProgressReporter` and `reporting` (throttled to one write a second, stage changes and the last step always written); `SingleFlight` and `Flight` (one in-process run per key, `Conflict` on a second start, cooperative `stopping`); `StatusFile` and `Detached` (a process outside the server's session, its state in an atomic JSON file that reads `died` when its pid is gone; the child finds the file in `STRATA_STATUS_FILE`; writes and merges hold `StatusFile.locked()` across processes, so none is lost); `Ticker` (a function every N seconds that survives its own errors and stops at once); `write_json_atomic`.
- `oslib.stdin_from_devnull()` (an empty stdin for the process and its tools; returns the old one) and `oslib.stdout_to_stderr()` (moves stdout for the process and the tools it starts, the Win32 handle too, and returns the old one). `oslib.start_supervised`, `watch_supervisor`, `terminate_own_group` and `kill_group`: listory's per-job process groups. A child started with a lifeline on its stdin ends its whole group when the parent dies; `kill_group` stops it and every tool it started. Replaces listory's `job_process`, `start_job`, `watch_supervisor`, `terminate_job_group` and `kill_job`.
- `strata_python.tasks.queue` (needs `[db]`): a durable job queue in the app's own database, SQLite or Postgres. `jobs_table(metadata)` for the app's models; `Queue(engine, table)` with `enqueue` (priority, `delay_s`, `dedupe`, `resources`, `timeout_s`, `domain`), `claim`, `heartbeat`, `report`, `finish`, `fail` (the `tasks.retry` rules; `on_fail(conn, job, error)` runs in the transaction that fails a job for good), `cancel`, `retry`, `get`, `page` (`Page[Task]`, newest first, cursor paging), `counts`, `recover` (jobs whose worker died are retried; each claim carries a token, so a slow worker whose job was claimed again cannot write over the new attempt), `prune`, and `step(handlers)` for tests; `Worker(queue, handlers, concurrency=)` runs them on threads with heartbeats, cooperative cancel and timeouts, and releases running jobs back to the queue on `stop()`. By default jobs run in a job process per worker thread (`tasks.child`), kept between jobs so a quick job costs well under a millisecond more than on a thread: cancel, timeout and `stop()` kill the job and the tools it started (the next job gets a new process), and jobs die with the server. Handlers must be module-level functions (checked when the `Worker` is made); `max_jobs=N` replaces a process after N jobs, `isolate=False` keeps handlers on threads, and `lower_priority=True` runs the processes at idle priority. Replaces an app's own queue and its job dicts that must survive a restart.
- `oslib.detached_kwargs()` (Popen arguments for a process outside this one's session or console) and `oslib.load_average()`.
- The `fake_models` fixture and `strata_python.testing.FakeModels`: a stand-in strata-models on a real loopback port with `STRATA_MODELS_URL` pointing at it, so an app's own `strata_python.clients.models` calls run in its tests without the service or a GPU. Script `fake_models.results[role]` (a reply or a function of the params; `MODELS_RESULTS` has one in the service's shape per role), `.fail(error, role=...)`, `.busy` (leases stay queued), `.down` (503s), and assert on `.jobs`, `.leases`, `.heartbeats`, `.released`. Delete the app's own stand-in.
- `strata_python.pages` and the `[pdf]` extra (ReportLab, pypdfium2, Pillow): a scanned page described as blocks, lines, styled spans and figures in scan pixels (`Page`, `Block`, `Line`, `Span`, `Figure`, JSON with `to_dict`/`from_dict`), drawn as a PDF with real, embedded fonts by `pages.render.render(page, fonts)`, and read back with `rasterise(pdf, page, dpi)` (greyscale pixels at the scan's size) and `read_text(pdf)`. A span takes a font matcher's per-word fit as it is (`x`, `baseline_y`, `size_pt`, `tracking_em`, `hscale`, `stroke_em`); small capitals are synthesised unless the face is a small-caps cut; the same input gives the same bytes. For apps that rebuild scanned books, and for scoring a rebuild against the scan with the very renderer that prints it.
- `strata_python.clients.models`: the strata-models client, standard library only, with the same functions as `strata_models_client` (`run`, `submit`, `wait`, `wait_job`, `faces`, `transcribe`, `ocr`, `speak`, `role_model`, `acquire`, `gpu_lease`, `Unavailable`, `JobFailed`), now typed, plus a shortcut for each of the other roles the service runs (`ask` for `vision-language`, `text` and the judges; `generate_image`, `embed`, `face_kind`, `denoise_audio`, `audio_quality`, `enhance_video`) and a result shape for every role (`OcrPage`, `Transcript`, `Faces`, `Speech`, `Answer`, `GeneratedImage`, `Embeddings`, `FaceKinds`, `CleanAudio`, `AudioQuality`, `EnhancedVideo`; `.from_json`, with everything the service sent in `.raw`). `ROLES` lists them. To move: replace `import strata_models_client as sm` with `from strata_python.clients import models as sm` and drop the client's path or git dependency.
- Typed settings: normalisers `number_between(lo, hi)`, `whole_between(lo, hi)`, `iso_date(allow_blank=True)` and `boolean`, and getters `get_int`, `get_float`, `get_bool`. Replace an app's own range and date normalisers and its `int(SETTINGS.get(...))` calls.
- `ListSetting(key, default=..., item=..., sep=os.pathsep, env=...)`: a list of strings, a JSON list in the file, split on `sep` in the environment, read with `get_list`. `all()`, `GET /api/settings` and `PUT /api/settings` carry it as a JSON list, and `set` shows and takes it joined. An app with a list of folders can now drop its own settings file handling.
- Settings stores: `Settings(..., store=...)` and `Settings.bind(store)` keep the values in any object with `load()` and `update(changes)`. On the first bind the JSON file's values are copied in once. `strata_python.db.settings` has the table every app shares (`settings_table(Base.metadata)`) and its `TableStore`, on SQLite and Postgres.
- `cli.add_doctor(..., checks=[...], hint="...")`: imports can be `(label, module)` pairs or `cli.Module(label, module, optional=True)`, and `cli.Check(label, fn)` rows (`fn` returns `(status, text)`; `ok`, `missing`, `slow` or `broken`) cover what an import cannot tell, with `cli.program("ffmpeg")` for a program on the PATH. Every app's rows line up the same. `cli.doctor(...)` runs the same thing directly. Replace an app's own `doctor` with these.
- `web.base_app(..., health=lambda: {...})` adds the app's own fields to `GET /api/health` after `ok` and `version`; it may set `ok` to false. Replace an app's own health route with it.
- `oslib.lower_priority(nice, io=True)` also lowers disk priority for batch jobs that read many large files: the idle I/O class on Linux (children inherit it), background mode on Windows, I/O throttling on macOS. Replace the app's own `ionice -c3 -p <pid>` helper with it.
- `strata-public-check` takes `[tool.strata-public-check] exclude = [...]` in pyproject.toml and `--exclude GLOB` to skip the built-in patterns in matching files (an sdist's copy matches too), and skips the built-in patterns on a line that says `strata-public-check: ignore`. Neither silences `STRATA_PUBLIC_DENY`.

### Changed

- `Settings.all()` is typed `dict[str, str | list[str]]`, since a `ListSetting` is a list. It is unchanged for an app with only `Setting`s, but its type checker may now ask it to narrow a value it passes on as a `str`.
- The documented way to declare an app's models base is now the class written out (`class Base(MappedAsDataclass, DeclarativeBase): metadata = MetaData(naming_convention=NAMING_CONVENTION)`, README step 8), because ty rejects `Base = make_base()` as a base class and fails the gate. `make_base()` still works and stays for code that needs a base at run time; an app that uses it for its models should switch.

### Fixed

- The `[pdf]` extra needs Pillow 13 (`pillow>=13,<14`): 12.x has eight published advisories, most of them in PDF parsing, and `uv audit` fails an app that locks it. Run `uv lock --upgrade-package pillow`.
- `strata-public-check` no longer reads three-part versions (`10.4.2` in package.json and package-lock.json) as private IPv4 addresses: only a whole four-part address counts. An app with a web front end can now run it in CI.
- The db pytest plugin no longer stops an app's tests from running when the old database package is installed too ("option names {'--pg'} already added"). It now switches on only for an app that uses `strata_python.db`: the db extra installed and a `[tool.strata-db]` table in pyproject.toml, or `strata_db = true` in `[tool.pytest.ini_options]` (`strata_db = false` turns it off). SQLAlchemy and Alembic brought in by another package no longer switch on its guard. Until it is on it adds no `--pg`; once on, `--pg` is ours unless another plugin has it, and `--strata-pg` always works. Drop the old database package once the app has moved to `strata_python.db`.

## 0.2.1 (2026-11-14)

### Added

- `strata_python.testing`: the test doubles every app copied into `tests/utils/fakes.py` (`FakeResponse`, `http_error`, `FakeUrlopen`, `FakePopen`, `FakeRun`, plus `Sleeper`), and CLI runners `run_cli(main, *args, check=True)` and `run_module(module, *args, src=..., check=True)` returning `CliResult(stdout, stderr, code)`. Delete the app's `fakes.py` and import from here.
- Plugin fixtures `fake_urlopen`, `fake_popen`, `fake_run` and `no_sleep`, so the app's conftest no longer defines them. `fake_popen` gives each test its own `.instances`. `FakeUrlopen.calls` entries are `Call` objects that still answer `call["url"]`.
- `strata_env = ["KEY=VALUE", ...]` in `[tool.pytest.ini_options]` sets the app's own variables for every test (`{env}` is the isolated folder), for what each app's `isolated_env` copy added.
- `strata-python[dev]`: ruff, pyright, ty, httpx and the pytest plugins at the versions `strata-check` is tested with. An app's dev group becomes `dev = ["strata-python[dev]"]`; delete those packages from it.
- `strata-python[db]` and `strata_python.db`: the shared-database code apps took from a git pin of the database package, now typed and released here. `connect`, `session`, `make_base` (the same naming convention, so existing revisions do not drift), `Project` from `[tool.strata-db]`, `upgrade`, `downgrade`, `drift`, `check_migrations`, `lint_revisions`, `stairway`, the live-database guard (`LiveDatabaseRefused`), scratch databases, a private throwaway Postgres for tests (now on Windows too), the `strata-db` command (`init`, `new-app`, `revision`, `migrate`, `check`, `current`; also `python -m strata_python.db`), the fixtures `db_engine`, `db_session`, `db_url`, `db_env`, `db_backend`, `db_project` with `--pg`, and the standard tests in `strata_python.db.testing`. The names are `STRATA_DB_*` (`URL`, `DATABASE`, `LIVE`, `TESTING`, `TEST`, `TEST_ADMIN_URL`, `ADMIN_URL`); throwaway databases are `strata_db_*`. To move: rename the app's table to `[tool.strata-db]`, import from `strata_python.db`, set `STRATA_DB_LIVE=1` and `STRATA_DB_DATABASE` in its service environment, and drop the git dependency (README, step 8).
- Tests under `tests/integration` get the `integration` marker automatically, and the marker is registered; delete the app's `pytest_collection_modifyitems`. Set `strata_auto_integration = false` to opt out.

### Changed

- The shared dependencies are capped below their next release that may break (`fastapi<1`, `uvicorn<1`, `pydantic<3`, `pytest<10`), so a breaking upstream release reaches the apps through a strata-python release, not on its own.
- `FakeRun.check_output` raises `CalledProcessError` when `.code` is set, as the real `check_output` does; the apps' copies returned the output.

## 0.2.0 (2026-11-14)

The first release on PyPI. Apps tracked a git commit before this.

### Changed

- **Install from PyPI.** Replace the `[tool.uv.sources] strata-python = { git = ... }` entry with the dependency `strata-python>=0.2,<0.3`, then run `uv lock`.

### Added

- `strata-public-check`: fails on email addresses, private IPv4 addresses, home-folder paths and the regexes in `STRATA_PUBLIC_DENY`, in a folder, an sdist or a wheel. Run it in a public app's CI.
- `Settings(..., prefix="MA")` names the environment variables an app already uses (from the pre-PyPI commits; listed here because no earlier release exists).
- The MIT licence.

## Before 0.2.0 (git commits only)

`oslib`, `settings`, `web`, `cli`, `http_server`, `live`, the pytest plugin (`isolated_env`, `no_network`, `http_server`, `live_app`) and `strata-check`, taken from python-app-template.
