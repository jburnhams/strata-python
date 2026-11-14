"""Alembic without per-app boilerplate: one shared env.py (strata_python/db/_alembic), the app's revisions in its
own migrations/ folder, the connection and MetaData passed in from here.

upgrade() is safe to call at every app start: on Postgres it takes an advisory lock so two processes starting at
once do not both migrate, sets search_path to the app's schema (creating the schema if needed), so the tables and
Alembic's version table land there whatever the connection's search_path, and runs as the app's owner role (if it
exists and you are a member) so the app role owns what it creates.

Identifiers (schema and role names) cannot be bound parameters, so they go through the dialect's own quoting
(`quote()`), never into SQL as they are.
"""

from __future__ import annotations

import os
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import MetaData, inspect, text
from sqlalchemy.engine import Connection, Engine

ENV_DIR = Path(__file__).parent / "_alembic"
LOCK_NAMESPACE = "strata_db"
PathLike = str | os.PathLike[str]


def quote(conn: Connection, name: str) -> str:
    """`name` as a quoted SQL identifier in this dialect ("a""b" for a"b)."""
    return conn.dialect.identifier_preparer.quote_identifier(name)


def make_config(
    migrations_dir: PathLike, metadata: MetaData, connection: Connection | None, **attributes: Any
) -> Config:
    migrations_dir = Path(migrations_dir)
    migrations_dir.mkdir(parents=True, exist_ok=True)
    cfg = Config()
    cfg.set_main_option("script_location", str(ENV_DIR))
    cfg.set_main_option("version_locations", str(migrations_dir))
    cfg.set_main_option("path_separator", "os")
    cfg.set_main_option("file_template", "%%(year)d%%(month).2d%%(day).2d_%%(rev)s_%%(slug)s")
    cfg.set_main_option("truncate_slug_length", "40")
    cfg.attributes.update(connection=connection, metadata=metadata, **attributes)
    return cfg


def _prepare(conn: Connection, owner_role: str | None, schema: str | None = None) -> None:
    if conn.dialect.name != "postgresql":
        return
    key = zlib.crc32(f"{LOCK_NAMESPACE}:{conn.engine.url.database}:{schema}".encode())
    conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})
    member = (
        owner_role
        and conn.execute(
            text("SELECT pg_has_role(current_user, :r, 'SET') FROM pg_roles WHERE rolname = :r"), {"r": owner_role}
        ).scalar()
    )
    role = owner_role if member else None
    if schema:
        if not conn.execute(text("SELECT 1 FROM pg_namespace WHERE nspname = :s"), {"s": schema}).scalar():
            owner = f" AUTHORIZATION {quote(conn, role)}" if role else ""
            conn.execute(text(f"CREATE SCHEMA {quote(conn, schema)}{owner}"))
            conn.execute(text(f"REVOKE ALL ON SCHEMA {quote(conn, schema)} FROM PUBLIC"))
        conn.execute(text(f"SET LOCAL search_path TO {quote(conn, schema)}"))
    if role:
        conn.execute(text(f"SET LOCAL ROLE {quote(conn, role)}"))


def _run(engine: Engine, owner_role: str | None, fn: Callable[[Connection], object], schema: str | None = None) -> None:
    """Run fn(conn) in one transaction. On SQLite, foreign-key enforcement is switched off around it (the pragma
        only takes effect outside a transaction): batch mode recreates a table by copy, drop and rename, and dropping a
        table that other rows reference would fail or cascade. Before committing, PRAGMA foreign_key_check must come
        back empty, otherwise everything is rolled back; enforcement is switched back on either way. The SQLite transaction
    is begun explicitly: the driver would otherwise run the DDL outside one, and a failed revision would leave half its
    tables behind."""
    with engine.connect() as conn:
        sqlite = conn.dialect.name == "sqlite"
        if sqlite:
            conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
            conn.commit()
        try:
            with conn.begin():
                if sqlite:  # pysqlite begins only before DML, so DDL would commit as it ran (review finding)
                    conn.exec_driver_sql("BEGIN")
                _prepare(conn, owner_role, schema)
                fn(conn)
                if sqlite and (bad := conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall()):
                    raise RuntimeError(
                        f"migration left foreign key violations (table, rowid, parent, fk): {bad[:10]}; rolled back"
                    )
        finally:
            if sqlite:
                conn.exec_driver_sql("PRAGMA foreign_keys = ON")
                conn.commit()


def upgrade(
    engine: Engine,
    migrations_dir: PathLike,
    metadata: MetaData,
    revision: str = "head",
    owner_role: str | None = None,
    schema: str | None = None,
) -> None:
    _run(engine, owner_role, lambda c: command.upgrade(make_config(migrations_dir, metadata, c), revision), schema)


def downgrade(
    engine: Engine,
    migrations_dir: PathLike,
    metadata: MetaData,
    revision: str = "base",
    owner_role: str | None = None,
    schema: str | None = None,
) -> None:
    _run(engine, owner_role, lambda c: command.downgrade(make_config(migrations_dir, metadata, c), revision), schema)


def current(engine: Engine, schema: str | None = None) -> str | None:
    with engine.connect() as conn:
        if schema and conn.dialect.name == "postgresql":
            conn.execute(text(f"SET search_path TO {quote(conn, schema)}"))
        return MigrationContext.configure(conn).get_current_revision()


def include_name(name: str | None, type_: str, _parents: object) -> bool:
    # Only the app's own tables count; ignore Alembic's bookkeeping table.
    return not (type_ == "table" and name == "alembic_version")


