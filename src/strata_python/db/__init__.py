"""The apps' database: one shared PostgreSQL with a schema per app (SQLite for a file path and in tests), typed
SQLAlchemy models, Alembic migrations generated from them, the `strata-db` command and pytest fixtures. Install the
extra: `strata-python[db]`.

    from strata_python.db import connect, make_base, session
    from sqlalchemy.orm import Mapped, mapped_column

    Base = make_base()
    class Note(Base):                       # a typed record; Alembic generates the table from it
        __tablename__ = "notes"
        id: Mapped[int] = mapped_column(primary_key=True, init=False)
        text: Mapped[str]

    engine = connect("notes")                # schema notes of the shared database (socket, peer auth)
    with session(engine) as s:
        s.add(Note(text="hi")); s.commit()

Migrations: `strata-db revision -m "..."` (autogenerate), `strata-db migrate`, `strata-db check`; or in code
`Project.load(__file__).upgrade(engine)` at service start. Tests: the plugin's `db_session` / `db_engine` / `db_url`
/ `db_env` fixtures and `strata_python.db.testing`. See the README.

The names load on first use: the pytest plugin imports `strata_python.db.guard` in every app with strata-python
installed, and that must not need SQLAlchemy.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import (
        DatabaseNotConfigured as DatabaseNotConfigured,
    )
    from .config import (
        normalise_url as normalise_url,
    )
    from .config import (
        resolve_url as resolve_url,
    )
    from .config import (
        url_for as url_for,
    )
    from .engine import connect as connect
    from .engine import session as session
    from .guard import LiveDatabaseRefused as LiveDatabaseRefused
    from .migrations import (
        check_migrations as check_migrations,
    )
    from .migrations import (
        downgrade as downgrade,
    )
    from .migrations import (
        drift as drift,
    )
    from .migrations import (
        lint_revisions as lint_revisions,
    )
    from .migrations import (
        stairway as stairway,
    )
    from .migrations import (
        upgrade as upgrade,
    )
    from .models import NAMING_CONVENTION as NAMING_CONVENTION
    from .models import Base as Base
    from .models import make_base as make_base
    from .project import Project as Project

_WHERE = {
    "DatabaseNotConfigured": "config",
    "normalise_url": "config",
    "resolve_url": "config",
    "url_for": "config",
    "connect": "engine",
    "session": "engine",
    "LiveDatabaseRefused": "guard",
    "check_migrations": "migrations",
    "downgrade": "migrations",
    "drift": "migrations",
    "lint_revisions": "migrations",
    "stairway": "migrations",
    "upgrade": "migrations",
    "NAMING_CONVENTION": "models",
    "Base": "models",
    "make_base": "models",
    "Project": "project",
}
__all__ = [  # noqa: RUF022  (grouped as _WHERE is)
    "DatabaseNotConfigured",
    "normalise_url",
    "resolve_url",
    "url_for",
    "connect",
    "session",
    "LiveDatabaseRefused",
    "check_migrations",
    "downgrade",
    "drift",
    "lint_revisions",
    "stairway",
    "upgrade",
    "NAMING_CONVENTION",
    "Base",
    "make_base",
    "Project",
]


def __getattr__(name: str) -> Any:
    if name not in _WHERE:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(f"{__name__}.{_WHERE[name]}"), name)
