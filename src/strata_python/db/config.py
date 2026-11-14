"""Where an app's data is.

Layout on the shared server: ONE database (its name in `STRATA_DB_DATABASE`, set where the apps run) with a schema
per app, owned by the app's role, so apps can query each other's tables (`select ... from notes.notes join
photos.albums ...`). Postgres cannot join across databases, so never one database per app.

Order: `STRATA_DB_URL` (any app; handy in tests and one-off runs), then `<APP>_DATABASE_URL` (e.g.
`NOTES_DATABASE_URL`), then the default: `postgresql+psycopg:///<database>?options=-csearch_path%3D<schema>` over the
Unix socket with peer auth (you connect as your OS user, a member of the app's owner role). The search_path makes the
app's unqualified table names resolve to its schema. A plain path ("data/app.db", "~/x.sqlite") means SQLite, which
has no schemas.

The shared database has no built-in name: the name is the owner's, and this package is public (AGENTS.md rule 4).

A real Postgres database is opt-in (see `strata_python.db.guard`): url_for() and connect() refuse one unless the
process has `STRATA_DB_LIVE=1`, and refuse it always during tests.
"""

from __future__ import annotations

import os
import re

URL = "STRATA_DB_URL"
DATABASE = "STRATA_DB_DATABASE"


class DatabaseNotConfigured(RuntimeError):
    """No URL for the app and no shared database named, so there is nowhere to connect."""


def normalise_url(url: str) -> str:
    """postgresql:///x -> postgresql+psycopg:///x (psycopg 3); a bare path -> sqlite:///abs/path."""
    if url.startswith(("postgresql://", "postgres://")):
        return "postgresql+psycopg://" + url.split("://", 1)[1]
    if "://" not in url and not url.startswith("sqlite:"):
        return "sqlite:///" + os.path.abspath(os.path.expanduser(url))
    return url


def env_name(app: str) -> str:
    """The app's own URL variable: "my-app" -> MY_APP_DATABASE_URL."""
    return re.sub(r"[^A-Z0-9]", "_", app.upper()) + "_DATABASE_URL"


def shared_database() -> str | None:
    """The shared database's name (`STRATA_DB_DATABASE`), read each time so a test or launcher can set it late."""
    return os.environ.get(DATABASE) or None


def schema_url(schema: str, database: str | None = None) -> str:
    database = database or shared_database()
    if not database:
        raise DatabaseNotConfigured(
            f"no database URL for {schema!r}: set {URL} or {env_name(schema)}, or name the shared database in "
            f"{DATABASE} (in the service environment, next to STRATA_DB_LIVE=1)"
        )
    return f"postgresql+psycopg:///{database}?options=-csearch_path%3D{schema}"


def with_search_path(url: str, schema: str) -> str:
    """A Postgres URL that resolves unqualified names in `schema`, unless it already sets search_path itself.
    Without this, a URL from the environment would read and write a different schema (public) from the one
    Project.upgrade() migrates (the app's)."""
    if not url.startswith("postgresql") or "search_path" in url:
        return url
    return url + ("&" if "?" in url else "?") + f"options=-csearch_path%3D{schema}"


def resolve_url(app: str, schema: str | None = None) -> str:
    """Where `app`'s data is (see the module docstring), without the live-database check."""
    for var in (URL, env_name(app)):
        if value := os.environ.get(var):
            return with_search_path(normalise_url(value), schema or app)
    return schema_url(schema or app)


def url_for(app: str, schema: str | None = None) -> str:
    """resolve_url(), refused (guard.LiveDatabaseRefused) when it is a real Postgres database and the process has
    not opted in with `STRATA_DB_LIVE=1`, or is running tests."""
    from .guard import check_url

    url = resolve_url(app, schema)
    check_url(url, f"url_for({app!r})")
    return url
