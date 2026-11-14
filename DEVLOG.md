# Development log

Why each change was made and which files it touched, newest day first. For what changed in each release, as an app sees it, read [CHANGELOG.md](CHANGELOG.md).

## 2026-11-14

**Contracts, a service client base, web extras and the task primitives (branch `claude/project-thread-aefumh`)**

The owner asked for the rest of the library work in the move-out plan while the apps move onto 0.2.x. These are its steps 3 to 5 (web extras, contracts and `ServiceClient`, task primitives), designed from the survey's notes on each app's copy. The models client stays standard library only and keeps its own calls (decided earlier today); only its `Unavailable` now subclasses the shared one, so `add_error_handlers` answers it as 503.

- `src/strata_python/contracts.py` **new**: `Health`, `SystemInfo`, `Folder`, `ServiceStatus`, `ErrorBody`, `Task`, `TaskState`, `FINISHED`, `Progress`, `Page[T]` (written with `Generic[T]`, since 3.11 is supported), `FileRef`, `Judgment`, `utc`.
- `src/strata_python/client.py` **new**: `ServiceClient`, `Rejected`, `Unavailable`, `env_url`. Only idempotent methods retry, because a retried POST can submit a job twice.
- `src/strata_python/web.py`: `add_error_handlers` and `DEFAULT_ERRORS`, `add_system`, `source_commit`, `base_app(commit=)`, and the settings routes declare `Settings.model()` as their response. The PUT body stays an untyped dict on purpose: a typed body would answer an unknown key with 422 instead of the 400 apps and their tests expect.
- `src/strata_python/settings.py`: `Settings.model()` (pydantic imported inside it, so the CLI's settings reads stay light).
- `src/strata_python/tasks/` **new**: `retry.py` (after strata-360's `pipeline/retry.py` as the survey describes it: programming errors fail at once, progress resets the count, `RetryLater` names its pause), `progress.py`, `flight.py` (`SingleFlight`, `Flight`, `Conflict`), `status.py` (`StatusFile`, `Detached`, `write_json_atomic`; `Detached` reaps its own exited child, since a zombie still passes `pid_alive`), `ticker.py`, `__init__.py`.
- `src/strata_python/oslib.py`: `detached_kwargs`, `load_average`. listory's process-group supervisor is not here yet: its code was not read in this pass.
- `src/strata_python/clients/models.py`: `Unavailable` subclasses `client.Unavailable`.
- `tests/unit/test_contracts.py`, `tests/unit/test_client.py`, `tests/unit/test_tasks.py` **new**; `tests/unit/test_web.py` (error bodies, `/api/system` with a real service up, one down and one not ok, `source_commit` on a made-up checkout and worktree, the settings schema in OpenAPI), `tests/unit/test_oslib.py`. All over real HTTP, real threads, real files and real child processes.
- `README.md` (module table, step 4, step 6, new step 6b), `AGENTS.md` (layout), `src/strata_python/__init__.py`, `CHANGELOG.md`.


**A stand-in strata-models for the apps' tests (branch `claude/project-thread-aefumh`)**

The move-out plan's next step for the models client: every app that calls strata-models would otherwise write its own fake service or mock the client. The stand-in that tested the client here becomes public, so apps test their own calls against the same service steps over real HTTP.

- `src/strata_python/testing.py`: `FakeModels` (FastAPI, imported inside the constructor so importing the fakes stays light) with the job, lease, heartbeat, release, registry and health endpoints, scripted results and failures, `busy`, `refuse` and `down`; `MODELS_RESULTS`, the example replies that were in the test, with output paths under `/out/` (ruff's S108 refuses `/tmp` in `src`).
- `src/strata_python/pytest_plugin.py`: the `fake_models` fixture (served by `live_server`, `STRATA_MODELS_URL` set).
- `tests/unit/test_clients_models.py`: uses the fixture instead of its own `Service` class. `tests/unit/test_testing.py`: scripted results, a failure for one role, a missing job and lease, an unknown role, an outage, `running_polls=0`.
- `README.md` (module table, step 5b, fixture table), `CHANGELOG.md`.


**Pillow 13 as the `[pdf]` extra's floor (branch `claude/project-thread-aefumh`)**

The audit job failed on main after `strata_python.pages` landed: the lock held Pillow 12.3.0, which has eight advisories fixed in 13.0.0. Dependabot's PR #23 raised the cap and the lock; this raises the floor too, so an app that installs `[pdf]` cannot resolve to a vulnerable Pillow either.

- `pyproject.toml`: `pillow>=13,<14`. `uv.lock`.
- `CHANGELOG.md`.


**The strata-models client moves in as `strata_python.clients.models` (issue #18; branch `claude/project-thread-vzy9v6`)**

Apps installed strata-models' client from a path or a git URL into their own venvs. The library rules say a strata service's client belongs here. The owner chose standard library and dataclasses for it, with no pydantic and no `[models]` extra, because the client is called from code and does not validate requests. That replaces the morning's note about `strata_python.services.models` on pydantic.

- `src/strata_python/clients/__init__.py`, `src/strata_python/clients/models.py` **new**: the client's code as it is in strata-models' `client/` (same names, defaults, long-poll and lease steps), fully annotated, with typed result shapes for the roles apps call today: `ocr`, `transcribe`, `faces` and `speak`, read from the service's runtimes (`runtimes/tesseract.py`, `workers/whisper.py`, `runtimes/faces.py`, `workers/tts.py`). The shortcuts still return the JSON, so a move is one import line, and each shape keeps the whole reply in `raw`. The issue also named `image-describe`, `font-match` and `page-build`, but the service has no `image-describe` role (that is `vision-language`) and the other two do not exist yet, so they are left until it serves them.
- `tests/unit/test_clients_models.py` **new**: a stand-in service in FastAPI over real HTTP (`live_app`), covering the shortcuts' requests, the typed shapes, a failed job, a refused request, a 503, no service, a wait that times out, a lease with heartbeats and release, a queued lease withdrawn after its timeout, and `gpu_lease` with no service.
- `README.md` (module table, step 5b), `AGENTS.md` (layout), `CHANGELOG.md`.
- Every role the service runs, as the owner asked after the first version: the roles in `models.toml` whose runtime is in `runtimes.RUNTIMES` (16 of 22), with params and result shapes from each runtime's docstring (`runtimes/llama.py` and `runtimes/cloud.py` for `ask`, `sdcpp.py`, `clip.py`, `facekind.py`, `workers/audio.py`, `workers/audio_quality.py`, `workers/video.py`). The other six (sound-events, speaker-embedding, detect-people, upscale, denoise, nsfw) have models listed but no runtime (`transformers`, `speechbrain`, `ultralytics`, `spandrel`, `nudenet` are not in `RUNTIMES`), so they are left out until the service serves them. A test keeps `ROLES` and the stand-in's results the same set.
- Left for strata-models: test the client against its real API in its integration suite, then delete `client/`.


**Settings: typed values, lists and a pluggable store (issues #10, #16; branch `claude/project-thread-vzy9v6`)**

strata-stills could not adopt `Settings` because two of its settings are lists, and strata-models because its settings move into a database table and it has numbers, ranges and dates. Both kept a copy of `settings.py`. Typed int, float and bool settings were left as normalisers plus getters, not new kinds. The values stay canonical strings, so the file is still hand-editable and every existing app reads the same. Only lists needed a new kind.

- `src/strata_python/settings.py`: `number_between`, `whole_between`, `iso_date`, `boolean`; `get_int`, `get_float`, `get_bool`, `get_list` and `value`; `ListSetting`; `SettingsStore` (a protocol: `load`, `update`), `JsonFileStore`, which is the old file code moved as it was, lock and atomic replace included, and `Settings(store=)`, `Settings.file` and `bind(store, import_file=True)`, which copies the file's valid values in once and leaves a marker row. Each kind parses through one `parse` method, so the environment, the store and `save` agree on what a value is.
- `src/strata_python/db/settings.py` **new**: `settings_table(metadata)` (key, value as JSON text, the same on both backends) and `TableStore`, an upsert per key in one transaction so concurrent saves of different keys keep both.
- `src/strata_python/web.py`: the settings routes carry lists.
- `tests/unit/test_settings.py`: each normaliser and getter, lists from the file, the environment and `save`, a hand-edited list file, `bind` copying once and not again. `tests/unit/test_db.py`: the table store on SQLite and Postgres with concurrent saves. `tests/unit/test_web.py`: lists over real HTTP. `tests/unit/test_cli.py`: `set` on a list.
- `README.md` (module table, step 3), `AGENTS.md` (layout), `CHANGELOG.md`.


**Doctor checks and health fields an app can add (issues #15, #17; branch `claude/project-thread-vzy9v6`)**

strata-stills and strata-models each kept their own `doctor`, and strata-models its own `/api/health`, because the shared ones could not be extended. The two issues asked for the same doctor hook in two shapes (`(ok | None, line)` and a four-word status); this takes the four-word one, which says more, with `optional=` for the "not available, not a failure" case.

- `src/strata_python/cli.py`: `Module`, `Check`, `Status`, `program()` and `doctor()`; `add_doctor` takes `checks` and `hint`. Rows keep the existing columns (a failing row is upper case, as `MISSING` was), and a check that raises is a `broken` row rather than a crashed doctor. Shared GPU checks (torch present, a device, a kernel runs) are left for when an app moves its doctor here and shows the exact checks it needs.
- `src/strata_python/web.py`: `base_app(health=...)`, merged after `ok` and `version`, called per request.
- `tests/unit/test_cli.py`, `tests/unit/test_web.py`: labels, optional modules, each status, a crashing check, a missing program with its hint, `add_doctor` passing them through; health fields over real HTTP, including `ok` turned false.
- `README.md` (module table, steps 4 and 5), `CHANGELOG.md`.


**Models: declare the base class, since ty cannot subclass `make_base()` (issue #20; branch `claude/project-thread-vzy9v6`)**

strata-stills found that ty reports `unsupported-base` on `class Photo(Base)` when `Base = make_base()`, and the gate fails on ty's warnings. Reproduced with this repo's ty: a value typed `type[DataclassBase]` has no MRO ty can know. pyright accepts it, which is why PR #7's checks passed.

- Considered a shared base class to subclass instead (the issue's second option): it cannot work, because SQLAlchemy gives a registry and MetaData only to a *direct* subclass of DeclarativeBase, so every app's tables would share one MetaData. So the docs show the two-line class with `NAMING_CONVENTION` (the issue's first option) and `make_base()` stays for run-time use.
- `src/strata_python/db/models.py`, `src/strata_python/db/__init__.py`: docstrings show the class and say why.
- `tests/unit/test_db.py`: ty and pyright run on a models file written the documented way; both pass it, and both fail a misspelt constructor argument, so the check sees through the base.
- `README.md` (module table, step 8), `CHANGELOG.md`.


**`oslib.lower_priority(io=True)` (issue #12; branch `claude/project-thread-vzy9v6`)**

Three strata-stills scripts each carry `os.nice(15)` plus `ionice -c3 -p <pid>`; it moved the CPU half to `lower_priority` and kept the I/O half, which belongs here.

- `src/strata_python/oslib.py`: `io=True` calls `ioprio_set` (idle class) through `ctypes` on the Linux architectures in a small syscall table, falling back to `ionice` elsewhere or when refused; `PROCESS_MODE_BACKGROUND_BEGIN` on Windows (this process only: Windows does not pass background mode on); `setiopolicy_np` throttling on macOS, excluded from coverage since no CI leg counts it. All best effort, as the CPU part was.
- `tests/unit/test_oslib.py`: each path aimed at a child process (the internal helper takes a pid, so the test process keeps its own priority) and read back with `ionice`, the syscall path with `ionice` hidden so a fallback cannot pass for it; and the public call in a child, whose own child inherits the class. They failed before the change.
- `README.md` (step for `oslib`), `CHANGELOG.md`.


**Release workflow: check the token before pushing anything (issue #8; branch `claude/project-thread-vzy9v6`)**

The release after #7 pushed its bump branch and then failed at the first `gh` call with "invalid header field value for Authorization", most likely a trailing newline in the pasted `RELEASE_TOKEN` (git base64-encodes it, `gh` sends it raw). The secret was re-saved and 0.2.1 is on PyPI; this makes a bad token fail first, with a message saying what to fix.

- `.github/workflows/release.yml`: a first step that fails when the secret is empty or holds whitespace, then reads the repository with it. It does not trim the token, as the issue offered: the checkout and release actions take the secret as it is, so a trimmed copy would only move the failure later.


**`strata-public-check`: versions are not addresses, and a way to silence what is left (issue #13; branch `claude/project-thread-vzy9v6`)**

An app with a web front end got 28 findings, most of them npm versions (`10.0.0`, `10.4.2`) matched as private IPv4 addresses, because the pattern took two or three parts after the prefix. It also found real home-folder paths in two of that app's files, so the check is worth having there once it is quiet.

- `src/strata_python/public_check.py`: the address pattern takes exactly four parts and not inside a longer run of digits and dots; `exclude` globs from `[tool.strata-public-check]` in ./pyproject.toml and `--exclude`, matched against a path or its end (an sdist's member path carries the sdist's folder); `strata-public-check: ignore` on a line skips the built-in patterns there. Both deliberately leave the owner's `STRATA_PUBLIC_DENY` patterns alone, so an excluded file is still checked against them (a review finding on the first version, which skipped it whole): they live in a secret, and an exception for one would have to be written in the public repo.
- `tests/unit/test_public_check.py`: versions and longer dotted numbers, an address before a full stop, the ignore marker (and that it does not cover deny patterns), excludes from pyproject.toml, the command line and inside an sdist.
- `README.md` (the module table), `CHANGELOG.md`.


**The db pytest plugin switches on only for apps that use it, and its `--pg` no longer clashes (issues #11, #19; branch `claude/project-thread-vzy9v6`)**

Two apps reported that with 0.2.1 installed pytest would not start when the old database package was installed too: both plugins add `--pg`, and a second `addoption` for a name raises. The plugin also switched its guard on whenever SQLAlchemy and Alembic were importable, which refuses the old package's throwaway databases in the same run. So an app could not take 0.2.x first and move its database later, the order README step 8 suggests.

- `src/strata_python/db/pytest_plugin.py`: "on" now means the extra is installed and the project asks (`[tool.strata-db]` from the rootdir up, or the new `strata_db` ini option, which wins either way). The guard starts in `pytest_load_initial_conftests` (still before conftest.py and the app's modules; `pytest_configure` when the plugin is loaded from a conftest.py, as here) instead of at import. `pytest_addoption` adds only names of our own (`--strata-pg`, the ini option); `--pg` is added once on, after every entry-point plugin has added its options, and a `ValueError` there means another plugin owns `--pg`, whose value `db_backend` reads. The state is in `config.stash`, since pytester runs nested sessions in one process. A fixture used while the plugin is off fails saying what turns it on, rather than running unguarded.
- `src/strata_python/db/project.py`: `find_table()`, the standard-library half of `Project.load`, so the plugin can ask without SQLAlchemy.
- `pyproject.toml`: `strata_db = true` for this repo's own tests.
- `tests/unit/test_db.py`: a stand-in plugin that adds `--pg` first; `--pg` and `--strata-pg` offered once on; the plugin off (guard not started, drivers not wrapped, `--pg` left to the other plugin, fixtures failing with the reason) with no table and with `strata_db = false`, in a subprocess since the guard is per process. All four failed before the change.
- `README.md` (step 8, tests), `AGENTS.md` (the db plugin rule), `CHANGELOG.md`.

**Scanned pages drawn as PDFs: `strata_python.pages` (branch `claude/project-thread-dvu0cz`, issue #9)**

The owner wants evals of how close a rebuilt PDF of a scanned book page gets to the cleaned scan (layout, figures, typefaces), with strata-models owning the models and the scoring, and the rendering here so the apps that print rebuilt books (strata-scan first) draw with the code that was scored.

- `src/strata_python/pages/model.py` **new**: the description, plain dataclasses with no third-party import so an app can build and send one without the extra. Boxes are `(x, y, w, h)` in scan pixels like Tesseract's; styles, roles, alignments and figure kinds are closed sets checked on construction (`PageError`); `to_dict` leaves defaults out. `Span` carries a font matcher's per-word fit (`x`, `baseline_y`, `size_pt`, `tracking_em`, `hscale`, `stroke_em`), the shape agreed with the strata-models font-match role.
- `src/strata_python/pages/render.py` **new**: ReportLab writes (BSD), pdfium reads back (BSD/Apache); PyMuPDF was ruled out for its AGPL licence. One text object a run, because ReportLab's text cursor ignores tracking and horizontal scale, so positions are worked out here. A line is fitted to its box by glyph spacing only, at most 0.1 em a glyph, so a near face lands where the print was without stretching glyphs; placed words are never refitted. Small capitals are synthesised at 0.78 unless the face is a small-caps cut; extra weight is a stroke round the glyphs (render mode 2). `invariant=1` makes the bytes depend only on the input. Faces are registered once a process under a name from their path, because ReportLab's font registry is global. Both libraries are untyped, so they are reached through `Any`; this module's own signatures are complete (pyright strict, ty).
- `tests/unit/test_pages.py` **new**: real renders read back by pdfium (no mocks): ink inside the line box, fitting within its limit, alignment, size, baseline, tracking, scale, weight, placed words, small capitals, an embedded TrueType face (ReportLab's own Vera), figures, several pages, the text read back, and the description's JSON round trip and refusals.
- `pyproject.toml`: the `pdf` extra, in this repo's dev group so its tests run. `README.md`, `AGENTS.md`: module table and layout.

**The shared database code moves in as `strata_python.db` (branch `claude/project-thread-t4kvbv`)**

The owner asked whether the Python client in the database repo should move here, since the apps pull it from that repo by git commit, and agreed it was worth doing for the SQLite test databases as well. The inventory found eight apps on it at five different commits (four of them from before scratch databases moved off the live cluster), the names carrying a machine's name throughout (package, environment variables, database prefixes, the advisory-lock key, the pyproject table), untyped code, f-string SQL, and a test server that only ran on POSIX. The owner chose `STRATA_DB_*` and `[tool.strata-db]` as the new names.

- `src/strata_python/db/` **new**: `config`, `guard`, `engine`, `models`, `project`, `migrations`, `scratch`, `testserver`, `testing`, `cli`, `pytest_plugin`, `_alembic/env.py` and the revision template, ported and typed (pyright strict, ty). Behaviour is the same except for what follows.
- Names: `STRATA_DB_*` variables, `[tool.strata-db]`, `strata_db_*` throwaway databases, `strata-db-pg-*` server folders, the `strata_db` lock namespace, the `strata-db` command. **Correction to the plan posted to the owner:** it said the old names would keep working here. They cannot: they contain the machine's name, which rule 4 keeps out of this public repo. Apps rename when they move instead (README step 8), and the old package stays where it is for apps that have not moved.
- The shared database has no default name (it was the machine's name). `STRATA_DB_DATABASE` names it, `DatabaseNotConfigured` says what to set, and `new-app` takes `--database` or the variable. `refuse_real_server` checks only when a name is set; the guard keeps tests on throwaway names either way.
- Identifiers in DDL (schemas, roles, databases) go through the dialect's `quote_identifier`, not hand-written quotes.
- `testserver`: binaries also from `PGBIN` and the Windows install folder; on Windows the server listens on loopback on a free port (no Unix socket); the watchdog is a Python process using `oslib.pid_alive` instead of `sh` and `kill -0`, so it works on both.
- `pytest_plugin`: a second entry point (`strata_python_db`). It is registered with every install and imports SQLAlchemy only inside fixtures; the guard is switched on only when SQLAlchemy and Alembic are installed. `strata_python/db/__init__.py` loads its names on first use, so importing the guard needs nothing else.
- `make_base()` is typed as returning a dataclass declarative base (a `TYPE_CHECKING`-only class), so an app's `Note(text=...)` is checked against its annotations.
- Review findings fixed: a failed SQLite migration kept the tables it had created, since pysqlite begins a transaction only before DML (true of the old package too; `_run` now issues `BEGIN`); `new-app` run by a CREATEROLE login that is not superuser could not create the schema it owns, since that login gets only ADMIN on the role (it now grants itself membership). Both have a test that failed first. A third finding, that the plugin imports psycopg as it loads, is the guard working as designed: AGENTS.md now says so instead of claiming the plugin imports nothing.
- Left for later: strata-models' store helpers (`connect(migrate=...)`, `StaleDatabase`, `revisions`, `fast_commits`), which come with the strata-models client; the database repo's `app-db.yml` and docs, which belong to that repo.
- `tests/unit/test_db.py` **new** (the old suite, in-process with `run_cli`, plus the guard outside tests, async and psycopg2 connections, the fixtures on SQLite in this run and on Postgres through `pytester` three ways), `tests/integration/test_db_cli.py` **new** (the flows that rewrite models between steps, as `python -m strata_python.db`). The Postgres tests no longer skip silently: CI sets `STRATA_TEST_PG=1` on Linux and Windows, where a missing server fails them; they skip on macOS, which has no server binaries, and say why.
- `pyproject.toml`: the `db` extra (in this repo's dev group so its tests run), the `strata-db` script, the second plugin entry point (disabled in addopts like the first), `S608` allowed in tests (SQL naming generated test schemas), `__main__.py` omitted from coverage. `tests/conftest.py` loads both plugins.
- `.github/workflows/ci.yml`: PostgreSQL server binaries on Linux, `STRATA_TEST_PG`.
- `README.md` (step 8 and the module table), `CHANGELOG.md`, `AGENTS.md` (layout, plugins).

**A shared testing module and an app-facing changelog (branch `claude/project-thread-t4kvbv`)**

The owner asked for a test module so every app uses the same testing code, and for a changelog apps can read to see what each release adds. The survey found `tests/utils/fakes.py` in seven apps in five versions whose behaviour is the same (only docstrings, formatting and one app's torch mocks differ), the same three fixtures in every root conftest, the same integration auto-marker, and `cli`/`cli_subprocess` fixtures that differ only in a docstring.

- `src/strata_python/testing.py` **new**: the fakes, typed, with three fixes over the copies: `FakePopen.fresh()` gives each test its own `instances` (the copies shared a class list that each fixture reset); `FakeRun.check_output` raises on a non-zero `.code` like the real one; `FakeUrlopen.calls` holds `Call` objects that keep `call["url"]` working. `run_cli` and `run_module` replace the integration conftests' `cli` and `cli_subprocess`, with the app's `main` and module passed in. Torch and transformers mocks stay in the one app that uses them.
- Review findings fixed: `FakeResponse.read(n)` now consumes its body, so a chunked read loop reaches EOF (the copies returned the first chunk forever); the integration marker looks at the path relative to the pytest root, so a checkout under a folder called `integration` is not all marked.
- `src/strata_python/pytest_plugin.py`: `fake_urlopen`, `fake_popen`, `fake_run`, `no_sleep` fixtures; `strata_env` ini option (KEY=VALUE, `{env}` for the isolated dir), which covers what the apps' own `isolated_env` copies added; the `integration` marker registered and added under `tests/integration` (`strata_auto_integration`).
- `tests/unit/test_testing.py` **new**, `tests/unit/test_plugin.py`: the fakes through real `urllib` and `subprocess` calls; the runners against a real module; the new ini options in a child pytest.
- `CHANGELOG.md` **new** (app-facing, by version, `Unreleased` first) and `DEVLOG.md` (this file, the old `CHANGELOG.md` renamed): apps had only a dated development log to read. `release.yml` turns `## Unreleased` into the version heading in its bump PR and uses that section as the GitHub release notes; the sdist ships `CHANGELOG.md`.
- `pyproject.toml`: a `dev` extra with the tools and pytest plugins (this repo's own dev group is now `strata-python[dev]`, so it tests the same set the apps get); caps on the runtime dependencies. The owner asked whether the library could hold every app's dependencies so dependabot stops raising a PR per package per app: the shared ones live here now, but each app's `uv.lock` still needs its own bumps, so the README gives apps a dependabot config that groups them into one weekly PR. Exact pins (`==`) were rejected: every app would move in lockstep and one app's need would block the rest.
- `README.md`: rewritten as the guide to moving an app onto the library (asked for by the owner): dependency, gate, settings, server, CLI, oslib, tests, a done-when check and how to keep current, each step saying what the app deletes.
- `AGENTS.md`: the two logs, the testing module, and that additive releases are patches while 0.x (a "feat" commit would release 0.3.0, which apps capped at `<0.3` do not get).

**Publish to PyPI, licence, and an opinionated scope (branch `claude/project-thread-t4kvbv`)**

A survey of the strata apps found none pinning a tag (none exists; they track main at six different commits), and the owner asked to publish to PyPI and to make the library opinionated for the strata apps, with nothing personal or machine-specific in it since the repo is public. A PyPI upload is permanent, so the clean-up and a check come before the first release.

- `LICENSE` **new**: MIT, as the npm libraries use. The repo had no licence, so it was all rights reserved.
- `pyproject.toml`: version 0.2.0 (not 0.1.0, so the first release cannot be confused with the untagged commits apps use today); `license`, `license-files`, `authors` (a handle, no email), `urls`, `classifiers`; `hatchling>=1.27` for SPDX licences; an sdist `include` list, because Hatch put every tracked file (agent notes, CI, `uv.lock`) in it; the `strata-public-check` script.
- `src/strata_python/public_check.py` **new**, `tests/unit/test_public_check.py` **new**: `strata-public-check` scans folders, sdists and wheels for email addresses (noreply addresses allowed), private IPv4 addresses, home-folder paths (placeholder users allowed) and the owner's own regexes from `STRATA_PUBLIC_DENY`. Those come from a secret so the names never enter the repo, and their matches are printed as "(hidden)" because CI logs are public.
- `.github/workflows/release.yml` **new**: leaflet-node's release flow (bump PR, merge, tag, GitHub release) with `uv version` in place of `npm version`. With no tag yet the first release is exactly the version in `pyproject.toml`; dependabot's `update-type: version-update:semver-major` trailers are ignored when choosing the bump, since `\bmajor\b` matches them (leaflet-node's copy has the same problem).
- `.github/workflows/pypi-publish.yml` **new**: on a published release (or by hand for a tag), checks the tag matches the version, runs the gate and integration tests, builds, runs `strata-public-check` on the sdist and wheel, and uploads with PyPI trusted publishing.
- `.github/workflows/ci.yml`: runs `strata-public-check` on the tree and a fresh build.
- `AGENTS.md`: "Generic or not here" becomes "Opinionated, for the strata apps" (strata service contracts and clients belong here; naming an app is fine, a person or machine is not); rule 4 widened to names, emails and port maps, enforced by the check; apps depend on a version range; the release flow; layout.
- `README.md`: install from PyPI with a version range (it told apps to pin tag `v0.1.0`, which never existed); a git `rev` only to try an unreleased commit; `strata-public-check`; releases.
- `src/strata_python/__init__.py`: neutral module docstring (it named the owner); lists `public_check`.
- `CHANGELOG.md`: the 2026-11-09 entry no longer names the owner (correction for rule 4, not a change of fact).

## 2026-11-09

**`Settings(prefix=...)` (branch `claude/project-thread-iw7199`)**
An app that moves onto the library may already run as a service with short environment variables (`MA_*`); the default prefix (`MY_APP_`) would silently ignore them.

- `src/strata_python/settings.py`: optional keyword `prefix` names the environment variables; default unchanged. `tests/unit/test_settings.py`: test for it.

**Create the library (branch `claude/project-thread-iw7199`)**

The owner asked for a `strata-python` project holding the server code the template and the strata apps were duplicating (six drifted copies of `oslib.py`, five of `settings.py`, copies of the server skeleton, CLI commands, hermetic test fixtures and `scripts/check.py`). This is its first version, built from the template's typed modules and made generic. The apps and the template have not been moved onto it yet.

- `src/strata_python/oslib.py` **new**: from the template; `cli_command(module)` is now generic and no longer edits `PYTHONPATH`/`PYTHONWARNINGS` in the environment (the template's did, and assumed its own source layout).
- `src/strata_python/settings.py` **new**: `Settings`/`Setting` replace the per-app `settings.py` (fixed keys, `APP_*` variables, hand-written getters). Validation lives in the setting; a bad stored value falls back to the default instead of crashing. `save` holds a thread lock and a file lock around the whole read-merge-write and writes through a unique temp file (found in review: concurrent PUTs lost updates or hit a missing temp file), and an empty string is a value, not "unset".
- `src/strata_python/web.py` **new**: `with_base`, `static_file`, health and settings routes, `add_web` and `serve`, from the template's `server/app.py`. The settings routes are generic (`PUT /api/settings` takes any declared key; unknown key or invalid value is a 400).
- `src/strata_python/cli.py` **new**: `doctor`, `set` and `serve` subcommands.
- `src/strata_python/http_server.py`, `live.py`, `pytest_plugin.py` **new**: the scripted loopback HTTP server, a real uvicorn-on-loopback helper, and the pytest plugin (hermetic environment, no-network guard, `http_server` and `live_app` fixtures). The template's `fake_urlopen`, `fake_popen` and `fake_run` fixtures are deliberately **not** carried over (see AGENTS.md, Testing policy).
- `src/strata_python/check.py` **new**: `strata-check`, the template's `scripts/check.py` with configurable paths. `--fix` applies safe fixes only.
- `pyproject.toml`, `uv.lock`, `scripts/check`, `.github/workflows/ci.yml`, `.github/dependabot.yml`, `AGENTS.md`, `CLAUDE.md`, `README.md` **new**: uv, ruff + `ruff format` at 120, pyright strict and ty, CI on Linux (Python 3.11-3.13), macOS and Windows.
- CI gates the coverage floor on Linux only: oslib has a branch per platform, so each other OS covers a different half of it.
- Tests: 59 unit tests (96.6% coverage; Windows-only branches excluded, and `kill_tree`/`file_lock`/`lower_priority` are exercised with real processes) and 5 integration tests (the gate on a tiny project, `serve` in a separate process).
