"""pytest fixtures for apps with a database (strata_python.db). Registered with every strata-python install, inert
unless the db extra is installed: nothing here imports SQLAlchemy until a fixture runs.

  db_engine    an Engine on a fresh database with the app's migrations applied, per test
  db_session   a Session on it (closed afterwards)
  db_url       a URL to a fresh migrated database, per test, for code that makes its own engine (connect(url))
  db_env       for app code that connects by itself (connect("myapp")): points STRATA_DB_URL and <APP>_DATABASE_URL
               at db_url for the test. Make it autouse in the app's conftest.py to cover every test
  db_backend   "sqlite" or "postgres"
  db_project   the app's Project ([tool.strata-db] in pyproject.toml)

Default backend: SQLite. The migrations run ONCE per session into a template; each test gets a copy (in memory for
db_engine, a file for db_url), so a test costs a copy, not a migration.
`pytest --pg` (also `--strata-pg`; or STRATA_DB_TEST=postgres, or engine = "postgres" in the project) uses a real Postgres instead:
a template database is migrated once and each test gets `CREATE DATABASE ... TEMPLATE` (a file copy on the server,
milliseconds), dropped afterwards. That server is a throwaway one, never the real server: STRATA_DB_TEST_ADMIN_URL
if set (CI), otherwise a private server started for the run (strata_python.db.testserver).

When is it on? Only for an app that uses strata_python.db: SQLAlchemy and Alembic are installed (the db extra) AND
the project says so, by a [tool.strata-db] table in its pyproject.toml (found from pytest's rootdir upwards) or by
`strata_db = true` in its pytest ini options (`strata_db = false` turns it off whatever else is there). Having
SQLAlchemy and Alembic installed by some other package is not enough (issue #19): such an app, or one still on the old
database package, must be able to run pytest with this plugin registered and inert.

When it is on, the plugin switches on the test guard (strata_python.db.guard) in pytest_load_initial_conftests, before
conftest.py or any app module is imported (or, when it is loaded from a conftest.py itself, as in this repo's own
tests, in pytest_configure, before any test runs): for the whole run, every Postgres connection in the process (connect(),
create_engine(), psycopg.connect()) and in its subprocesses (STRATA_DB_TESTING=1) is refused unless it is a
throwaway strata_db_* database. A test that would reach the shared database fails with LiveDatabaseRefused instead.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from collections.abc import Iterator
from importlib.util import find_spec
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from . import guard

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import Session

    from .project import Project

TEST_BACKEND = "STRATA_DB_TEST"
INSTALLED = find_spec("sqlalchemy") is not None and find_spec("alembic") is not None
INI = "strata_db"
PG_DEST = "strata_db_pg"
PG_HELP = "run strata_python.db fixtures on a throwaway PostgreSQL"
_ACTIVE = pytest.StashKey[bool]()  # per Config, not a module global: pytester runs nested sessions in one process


def pytest_addoption(parser: pytest.Parser) -> None:
    # Only names of our own here: another plugin (the old database package) may also add --pg, and two addoption
    # calls for one name raise. --pg itself is added once we know the plugin is on (_activate).
    parser.addoption("--strata-pg", action="store_true", dest=PG_DEST, help=PG_HELP)
    parser.addini(INI, type="bool", default=None, help="turn the strata_python.db fixtures and test guard on or off")


def active(config: pytest.Config) -> bool:
    """Whether this run uses strata_python.db (see the module docstring)."""
    if not INSTALLED:
        return False
    wanted = cast("bool | None", config.getini(INI))
    if wanted is not None:
        return bool(wanted)
    from .project import find_table

    return find_table(config.rootpath) is not None


def _activate(config: pytest.Config, parser: pytest.Parser | None) -> None:
    if _ACTIVE in config.stash:
        return
    config.stash[_ACTIVE] = on = active(config)
    if not on:
        return
    guard.enter_test_mode()
    if parser is not None:  # still before the full command line is parsed, so --pg given there is recognised
        # ValueError: another plugin (the old database package) has --pg; it means the same, and db_backend reads it
        with contextlib.suppress(ValueError):
            parser.addoption("--pg", action="store_true", dest=PG_DEST, help=PG_HELP)


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(early_config: pytest.Config, parser: pytest.Parser) -> None:
    _activate(early_config, parser)  # entry point or -p: before conftest.py and the app's modules are imported


def pytest_configure(config: pytest.Config) -> None:
    _activate(config, None)  # loaded from a conftest.py (pytest_plugins): the hook above has already run


@pytest.fixture(scope="session")
def db_project(request: pytest.FixtureRequest) -> Project:
    from .project import Project

    if not request.config.stash.get(_ACTIVE, False):
        pytest.fail(
            "the strata_python.db fixtures are off: they need the db extra (strata-python[db]) and a [tool.strata-db]"
            f" table in pyproject.toml, or {INI} = true in the pytest ini options",
            pytrace=False,
        )
    return Project.load(request.config.rootpath)


@pytest.fixture(scope="session")
def db_backend(request: pytest.FixtureRequest, db_project: Project) -> str:
    cfg = request.config
    pg = cfg.getoption(PG_DEST) or cfg.getoption("--pg", default=False)  # ours, or another plugin's --pg
    pg = pg or os.environ.get(TEST_BACKEND) == "postgres" or db_project.postgres_only
    return "postgres" if pg else "sqlite"


@pytest.fixture(scope="session")
def _db_template(db_project: Project, db_backend: str) -> Iterator[str]:
    from sqlalchemy import create_engine

    from . import migrations as m
    from .engine import sqlite_pragmas
    from .scratch import create_pg, drop_pg, unique

    if db_backend == "postgres":
        name = unique(f"tmpl_{db_project.app}")
        eng = create_engine(create_pg(name))
        m.upgrade(eng, db_project.migrations_dir, db_project.metadata)
        eng.dispose()  # a template must have no open connections when it is copied
        yield name
        drop_pg(name)
    else:
        d = tempfile.mkdtemp(prefix="strata_db_")
        path = os.path.join(d, "template.db")
        eng = create_engine("sqlite:///" + path)  # no WAL: the template must be one self-contained file
        sqlite_pragmas(eng, wal=False)
        m.upgrade(eng, db_project.migrations_dir, db_project.metadata)
        eng.dispose()
        yield path
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def db_engine(_db_template: str, db_backend: str, db_project: Project) -> Iterator[Engine]:
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from .engine import sqlite_pragmas
    from .scratch import create_pg, drop_pg, sqlite_copy_to_memory, unique

    if db_backend == "postgres":
        name = unique(f"test_{db_project.app}")
        eng = create_engine(create_pg(name, template=_db_template))
        yield eng
        eng.dispose()
        drop_pg(name)
    else:
        mem = sqlite_copy_to_memory(_db_template)
        eng = create_engine("sqlite://", creator=lambda: mem, poolclass=StaticPool)
        sqlite_pragmas(eng, wal=False)
        yield eng
        eng.dispose()
        mem.close()


@pytest.fixture
def db_session(db_engine: Engine) -> Iterator[Session]:
    from sqlalchemy.orm import Session

    with Session(db_engine) as s:
        yield s


@pytest.fixture
def db_url(_db_template: str, db_backend: str, db_project: Project, tmp_path: Path) -> Iterator[str]:
    from .scratch import create_pg, drop_pg, unique

    if db_backend == "postgres":
        name = unique(f"test_{db_project.app}")
        yield create_pg(name, template=_db_template)
        drop_pg(name)
    else:
        path = tmp_path / "test.db"
        shutil.copyfile(_db_template, path)
        yield f"sqlite:///{path}"


@pytest.fixture
def db_env(db_url: str, db_backend: str, db_project: Project, monkeypatch: pytest.MonkeyPatch) -> str:
    """Points the app's own connect("<app>") at a fresh migrated test database for this test (STRATA_DB_URL and
    <APP>_DATABASE_URL). On Postgres the fixtures' tables are in public, so the URL says search_path=public."""
    from .config import URL, env_name

    url = db_url
    if db_backend == "postgres":
        url += ("&" if "?" in url else "?") + "options=-csearch_path%3Dpublic"
    monkeypatch.setenv(URL, url)
    monkeypatch.setenv(env_name(db_project.app), url)
    return url
