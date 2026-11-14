"""Engines and sessions."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .config import normalise_url, url_for
from .guard import check_url

APP_NAME = re.compile(r"[a-z][a-z0-9_-]*")


def connect(app_or_url: str, **kw: Any) -> Engine:
    """Engine for an app name ("notes": see config.url_for) or a URL / SQLite path.
    SQLite gets foreign keys on, WAL and a busy timeout, so it behaves like the server in tests.
    A real Postgres database needs `STRATA_DB_LIVE=1` and is never allowed during tests (strata_python.db.guard)."""
    url = url_for(app_or_url) if APP_NAME.fullmatch(app_or_url) else normalise_url(app_or_url)
    check_url(url, f"connect({app_or_url!r})" if "://" not in app_or_url else "connect(url)")
    engine = create_engine(url, **kw)
    if engine.dialect.name == "sqlite":
        sqlite_pragmas(engine)
    return engine


def sqlite_pragmas(engine: Engine, wal: bool = True) -> None:
    """Foreign keys on, a 30 s busy timeout and (for a file) WAL on every new SQLite connection of `engine`."""

    def on_connect(dbapi_conn: Any, _record: Any) -> None:
        if not isinstance(dbapi_conn, sqlite3.Connection):
            return
        c = dbapi_conn.cursor()
        c.execute("PRAGMA foreign_keys = ON")
        c.execute("PRAGMA busy_timeout = 30000")
        if wal and engine.url.database not in (None, "", ":memory:"):
            c.execute("PRAGMA journal_mode = WAL")
        c.close()

    event.listen(engine, "connect", on_connect)


@contextmanager
def session(engine: Engine, **kw: Any) -> Generator[Session]:
    """A Session that is closed afterwards (commit yourself: s.commit())."""
    with Session(engine, **kw) as s:
        yield s
