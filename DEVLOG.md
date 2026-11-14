# Development log

Why each change was made and which files it touched, newest day first. For what changed in each release, as an app sees it, read [CHANGELOG.md](CHANGELOG.md).

## 2026-11-14

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
