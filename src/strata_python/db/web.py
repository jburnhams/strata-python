"""A FastAPI server's records: one engine per app, opened and migrated on the first request that needs it, a session
per request, and the engine disposed when the server stops.

    from strata_python.db.web import DbSession, add_database

    app = web.base_app("notes", __version__, SETTINGS)
    add_database(app, Project.load(__file__), default=lambda: local_url(SETTINGS))

    @app.get("/api/notes")
    def notes(db: DbSession) -> list[NoteOut]:
        return [NoteOut.of(n) for n in db.scalars(select(Note))]

Every app with records behind its server wrote this glue (flashtap's `learners/db.py` and `_records`, issue #39). The
engine is lazy so a server nobody records on never creates a database and its other routes never wait for a
migration; `default` (a URL, a path, or a function giving one, read when the engine opens) keeps a checkout or a
deployment with no database configured working on a local SQLite file. Needs the `[db]` extra."""

from __future__ import annotations

import contextlib
import threading
from collections.abc import AsyncGenerator, Callable, Iterator
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, FastAPI, Request
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from .project import Project

STATE = "strata_db"  # the attribute on app.state


class Database:
    """The app's engine, opened once on first use (thread-safe: sync routes run in a pool) and disposed by `close`."""

    def __init__(self, project: Project, url: str | None = None, default: str | Callable[[], str] | None = None):
        self.project, self.url, self.default = project, url, default
        self._engine: Engine | None = None
        self._lock = threading.Lock()

    @property
    def opened(self) -> bool:
        return self._engine is not None

    def engine(self) -> Engine:
        """The migrated engine, opened on the first call. A failure (a bad URL, a migration that fails) is raised to
        that request and tried again on the next one, so a fixed configuration needs no restart."""
        if self._engine is None:
            with self._lock:
                if self._engine is None:
                    d = self.default
                    default = d if d is None or isinstance(d, str) else d()
                    self._engine = self.project.open(self.url, default=default)
        return self._engine

    def close(self) -> None:
        with self._lock:
            if self._engine is not None:
                self._engine.dispose()
                self._engine = None


def add_database(
    app: FastAPI, project: Project, *, url: str | None = None, default: str | Callable[[], str] | None = None
) -> Database:
    """Hold the app's database on `app.state` for `DbSession`, and dispose it when the server stops (wrapping the
    app's own lifespan, which still runs). `url` overrides the configured one (a test); `default` is used when
    nothing is configured (see `config.local_url`). Returns the `Database`, also reachable as `database(app)`."""
    holder = Database(project, url, default)
    setattr(app.state, STATE, holder)
    inner = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(a: Any) -> AsyncGenerator[Any]:
        try:
            async with inner(a) as state:
                yield state
        finally:
            holder.close()

    app.router.lifespan_context = lifespan
    return holder


def database(app: FastAPI) -> Database:
    """The `Database` `add_database` put on the app (RuntimeError, naming the call, when there is none)."""
    holder = getattr(app.state, STATE, None)
    if not isinstance(holder, Database):
        raise RuntimeError("no database on this app: call strata_python.db.web.add_database(app, project) first")
    return holder


def db_session(request: Request) -> Iterator[Session]:
    """A Session on the app's database for one request, closed afterwards (commit yourself: `db.commit()`)."""
    with Session(database(request.app).engine()) as s:
        yield s


DbSession = Annotated[Session, Depends(db_session)]
"""A route parameter typed `DbSession` gets a session for that request."""
