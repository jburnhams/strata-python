# strata-python

Common code for the strata Python web server apps (FastAPI + CLI), the Python counterpart of [strata-react](https://github.com/jburnhams/strata-react). Every app made from [python-app-template](https://github.com/jburnhams/python-app-template) used to carry its own drifting copy of these modules; they live here once. It is opinionated and built for those apps rather than as a general-purpose library, but it is public, MIT-licensed and on PyPI.

| Module | What it gives an app |
|---|---|
| `strata_python.oslib` | Cross-platform file locks, process liveness (`pid_alive`), `kill_tree`, `lower_priority`, `venv_python`, `cli_command(module)` |
| `strata_python.settings` | `Settings` / `Setting` / `ListSetting`: one JSON file in `~/.<app>` with `<APP>_*` environment overrides, validation (`absolute_path`, `non_empty`, `number_between`, `whole_between`, `iso_date`, `boolean`), typed getters, atomic saves, and `bind(store)` to keep them elsewhere (`strata_python.db.settings.TableStore`) |
| `strata_python.web` | `base_app` (health with the app's own fields + settings routes), `add_web` (built web app or stand-in page at every non-API path), `with_base` (reverse-proxy-safe `<base href>`), `static_file` (path-safe), `serve` |
| `strata_python.cli` | `add_doctor` (imports, labels, optional modules and the app's own `Check`s), `add_set`, `add_serve` for the app's argparse subcommands |
| `strata_python.clients.models` | The strata-models client, standard library only: `run`, `submit`, `wait`, `faces`, `transcribe`, `ocr`, `speak`, `role_model`, `gpu_lease`, and typed results (`OcrPage`, `Transcript`, `Faces`, `Speech`) |
| `strata_python.http_server` / `live` | A real scripted HTTP server and a real uvicorn server on loopback, for tests that make actual requests |
| pytest plugin | Loaded automatically: hermetic home and data folders (`strata_env` adds the app's own variables), a no-network guard, `http_server` and `live_app`, the `fake_urlopen`, `fake_popen`, `fake_run` and `no_sleep` fixtures, and the `integration` marker on everything under `tests/integration` |
| `strata_python.testing` | The fakes behind those fixtures (`FakeResponse`, `http_error`, `FakeUrlopen`, `FakePopen`, `FakeRun`, `Sleeper`) and the CLI runners `run_cli(main, *args)` and `run_module("myapp", *args, src=...)` |
| `strata_python.db` (extra `[db]`) | The shared database: `connect("myapp")` (the app's schema of one shared Postgres, or a SQLite path), a declarative base with the shared `NAMING_CONVENTION` for typed models, Alembic migrations with no per-app `env.py`, the live-database guard, the `strata-db` command, and the `db_*` fixtures (SQLite by default, a throwaway Postgres with `--pg`) |
| `strata-check` | The one gate: lockfile, ruff, `ruff format`, pyright strict, ty, unit tests with coverage |
| `strata-public-check` | Fails on email addresses, private IPv4 addresses, home-folder paths and the regexes in `STRATA_PUBLIC_DENY` in a tree, sdist or wheel; run before anything public is published. Silence a false positive with `[tool.strata-public-check] exclude = ["web/package-lock.json"]` (or `--exclude`), or `strata-public-check: ignore` on the line (never applies to `STRATA_PUBLIC_DENY`) |

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
        ListSetting("source_roots", item=absolute_path, env="MYAPP_SOURCES"),  # MYAPP_SOURCES split on os.pathsep
    ],
    prefix="MYAPP",  # only if the app already runs with other variable names
)

SETTINGS.get("greeting")  # MYAPP_GREETING, else the file, else the default
SETTINGS.save({"greeting": "Hi"})  # validated and written atomically; None resets to the default
SETTINGS.folder("data_dir")  # the value as a folder, created if missing
```

Values are canonical strings, and lists of them. Normalisers for the common kinds give one spelling each: `number_between(lo, hi)`, `whole_between(lo, hi)`, `iso_date(allow_blank=True)` and `boolean`, read back with `get_float`, `get_int` and `get_bool`. A `ListSetting` is a JSON list in the file (a string there is split, so a hand edit works), split on `sep` in the environment (`os.pathsep` by default, `sep=","` for names), with `item` normalising each entry. `get_list` returns it, and `all()` and `GET /api/settings` return it as JSON. `get` and `set` show it joined, and `save` takes a list or a joined string.

To keep the values in the app's database instead of the file, add the table to the models and bind at service start, after the migrations. The file's values are copied in the first time, and the CLI and tests without a database keep using the file:

```python
from strata_python.db.settings import TableStore, settings_table

SETTINGS_TABLE = settings_table(Base.metadata)        # in models.py, so the migrations create it
SETTINGS.bind(TableStore(engine, SETTINGS_TABLE))     # any object with load() and update(changes) is a store
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
from strata_python.clients import models as sm   # was: import strata_models_client as sm

page = sm.OcrPage.from_json(sm.ocr("/path/page.png", app="strata-scan"))   # typed: page.lines[0].words[0].box
talk = sm.Transcript.from_json(sm.transcribe("/path/talk.wav", app="strata-360"))
with sm.gpu_lease("strata-stills", vram_gb=4):
    run_my_own_cuda_code()
```

The shortcuts still return the service's JSON, so existing code keeps working, and the result classes keep all of it in `.raw`.

### 6. OS helpers

Delete the app's `oslib.py` and import from `strata_python.oslib`: `file_lock(f)`, `pid_alive(pid)`, `kill_tree(pid, sig)`, `lower_priority(nice, io=False)` (`io=True` for idle disk priority too; drop the app's `ionice` helper), `venv_python(venv)` and `cli_command(module)`. `cli_command` now takes the module name (`cli_command("myapp")`). Local `alive(pid)`, `_alive` and `lower_priority()` copies go too. Helpers this library does not have yet (process-group supervision, load average) stay in the app until they move here, and the app's file says so.

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

`strata_python.testing` holds the classes behind them (`FakeResponse`, `http_error`, `FakeUrlopen`, `FakePopen`, `FakeRun`, `Sleeper`) for a test that needs one directly. Prefer the real thing: `http_server` over `fake_urlopen`, the actual program over `fake_run`. A fake is for a true external edge where speed matters.

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
- At service start (only there): `Project.load(__file__).upgrade(connect("myapp"))`.
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
