# Changelog

What each release adds or changes for an app that depends on strata-python, newest first. Every breaking change says what to do in one line. Versions follow [semver](https://semver.org); while the version is 0.x a minor release may break, so depend on `strata-python>=0.N,<0.N+1`. Why each change was made is in [DEVLOG.md](DEVLOG.md).

## Unreleased

## 0.2.2 (2026-11-14)

### Added

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
