"""Standard database tests every app runs, so no app writes (or forgets) its own. Import them into one test module:

    # tests/unit/test_database.py
    from strata_python.db.testing import test_migrations, test_schema_matches_models  # noqa: F401

pytest collects imported test functions like local ones. They use the plugin's fixtures, so they run on SQLite by
default and on a throwaway Postgres with `pytest --pg` (always Postgres for engine = "postgres" apps), and never on
the app's real database.

  test_schema_matches_models  the migrated test database matches the models (a model changed without a revision)
  test_migrations             on a new, empty scratch database: one head and one base, upgrade to head matches the
                              models, downgrade to base leaves nothing, and each revision goes up, down one step and
                              up again (what `strata-db check` does, as part of the app's own test run)
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from .engine import connect
from .migrations import check_migrations, drift
from .project import Project
from .scratch import scratch_database


def scratch_engine(url: str) -> Engine:
    """An engine on a scratch database's URL: SQLite with the server-like pragmas, Postgres as it is."""
    return connect(url) if url.startswith("sqlite") else create_engine(url)


def test_schema_matches_models(db_engine: Engine, db_project: Project) -> None:
    assert drift(db_engine, db_project.metadata) == []  # noqa: S101  (a test, collected in the app's run)


def test_migrations(db_backend: str, db_project: Project) -> None:
    with scratch_database(db_backend == "postgres") as url:
        eng = scratch_engine(url)
        try:
            problems = check_migrations(eng, db_project.migrations_dir, db_project.metadata)
        finally:
            eng.dispose()
    assert problems == []  # noqa: S101
