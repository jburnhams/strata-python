# Changelog

What each release adds or changes for an app that depends on strata-python, newest first. Every breaking change says what to do in one line. Versions follow [semver](https://semver.org); while the version is 0.x a minor release may break, so depend on `strata-python>=0.N,<0.N+1`. Why each change was made is in [DEVLOG.md](DEVLOG.md).

## Unreleased

### Changed

- The documented way to declare an app's models base is now the class written out (`class Base(MappedAsDataclass, DeclarativeBase): metadata = MetaData(naming_convention=NAMING_CONVENTION)`, README step 8), because ty rejects `Base = make_base()` as a base class and fails the gate. `make_base()` still works and stays for code that needs a base at run time; an app that uses it for its models should switch.

### Added

- `oslib.lower_priority(nice, io=True)` also lowers disk priority for batch jobs that read many large files: the idle I/O class on Linux (children inherit it), background mode on Windows, I/O throttling on macOS. Replace the app's own `ionice -c3 -p <pid>` helper with it.
- `strata-public-check` takes `[tool.strata-public-check] exclude = [...]` in pyproject.toml and `--exclude GLOB` to skip the built-in patterns in matching files (an sdist's copy matches too), and skips the built-in patterns on a line that says `strata-public-check: ignore`. Neither silences `STRATA_PUBLIC_DENY`.

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