def drift(engine: Engine, metadata: MetaData) -> list[Any]:
    """Differences between the database's schema and the models (empty = in sync). Alembic's own comparison."""
    with engine.connect() as conn:
        opts: dict[str, Any] = {
            "compare_type": True,
            "compare_server_default": conn.dialect.name != "sqlite",  # see _alembic/env.py
            "include_name": include_name,
        }
        return list(cast("list[Any]", compare_metadata(MigrationContext.configure(conn, opts=opts), metadata)))


def _revisions(migrations_dir: PathLike) -> ScriptDirectory:
    """The app's revisions, loaded. Inside a (database-less) Operations context, because a revision may call `op.f()`
    or other `op` helpers at module level, which outside a migration raises "proxy object has not yet been established"."""
    script = ScriptDirectory.from_config(make_config(migrations_dir, MetaData(), None))
    with Operations.context(MigrationContext.configure(dialect_name="sqlite")):
        script.get_revisions("heads")  # loads every revision file now
    return script


def lint_revisions(migrations_dir: PathLike) -> list[str]:
    """Checks on the revision files alone, no database: exactly one base and one head. Two branches that each added
    a revision on the same parent (two PRs written in parallel) leave two heads, and `upgrade head` then fails at app
    start; this catches it in the tests instead."""
    try:
        script = _revisions(migrations_dir)
        heads, bases = script.get_heads(), script.get_bases()
    except Exception as e:  # any load error is the finding: report it like the others, do not crash the check
        return [f"revision files do not form a chain: {type(e).__name__}: {str(e).splitlines()[0]}"]
    problems: list[str] = []
    if len(heads) > 1:
        problems.append(
            f"{len(heads)} heads ({', '.join(sorted(heads))}): revisions written in parallel on the same "
            "parent; set the newer one's down_revision to the other (or write a merge revision)"
        )
    if len(bases) > 1:
        problems.append(
            f"{len(bases)} base revisions ({', '.join(sorted(bases))}): only the first may have down_revision = None"
        )
    return problems


def stairway(engine: Engine, migrations_dir: PathLike, metadata: MetaData) -> list[str]:
    """From base, one revision at a time (in dependency order): upgrade to it, downgrade one step, upgrade again.
    Catches a downgrade that only works from head, or a revision that cannot be re-applied after its own downgrade (a
    leftover index, type or sequence). Ends at head. Returns problems (empty = all good); stops at the first broken
    step. Branches and merges: "one step down" from a revision with one parent is that parent (which also undoes a
    sibling branch applied meanwhile; it is re-applied when the merge needs it), and from a merge revision, its
    first parent (which removes just the merge: the other parents stay applied as heads)."""
    script = _revisions(migrations_dir)
    for rev in reversed(list(script.walk_revisions())):
        name = f"{rev.revision} ({rev.doc.splitlines()[0] if rev.doc else 'no message'})"
        parents = rev.down_revision
        down = "base" if not parents else parents if isinstance(parents, str) else parents[0]
        steps: tuple[tuple[str, Callable[[Engine, PathLike, MetaData, str], None], str], ...] = (
            ("upgrade", upgrade, rev.revision),
            ("downgrade one step from", downgrade, down),
            ("upgrade again to", upgrade, rev.revision),
        )
        for step, fn, target in steps:
            try:
                fn(engine, migrations_dir, metadata, target)
            except Exception as e:  # the failing step is the finding
                return [f"stairway: {step} {name} failed: {type(e).__name__}: {str(e).splitlines()[0]}"]
    upgrade(engine, migrations_dir, metadata)  # every head (a sibling undone above may not be re-applied yet)
    return []


def _tables(engine: Engine) -> list[str]:
    return [t for t in inspect(engine).get_table_names() if t != "alembic_version"]


def check_migrations(engine: Engine, migrations_dir: PathLike, metadata: MetaData) -> list[str]:
    """On an EMPTY database: one head and one base; upgrade from nothing to head and compare with the models;
    downgrade to base (every downgrade must run and leave no tables); then the stairway (each revision up, down one
    step, up again), which ends at head. Returns a list of problems (empty = all good)."""
    if problems := lint_revisions(migrations_dir):
        return problems
    if _tables(engine):
        return ["check_migrations needs an empty database"]
    upgrade(engine, migrations_dir, metadata)
    if d := drift(engine, metadata):
        problems.append(f"after upgrade head the schema differs from the models (run strata-db revision): {d}")
    downgrade(engine, migrations_dir, metadata, "base")
    if left := _tables(engine):
        problems.append(f"downgrade to base left tables: {left}")
        return problems  # upgrading again would only fail on the leftovers
    return problems + stairway(engine, migrations_dir, metadata)


def revision(
    engine: Engine, migrations_dir: PathLike, metadata: MetaData, message: str, autogenerate: bool = True
) -> str | None:
    """Write a new revision file. With autogenerate, `engine` must be a database at head (strata-db uses a scratch
    one) and nothing is written when there is no change. Returns the path written, or None."""

    def directives(_context: object, _rev: object, dirs: list[Any]) -> None:
        if autogenerate and dirs[0].upgrade_ops.is_empty():
            dirs[:] = []

    with engine.begin() as conn:
        cfg = make_config(migrations_dir, metadata, conn, process_revision_directives=directives)
        scripts = command.revision(cfg, message=message, autogenerate=autogenerate)
    written = [s.path for s in (scripts if isinstance(scripts, list) else [scripts]) if s is not None]
    return written[0] if written else None
