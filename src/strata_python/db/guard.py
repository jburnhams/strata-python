"""Keeping tests and stray processes off real data.

Two rules:

1. **Tests never open a real database.** During a test run (pytest with the db extra installed, which loads the
   plugin; or `STRATA_DB_TESTING=1`, which the plugin sets so that subprocesses inherit it; or `PYTEST_CURRENT_TEST`)
   the only Postgres databases that can be opened are throwaway ones: names starting `strata_db_` (what the fixtures,
   `scratch_database()` and `strata-db`'s scratch runs create) and the admin database they are created through
   (`STRATA_DB_TEST_ADMIN_URL`). Anything else raises `LiveDatabaseRefused`, whatever the environment says, including
   `STRATA_DB_LIVE`. Under pytest this is enforced on every psycopg (and psycopg2) connection in the process, so it
   also covers code that calls `create_engine()` or `psycopg.connect()` itself, not only `connect()`. It is the guard
   that matters: the plugin's `no_network` does not cover the local Unix socket the shared server listens on.
2. **A real database is opt-in.** Outside tests, `connect()` and `url_for()` open a Postgres database that is not a
   throwaway one only when the process says so: `STRATA_DB_LIVE=1` in its environment (set it in the app's service
   unit or launcher, not in a shell profile, so scripts, REPLs and agents started from a shell never get it), or
   `strata-db --live`.

SQLite is never restricted: a file path is always deliberate, and tests use in-memory or temporary copies.

No SQLAlchemy at import: the pytest plugin imports this module in every app that has strata-python installed.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable
from typing import Any, cast

THROWAWAY_PREFIX = "strata_db_"
LIVE = "STRATA_DB_LIVE"
TESTING = "STRATA_DB_TESTING"
TEST_ADMIN_URL = "STRATA_DB_TEST_ADMIN_URL"
_GUARDED = "_strata_db_guarded"


class LiveDatabaseRefused(RuntimeError):
    """A connection to a real (non-throwaway) database was refused: under tests always, otherwise without
    `STRATA_DB_LIVE=1`."""


_testing = False


def testing() -> bool:
    return _testing or os.environ.get(TESTING) == "1" or "PYTEST_CURRENT_TEST" in os.environ


def live_allowed() -> bool:
    return os.environ.get(LIVE) == "1" and not testing()


def allow_live() -> None:
    """Opt this process in to real databases (`strata-db --live`). Has no effect under tests."""
    os.environ[LIVE] = "1"


def is_throwaway(dbname: str | None) -> bool:
    return bool(dbname) and str(dbname).startswith(THROWAWAY_PREFIX)


def _admin_database() -> str | None:
    """The database the test admin URL connects to: allowed under tests, since throwaway databases are created
    through it. Never the shared database, whatever the URL says."""
    url = os.environ.get(TEST_ADMIN_URL)  # set by CI, or by testserver once it has started
    if not url:
        return None
    from sqlalchemy.engine import make_url

    from .config import normalise_url, shared_database

    name = make_url(normalise_url(url)).database or None
    return None if name == shared_database() else name


def check_database(dbname: str | None, where: str = "connect") -> None:
    """Raise LiveDatabaseRefused unless the Postgres database `dbname` may be opened by this process now."""
    if is_throwaway(dbname):
        return
    if testing():
        if dbname and dbname == _admin_database():
            return
        raise LiveDatabaseRefused(
            f"{where}: refused Postgres database {dbname!r} during tests. Tests may only use throwaway "
            f"{THROWAWAY_PREFIX}* databases: use the db_env / db_url / db_engine / db_session fixtures "
            "(db_env points the app's own connect(app) at a fresh test database)."
        )
    if not live_allowed():
        raise LiveDatabaseRefused(
            f"{where}: {dbname!r} is a real database and this process has not opted in. Set {LIVE}=1 in the app's "
            "service environment (systemd unit, launcher), or pass --live to strata-db."
        )


def check_url(url: str, where: str = "connect") -> None:
    """check_database() for a SQLAlchemy URL; SQLite and other non-Postgres URLs pass."""
    from sqlalchemy.engine import make_url

    u = make_url(url)
    if u.get_backend_name() != "postgresql":
        return
    q: str | tuple[str, ...] | None = u.query.get("dbname")  # psycopg's dialect lets ?dbname= override the path
    dbname = (q[-1] if isinstance(q, tuple) else q) or u.database
    check_database(dbname or os.environ.get("PGDATABASE") or u.username or os.environ.get("USER"), where)


def enter_test_mode() -> None:
    """Called by the pytest plugin as it loads (before conftest.py and the app's modules are imported)."""
    global _testing
    _testing = True
    os.environ[TESTING] = "1"
    _guard_drivers()


def _guarded_sync(where: str, orig: Callable[..., Any]) -> Callable[..., Any]:
    def connect(*a: Any, **k: Any) -> Any:
        conn = orig(*a, **k)
        try:
            check_database(conn.info.dbname, where)
        except LiveDatabaseRefused:
            conn.close()
            raise
        return conn

    return connect


def _guard_drivers() -> None:
    """Check the database of every new psycopg / psycopg2 connection (so raw create_engine and driver calls are
    covered too). The check runs right after connecting, before any statement; a refused connection is closed."""
    try:
        import psycopg
    except ImportError:
        psycopg = None
    if psycopg is not None and not getattr(psycopg, _GUARDED, False):
        sync_connect = cast("Any", psycopg.Connection).connect.__func__
        async_connect = cast("Any", psycopg.AsyncConnection).connect.__func__
        guarded = _guarded_sync("psycopg.connect", sync_connect)

        async def aconnect(cls: Any, *a: Any, **k: Any) -> Any:
            conn = await async_connect(cls, *a, **k)
            try:
                check_database(conn.info.dbname, "psycopg.AsyncConnection.connect")
            except LiveDatabaseRefused:
                await conn.close()
                raise
            return conn

        cast("Any", psycopg.Connection).connect = classmethod(guarded)
        cast("Any", psycopg.AsyncConnection).connect = classmethod(aconnect)
        cast("Any", psycopg).connect = psycopg.Connection.connect  # the module-level alias SQLAlchemy calls
        setattr(psycopg, _GUARDED, True)
    try:
        p2: Any = importlib.import_module("psycopg2")  # by name: it is never a dependency, only guarded if there
    except ImportError:
        return
    if not getattr(p2, _GUARDED, False):
        p2.connect = _guarded_sync("psycopg2.connect", p2.connect)
        setattr(p2, _GUARDED, True)
