"""strata-db: the app-facing command line (run from anywhere inside the app's repo; also `python -m strata_python.db`).

  strata-db init --app NAME --models pkg.models:Base [--engine portable|postgres]
                              add [tool.strata-db] to pyproject.toml and create migrations/
  strata-db new-app [NAME]    on the shared server: owner role NAME (NOLOGIN, you are a member) and schema NAME in
                              the shared database, owned by it, no USAGE for PUBLIC (other app roles cannot see it).
                              No sudo (your role has CREATEDB CREATEROLE). --database for another database (tests)
  strata-db revision -m MSG   autogenerate a migration: a scratch database is migrated to head and compared with
                              the models; nothing is written if there is no change. --empty for a hand-written one
  strata-db migrate --live [--url URL]
                              upgrade the app's schema (default URL: config.url_for(app)) to head (or --to REV).
                              --live (or STRATA_DB_LIVE=1) says you mean a real database; refused under tests
  strata-db check             on a scratch database: migrate from empty, compare with the models, downgrade to
                              base, migrate again. Exit 1 on any problem (models changed without a revision, ...)
  strata-db current --live [--url URL]  revision the database is at
Scratch databases go on a throwaway Postgres (STRATA_DB_TEST_ADMIN_URL, else a private server,
strata_python.db.testserver), never the real one. `revision` uses Postgres when one can be had, because Alembic
renders defaults in the scratch database's dialect and Postgres's form (e.g. `false`) also works on SQLite, while
SQLite's (`0`) breaks Postgres; --sqlite forces SQLite. `check` runs on SQLite and, when reachable, Postgres
(engine = "postgres" projects: Postgres only).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from sqlalchemy import create_engine, text

from . import migrations as m
from .config import normalise_url, resolve_url, shared_database
from .engine import connect
from .guard import LIVE, LiveDatabaseRefused, allow_live
from .project import TABLE, Project
from .scratch import live_admin_url, pg_available, scratch_database
from .testing import scratch_engine

NAME = re.compile(r"[a-z][a-z0-9_]*")  # role and schema names: nothing that needs quoting to read


def cmd_init(a: argparse.Namespace) -> None:
    pp = Path.cwd() / "pyproject.toml"
    body = pp.read_text(encoding="utf-8") if pp.exists() else f'[project]\nname = "{a.app}"\nversion = "0.1.0"\n'
    if f"[tool.{TABLE}]" in body:
        sys.exit(f"{pp} already has [tool.{TABLE}]")
    body = (
        body.rstrip("\n") + f'\n\n[tool.{TABLE}]\napp = "{a.app}"\nmodels = "{a.models}"\nmigrations = "migrations"\n'
    )
    if a.engine != "portable":
        body += f'engine = "{a.engine}"\n'
    pp.write_text(body, encoding="utf-8")
    (Path.cwd() / "migrations").mkdir(exist_ok=True)
    print(f"wrote [tool.{TABLE}] to {pp}; migrations/ ready. Next: strata-db revision -m 'initial'")


def cmd_new_app(a: argparse.Namespace) -> None:
    try:
        p: Project | None = Project.load()
    except FileNotFoundError:
        p = None
    name: str | None = a.name or (p.app if p else None)
    schema: str | None = a.schema or (p.schema if p and not a.name else name)
    database: str | None = a.database or shared_database()
    if not name or not schema or not NAME.fullmatch(name) or not NAME.fullmatch(schema):
        sys.exit("app and schema names: lower case letters, digits and _ (they are role and schema names)")
    if not database:
        sys.exit("which database? Pass --database or set STRATA_DB_DATABASE")
    adm = create_engine(live_admin_url(), isolation_level="AUTOCOMMIT")
    with adm.connect() as c:
        q = c.dialect.identifier_preparer.quote_identifier
        if not c.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :n"), {"n": name}).scalar():
            c.execute(text(f"CREATE ROLE {q(name)} NOLOGIN"))
            print(f"created role {name}")
        if not c.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": database}).scalar():
            c.execute(text(f"CREATE DATABASE {q(database)}"))
            print(f"created database {database}")
    adm.dispose()
    eng = create_engine(adm.url.set(database=database), isolation_level="AUTOCOMMIT")
    with eng.connect() as c:
        q = c.dialect.identifier_preparer.quote_identifier
        if not c.execute(text("SELECT 1 FROM pg_namespace WHERE nspname = :s"), {"s": schema}).scalar():
            c.execute(text(f"CREATE SCHEMA {q(schema)} AUTHORIZATION {q(name)}"))
            c.execute(text(f"REVOKE ALL ON SCHEMA {q(schema)} FROM PUBLIC"))
            print(f"created schema {database}.{schema} (owner {name}, no PUBLIC access)")
    eng.dispose()
    where = resolve_url(name, schema) if database == shared_database() else "(--database given)"
    print(f"{name}: schema {schema} in {database}; URL {where}  (strata-db migrate creates the tables)")


def cmd_revision(a: argparse.Namespace) -> None:
    p = Project.load()
    pg = p.postgres_only or a.pg or (not a.sqlite and pg_available())
    print(f"autogenerating against a scratch {'Postgres' if pg else 'SQLite'} database")
    with scratch_database(pg) as url:
        eng = scratch_engine(url)
        m.upgrade(eng, p.migrations_dir, p.metadata)
        path = m.revision(eng, p.migrations_dir, p.metadata, a.message, autogenerate=not a.empty)
        eng.dispose()
    if path:
        print(f"wrote {Path(path).relative_to(p.root).as_posix()}: review it, then commit it")
    else:
        print("no changes: the models match the migrations")


def cmd_migrate(a: argparse.Namespace) -> None:
    if a.live:
        allow_live()
    p = Project.load()
    eng = connect(normalise_url(a.url) if a.url else p.url())
    schema = p.schema if eng.dialect.name == "postgresql" else None
    before = m.current(eng, schema)
    p.upgrade(eng, a.to)
    print(f"{p.app}: {before or 'empty'} -> {m.current(eng, schema)}")
    eng.dispose()


def cmd_check(a: argparse.Namespace) -> None:
    p = Project.load()
    if p.postgres_only or a.pg:
        engines = ["postgres"]
    else:
        engines = ["sqlite"] + (["postgres"] if not a.sqlite and pg_available() else [])
    failed = False
    for e in engines:
        with scratch_database(e == "postgres") as url:
            eng = scratch_engine(url)
            try:
                problems = m.check_migrations(eng, p.migrations_dir, p.metadata)
            except Exception as ex:  # a migration that crashes is a failed check, reported like the others
                problems = [f"{type(ex).__name__}: {ex}".split("\n")[0]]
            eng.dispose()
        for x in problems:
            print(f"FAIL [{e}] {x}")
        failed |= bool(problems)
        if not problems:
            print(f"ok [{e}]: {p.app} migrations run from empty, match the models, and downgrade cleanly")
    if "postgres" not in engines and not a.sqlite:
        print("note: Postgres not reachable, checked SQLite only")
    if failed:
        sys.exit(1)


def cmd_current(a: argparse.Namespace) -> None:
    if a.live:
        allow_live()
    p = Project.load()
    eng = connect(normalise_url(a.url) if a.url else p.url())
    print(m.current(eng, p.schema) or "empty")
    eng.dispose()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="strata-db", description=(__doc__ or "").split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init", help="add [tool.strata-db] to pyproject.toml")
    s.add_argument("--app", required=True)
    s.add_argument("--models", required=True)
    s.add_argument("--engine", choices=["portable", "postgres"], default="portable")
    s.set_defaults(f=cmd_init)
    s = sub.add_parser("new-app", help="role and schema on the shared server")
    s.add_argument("name", nargs="?")
    s.add_argument("--schema")
    s.add_argument("--database", help="default: STRATA_DB_DATABASE")
    s.set_defaults(f=cmd_new_app)
    s = sub.add_parser("revision", help="autogenerate a migration")
    s.add_argument("-m", "--message", required=True)
    s.add_argument("--empty", action="store_true")
    s.add_argument("--pg", action="store_true")
    s.add_argument("--sqlite", action="store_true")
    s.set_defaults(f=cmd_revision)
    live = f"you mean a real database (as {LIVE}=1); refused under tests"
    s = sub.add_parser("migrate", help="upgrade a database to head")
    s.add_argument("--url")
    s.add_argument("--live", action="store_true", help=live)
    s.add_argument("--to", default="head")
    s.set_defaults(f=cmd_migrate)
    s = sub.add_parser("check", help="the migration checks on scratch databases")
    s.add_argument("--pg", action="store_true", help="Postgres only")
    s.add_argument("--sqlite", action="store_true", help="SQLite only")
    s.set_defaults(f=cmd_check)
    s = sub.add_parser("current", help="the revision a database is at")
    s.add_argument("--url")
    s.add_argument("--live", action="store_true", help=live)
    s.set_defaults(f=cmd_current)
    a = ap.parse_args(argv)
    try:
        a.f(a)
    except LiveDatabaseRefused as e:
        sys.exit(f"strata-db: {e}")


if __name__ == "__main__":
    main()
