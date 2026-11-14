"""Throwaway databases for tests and autogenerate.

Postgres: created and dropped through an admin connection on a throwaway server, never the real one.
`STRATA_DB_TEST_ADMIN_URL` names it (in CI a service container); unset, a private server is started for the process
(strata_python.db.testserver). A server that holds the shared database (`STRATA_DB_DATABASE`) is refused: that is
the real one.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import uuid
from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from .config import normalise_url, shared_database
from .guard import TEST_ADMIN_URL, THROWAWAY_PREFIX, is_throwaway, testing

ADMIN_URL = "STRATA_DB_ADMIN_URL"


class RealServerRefused(RuntimeError):
    """The test admin URL points at the real server (it holds the shared database)."""


_checked: set[str] = set()


def admin_url() -> str:
    """Admin URL of the throwaway server for test and scratch databases (starts the private one if needed)."""
    from . import testserver

    url = normalise_url(os.environ.get(TEST_ADMIN_URL) or testserver.url())
    if url not in _checked:
        refuse_real_server(url)
        _checked.add(url)
    return url


def refuse_real_server(url: str) -> None:
    """RealServerRefused when the server behind `url` holds the shared database."""
    database = shared_database()
    if not database:  # nothing named, so nothing to recognise; the guard still keeps tests on throwaway names
        return
    eng = create_engine(url, pool_size=1)
    try:
        with eng.connect() as c:
            real = c.execute(text("SELECT 1 FROM pg_database WHERE datname = :d"), {"d": database}).scalar()
    finally:
        eng.dispose()
    if real:
        raise RealServerRefused(
            f"{TEST_ADMIN_URL} points at a server that holds the shared database {database!r}: test and scratch "
            "databases never go on the real server. Unset it (a private throwaway server is started) or point it at "
            "a throwaway one."
        )


def live_admin_url() -> str:
    """Admin URL for `strata-db new-app` on the real server: `STRATA_DB_ADMIN_URL`, else you over the local socket.
    Under tests it is the throwaway server instead."""
    if testing():
        return admin_url()
    return normalise_url(os.environ.get(ADMIN_URL, "postgresql+psycopg:///postgres"))


def _admin() -> Engine:
    return create_engine(admin_url(), isolation_level="AUTOCOMMIT", pool_size=1)


def pg_available() -> bool:
    """A throwaway Postgres can be had: `STRATA_DB_TEST_ADMIN_URL` reachable, or server binaries for a private one."""
    from . import testserver

    if not os.environ.get(TEST_ADMIN_URL) and not testserver.available():
        return False
    try:
        eng = create_engine(admin_url(), pool_size=1)
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
        eng.dispose()
    except RealServerRefused:
        raise
    except Exception:  # unreachable for any reason: the caller falls back to SQLite and says so
        return False
    return True


def pg_url(dbname: str) -> str:
    u = create_engine(admin_url()).url.set(database=dbname)
    return u.render_as_string(hide_password=False)


def create_pg(name: str, template: str | None = None) -> str:
    eng = _admin()
    with eng.connect() as c:
        q = c.dialect.identifier_preparer.quote_identifier
        c.execute(text(f"CREATE DATABASE {q(name)}" + (f" TEMPLATE {q(template)}" if template else "")))
    eng.dispose()
    return pg_url(name)


def drop_pg(name: str) -> None:
    if not is_throwaway(name):
        raise ValueError(f"drop_pg only drops throwaway databases ({THROWAWAY_PREFIX}*), not {name!r}")
    eng = _admin()
    with eng.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {c.dialect.identifier_preparer.quote_identifier(name)} WITH (FORCE)"))
    eng.dispose()


def unique(kind: str) -> str:
    """A throwaway database name: `strata_db_<kind>_<pid>_<random>`."""
    return f"{THROWAWAY_PREFIX}{kind}_{os.getpid()}_{uuid.uuid4().hex[:8]}"


@contextmanager
def scratch_database(postgres: bool, kind: str = "scratch") -> Generator[str]:
    """URL of an empty database that is gone afterwards: a temp SQLite file, or a new Postgres database."""
    if postgres:
        name = unique(kind)
        url = create_pg(name)
        try:
            yield url
        finally:
            drop_pg(name)
    else:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:  # Windows: a file still open at exit
            yield "sqlite:///" + os.path.join(d, "scratch.db")


def sqlite_copy_to_memory(path: str) -> sqlite3.Connection:
    """An in-memory SQLite connection holding a copy of the file (fast per-test clone of a migrated template)."""
    src = sqlite3.connect(path)
    mem = sqlite3.connect(":memory:", check_same_thread=False)
    src.backup(mem)
    src.close()
    return mem
