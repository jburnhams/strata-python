"""strata_python.db on a temporary app project: config, the guard, migrations and their checks, strata-db, the
fixtures. Real SQLite files and a real throwaway Postgres (the private test server, or STRATA_DB_TEST_ADMIN_URL);
no mocks. Tests that rewrite an app's models after importing them run strata-db in a subprocess
(tests/integration/test_db_cli.py), since a module is imported once per process."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import uuid
from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

import strata_python.db as db
from strata_python.db import cli, guard, scratch, testserver
from strata_python.db.config import DatabaseNotConfigured, normalise_url, resolve_url, url_for
from strata_python.db.migrations import check_migrations, current, drift, lint_revisions, upgrade
from strata_python.db.project import Project
from strata_python.db.scratch import admin_url, drop_pg, pg_available, pg_url, scratch_database
from strata_python.db.testing import scratch_engine
from strata_python.testing import CliResult, run_cli

# Postgres tests need a throwaway server. CI installs one and sets STRATA_TEST_PG=1, so there a missing server fails
# the test instead of skipping it; on a machine without the binaries they skip, and say why.
PG = os.environ.get("STRATA_TEST_PG") == "1" or (os.name != "nt" and os.geteuid() != 0 and testserver.available())
PG = PG or bool(os.environ.get(guard.TEST_ADMIN_URL))
needs_pg = pytest.mark.skipif(not PG, reason="no throwaway Postgres (CI sets STRATA_TEST_PG=1 where it has one)")
BACKENDS = ["sqlite", pytest.param("postgres", marks=needs_pg)]

MODELS_V1 = """
from sqlalchemy.orm import Mapped, mapped_column
from strata_python.db import make_base
Base = make_base()

class Thing(Base):
    __tablename__ = "things"
    id: Mapped[int] = mapped_column(primary_key=True, init=False)
    name: Mapped[str]
"""

PARENT_CHILD = """
from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column
from strata_python.db import make_base
Base = make_base()

class Author(Base):
    __tablename__ = "authors"
    id: Mapped[int] = mapped_column(primary_key=True, init=False)
    name: Mapped[str] = mapped_column(String(50))

class Note(Base):
    __tablename__ = "notes"
    id: Mapped[int] = mapped_column(primary_key=True, init=False)
    author_id: Mapped[int] = mapped_column(ForeignKey("authors.id"))
"""


def strata_db(cwd: Path, *args: object, check: bool = True) -> CliResult:
    old = Path.cwd()
    os.chdir(cwd)
    try:
        return run_cli(cli.main, *args, check=check)
    finally:
        os.chdir(old)


def make_app(root: Path, models: str = MODELS_V1, name: str | None = None) -> Path:
    """A project with its models in a package of its own name (each test imports a different module)."""
    name = name or f"t{uuid.uuid4().hex[:8]}"
    pkg = root / name
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "models.py").write_text(textwrap.dedent(models))
    strata_db(root, "init", "--app", name, "--models", f"{name}.models:Base")
    return root


@pytest.fixture
def app(tmp_path: Path) -> Path:
    return make_app(tmp_path)


def first_revision(app: Path) -> str:
    return next((app / "migrations").glob("*.py")).read_text().split("revision: str = '")[1].split("'")[0]


# --- where the data is ---------------------------------------------------------------------------------------------


def test_url_config(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("STRATA_DB_DATABASE", "shared")
    assert (
        resolve_url("bh") == "postgresql+psycopg:///shared?options=-csearch_path%3Dbh"
    )  # one database, a schema per app
    assert resolve_url("bh", "faces") == "postgresql+psycopg:///shared?options=-csearch_path%3Dfaces"
    monkeypatch.setenv("BH_DATABASE_URL", "postgresql:///other")
    assert resolve_url("bh") == "postgresql+psycopg:///other?options=-csearch_path%3Dbh"  # same schema as migrations
    monkeypatch.setenv("BH_DATABASE_URL", "postgresql://u@h/db?sslmode=disable")
    assert resolve_url("bh") == "postgresql+psycopg://u@h/db?sslmode=disable&options=-csearch_path%3Dbh"
    monkeypatch.setenv("BH_DATABASE_URL", "postgresql:///db?options=-csearch_path%3Dx")
    assert resolve_url("bh") == "postgresql+psycopg:///db?options=-csearch_path%3Dx"  # an explicit choice is kept
    monkeypatch.setenv("STRATA_DB_URL", "data/x.db")
    assert url_for("bh") == "sqlite:///" + os.path.abspath("data/x.db")
    assert normalise_url("sqlite:///:memory:") == "sqlite:///:memory:"
    assert normalise_url("postgres://h/x") == "postgresql+psycopg://h/x"


def test_no_database_named_says_what_to_set(monkeypatch: pytest.MonkeyPatch):
    """The shared database has no built-in name (it is the owner's): without one, the error names the settings."""
    with pytest.raises(DatabaseNotConfigured, match="STRATA_DB_DATABASE") as e:
        resolve_url("my-app")
    assert "MY_APP_DATABASE_URL" in str(e.value)


def test_names_load_lazily():
    assert db.connect is db.engine.connect  # pyright: ignore[reportAttributeAccessIssue]
    assert "make_base" in db.__all__
    with pytest.raises(AttributeError, match="nope"):
        _ = db.nope  # pyright: ignore[reportAttributeAccessIssue]


# --- the guard -----------------------------------------------------------------------------------------------------


def test_guard_refuses_real_databases_in_tests(monkeypatch: pytest.MonkeyPatch):
    """Under pytest the shared database is refused, even with STRATA_DB_LIVE=1 and whichever way it is named."""
    monkeypatch.setenv("STRATA_DB_DATABASE", "shared")
    monkeypatch.setenv("STRATA_DB_LIVE", "1")
    assert guard.testing()
    assert not guard.live_allowed()
    attempts: list[Callable[[], object]] = [
        lambda: url_for("bh"),
        lambda: db.connect("bh"),
        lambda: db.connect("postgresql:///shared"),
        lambda: db.connect("postgresql://db.example/prod"),
    ]
    for attempt in attempts:
        with pytest.raises(guard.LiveDatabaseRefused, match="during tests"):
            attempt()
    with pytest.raises(guard.LiveDatabaseRefused):  # ?dbname= wins in psycopg
        db.connect("postgresql:///strata_db_x?dbname=shared")
    monkeypatch.setenv("BH_DATABASE_URL", "postgresql:///strata_db_test_bh_1")  # a throwaway one is fine
    assert url_for("bh").startswith("postgresql+psycopg:///strata_db_test_bh_1")
    with pytest.raises(ValueError, match="throwaway"):
        drop_pg("shared")


OUTSIDE_TESTS = """
from strata_python.db import guard, scratch
try:
    guard.check_database("shared")
except guard.LiveDatabaseRefused as e:
    assert "STRATA_DB_LIVE=1" in str(e)
else:
    raise AssertionError("not refused")
guard.check_url("sqlite:///x.db")  # SQLite is never restricted
assert scratch.live_admin_url() == "postgresql+psycopg:///postgres"
guard.allow_live()
guard.check_database("shared")  # opted in
print("ok")
"""


def clean_env(**extra: str) -> dict[str, str]:
    """This process's environment as a service would have it: no test flags, no database settings."""
    drop = ("STRATA_DB_TESTING", "PYTEST_CURRENT_TEST", "STRATA_DB_URL", "BH_DATABASE_URL", "STRATA_DB_LIVE")
    return {k: v for k, v in os.environ.items() if k not in drop and not k.startswith("STRATA_DB_ADMIN")} | extra


def test_guard_outside_tests_needs_the_opt_in(tmp_path: Path):
    """The rules for a service, in a process that is not a test run."""
    r = subprocess.run([sys.executable, "-c", OUTSIDE_TESTS], env=clean_env(), capture_output=True, text=True)
    assert r.stdout.strip() == "ok", r.stderr
    code = "from strata_python.db import scratch; print(scratch.live_admin_url())"
    env = clean_env(STRATA_DB_ADMIN_URL="postgresql://admin@h/postgres")
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert r.stdout.strip() == "postgresql+psycopg://admin@h/postgres", r.stderr


def test_admin_database_is_allowed_but_never_the_shared_one(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("STRATA_DB_TEST_ADMIN_URL", "postgresql://u@h/admin")
    guard.check_database("admin")
    monkeypatch.setenv("STRATA_DB_DATABASE", "admin")
    with pytest.raises(guard.LiveDatabaseRefused):
        guard.check_database("admin")


@needs_pg
def test_guard_covers_raw_drivers():
    """create_engine() and psycopg.connect() straight to a real database are refused too, not only connect()."""
    name = f"realdb_{uuid.uuid4().hex[:8]}"
    adm = create_engine(admin_url(), isolation_level="AUTOCOMMIT")
    with adm.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        url = pg_url(name)
        with pytest.raises(guard.LiveDatabaseRefused):
            create_engine(url).connect()
        with pytest.raises(guard.LiveDatabaseRefused):
            psycopg.connect(url.replace("postgresql+psycopg", "postgresql"))
        clean = {k: v for k, v in os.environ.items() if k not in ("STRATA_DB_TESTING", "PYTEST_CURRENT_TEST")}
        code = f"from sqlalchemy import create_engine; create_engine({url!r}).connect()"
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=clean)
        assert r.returncode == 0, r.stderr  # a process with no plugin and no test flag is not guarded...
        code = f"import strata_python.db as d; d.connect({url!r}).connect()"
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert "LiveDatabaseRefused" in r.stderr
        assert "during tests" in r.stderr  # ...but STRATA_DB_TESTING is inherited
    finally:
        with adm.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        adm.dispose()


def test_live_database_is_opt_in(tmp_path: Path):
    """Outside tests a real database needs STRATA_DB_LIVE=1 (or strata-db --live)."""
    env = clean_env(STRATA_DB_DATABASE="shared")
    code = "from strata_python.db import url_for; print(url_for('bh'))"
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode
    assert "STRATA_DB_LIVE=1" in r.stderr
    r = subprocess.run([sys.executable, "-c", code], env={**env, "STRATA_DB_LIVE": "1"}, capture_output=True, text=True)
    assert r.stdout.strip() == "postgresql+psycopg:///shared?options=-csearch_path%3Dbh"
    r = subprocess.run(
        [sys.executable, "-c", code], env={**env, "BH_DATABASE_URL": "data/bh.db"}, capture_output=True, text=True
    )
    assert r.returncode == 0  # SQLite is not restricted


def test_migrate_refuses_the_shared_database_in_tests(app: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("STRATA_DB_DATABASE", "shared")
    r = strata_db(app, "migrate", "--live", check=False)
    assert r.code
    assert "during tests" in r.stderr


# --- engines, projects, migrations ---------------------------------------------------------------------------------


def test_connect_sqlite_pragmas(tmp_path: Path):
    eng = db.connect(str(tmp_path / "a.db"))
    with eng.connect() as c:
        assert c.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert c.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert c.exec_driver_sql("PRAGMA busy_timeout").scalar() == 30000
    eng.dispose()
    with db.session(db.connect("sqlite://")) as s:
        assert s.execute(text("SELECT 1")).scalar() == 1


def test_models_are_typed_records_with_stable_constraint_names(app: Path):
    meta = Project.load(app).metadata
    assert meta.naming_convention == db.NAMING_CONVENTION
    assert db.make_base() is not db.make_base()  # each app its own MetaData
    assert db.Base.metadata.naming_convention["pk"] == "pk_%(table_name)s"


def test_init_writes_config(app: Path):
    p = Project.load(app)
    assert p.app.startswith("t")
    assert p.migrations_dir == app / "migrations"
    assert p.engine == "portable"
    assert p.schema == p.app
    assert not p.postgres_only
    assert "things" in p.metadata.tables
    assert strata_db(app, "init", "--app", "x", "--models", "m:B", check=False).code != 0  # not twice
    with pytest.raises(FileNotFoundError, match=r"\[tool.strata-db\]"):
        Project.load(app.parent)
    assert Project.load(app / "pyproject.toml").root == app  # from a file: its folder and up


def test_init_creates_pyproject_and_postgres_engine(tmp_path: Path):
    strata_db(tmp_path, "init", "--app", "pg_only", "--models", "m:Base", "--engine", "postgres")
    p = Project.load(tmp_path)
    assert p.postgres_only
    assert (tmp_path / "migrations").is_dir()


def test_metadata_may_be_a_metadata(tmp_path: Path):
    (tmp_path / "md.py").write_text("from sqlalchemy import MetaData\nMETA = MetaData()\n")
    p = Project(root=tmp_path, app="x", models="md:META", migrations_dir=tmp_path / "m")
    assert p.metadata.tables == {}


def test_migrate_and_upgrade_at_start(app: Path, tmp_path: Path):
    strata_db(app, "revision", "-m", "initial", "--sqlite")
    url = f"sqlite:///{tmp_path / 'live.db'}"
    assert "empty ->" in strata_db(app, "migrate", "--url", url).stdout
    eng = db.connect(url)
    p = Project.load(app)
    p.upgrade(eng)  # calling again at every start is harmless
    assert current(eng) is not None
    assert drift(eng, p.metadata) == []
    assert strata_db(app, "current", "--url", url).stdout.strip() == current(eng)
    eng.dispose()


def test_project_url_and_current_through_the_environment(app: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    strata_db(app, "revision", "-m", "initial", "--sqlite")
    monkeypatch.setenv("STRATA_DB_URL", str(tmp_path / "env.db"))
    assert Project.load(app).url().startswith("sqlite:///")
    strata_db(app, "migrate")
    assert strata_db(app, "current").stdout.strip() != "empty"


def test_revision_empty_and_no_change(app: Path):
    assert "wrote migrations/" in strata_db(app, "revision", "-m", "initial", "--sqlite").stdout
    assert "no changes" in strata_db(app, "revision", "-m", "again", "--sqlite").stdout
    assert "wrote" in strata_db(app, "revision", "-m", "by hand", "--sqlite", "--empty").stdout
    assert len(list((app / "migrations").glob("*.py"))) == 2


def test_check_sqlite_only_and_postgres_note(app: Path):
    strata_db(app, "revision", "-m", "initial", "--sqlite")
    assert "ok [sqlite]" in strata_db(app, "check", "--sqlite").stdout


@pytest.mark.parametrize("backend", BACKENDS)
def test_check_migrations_catches_a_broken_downgrade(app: Path, backend: str):
    strata_db(app, "revision", "-m", "initial", "--sqlite")
    rev = next((app / "migrations").glob("*.py"))
    rev.write_text(rev.read_text().replace("op.drop_table('things')", "pass"))  # downgrade forgets the table
    p = Project.load(app)
    with scratch_database(backend == "postgres") as url:
        eng = scratch_engine(url)
        problems = check_migrations(eng, p.migrations_dir, p.metadata)
        assert check_migrations(eng, p.migrations_dir, p.metadata) == ["check_migrations needs an empty database"]
        eng.dispose()
    assert any("downgrade to base left tables" in x for x in problems)
    r = strata_db(app, "check", "--sqlite", check=False)
    assert r.code == 1
    assert "FAIL [sqlite]" in r.stdout


def test_check_reports_drift(app: Path):
    """A revision that does not create what the models say: the check names the drift."""
    strata_db(app, "revision", "-m", "initial", "--sqlite", "--empty")
    r = strata_db(app, "check", "--sqlite", check=False)
    assert r.code == 1
    assert "differs from the models" in r.stdout


def test_check_reports_a_migration_that_crashes(app: Path):
    strata_db(app, "revision", "-m", "initial", "--sqlite")
    rev = next((app / "migrations").glob("*.py"))
    rev.write_text(
        rev.read_text().replace("def upgrade() -> None:", "def upgrade() -> None:\n    raise ValueError('x')")
    )
    r = strata_db(app, "check", "--sqlite", check=False)
    assert r.code == 1
    assert "ValueError" in r.stdout


def test_migration_that_breaks_foreign_keys_is_rolled_back(tmp_path: Path):
    """With enforcement off during the migration, a migration that leaves dangling references must fail and roll back."""
    app = make_app(tmp_path / "app", PARENT_CHILD)
    strata_db(app, "revision", "-m", "initial", "--sqlite")
    url = f"sqlite:///{tmp_path / 'fk.db'}"
    strata_db(app, "migrate", "--url", url)
    eng = db.connect(url)
    with eng.begin() as c:
        c.exec_driver_sql("INSERT INTO authors (id, name) VALUES (1, 'Ada')")
        c.exec_driver_sql("INSERT INTO notes (id, author_id) VALUES (1, 1)")
    p = Project.load(app)
    head = current(eng)
    (p.migrations_dir / "zz_bad.py").write_text(
        textwrap.dedent(f"""
        from alembic import op
        revision = "bad0001"; down_revision = "{head}"; branch_labels = None; depends_on = None
        def upgrade(): op.execute("DELETE FROM authors")
        def downgrade(): pass
    """)
    )
    with pytest.raises(RuntimeError, match="foreign key"):
        p.upgrade(eng)
    assert current(eng) == head
    with eng.connect() as c:
        assert c.exec_driver_sql("SELECT count(*) FROM authors").scalar() == 1
        assert c.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1  # enforcement is back on
    eng.dispose()


def test_lint_revisions_catches_two_heads_and_two_bases(app: Path):
    """Two revisions written in parallel on the same parent: `upgrade head` would fail at app start."""
    strata_db(app, "revision", "-m", "initial", "--sqlite")
    assert lint_revisions(app / "migrations") == []
    parent = first_revision(app)
    for rid in ("aaaa00000001", "bbbb00000002"):
        (app / "migrations" / f"x_{rid}.py").write_text(
            f"revision = '{rid}'\ndown_revision = '{parent}'\nbranch_labels = None\ndepends_on = None\n"
            "def upgrade(): pass\ndef downgrade(): pass\n"
        )
    (app / "migrations" / "x_base2.py").write_text(
        "revision = 'cccc00000009'\ndown_revision = None\nbranch_labels = None\ndepends_on = None\n"
        "def upgrade(): pass\ndef downgrade(): pass\n"
    )
    problems = lint_revisions(app / "migrations")
    assert "3 heads" in problems[0]
    assert "aaaa00000001" in problems[0]
    assert "2 base revisions" in problems[1]
    r = strata_db(app, "check", "--sqlite", check=False)
    assert r.code == 1
    assert "3 heads" in r.stdout


def test_lint_revisions_reports_files_that_do_not_load(app: Path):
    (app / "migrations" / "broken.py").write_text("revision = 'x'\ndown_revision = 'missing'\n")
    assert "do not form a chain" in lint_revisions(app / "migrations")[0]


@pytest.mark.parametrize("backend", BACKENDS)
def test_stairway_catches_a_revision_that_cannot_be_reapplied(app: Path, backend: str):
    """The second revision's downgrade forgets its index. Downgrading to base still leaves an empty database (the
    first revision drops the table and the index with it), but re-applying the second revision after its own
    downgrade fails: only the stairway sees that."""
    strata_db(app, "revision", "-m", "initial", "--sqlite")
    (app / "migrations" / "x_cccc00000003.py").write_text(
        f"from alembic import op\nrevision = 'cccc00000003'\ndown_revision = '{first_revision(app)}'\n"
        "branch_labels = None\ndepends_on = None\n"
        "def upgrade(): op.create_index('ix_things_name_extra', 'things', ['name'])\n"
        "def downgrade(): pass\n"
    )
    p = Project.load(app)
    with scratch_database(backend == "postgres") as url:
        eng = scratch_engine(url)
        problems = check_migrations(eng, p.migrations_dir, p.metadata)
        eng.dispose()
    assert not any("left tables" in x for x in problems)
    assert any(
        x.startswith("stairway: upgrade again to cccc00000003") and "ix_things_name_extra" in x for x in problems
    )


def test_check_loads_revisions_that_use_op_at_module_level(app: Path):
    """`op.f()` at module level is fine during a migration; loading the files for the checks must not break on it."""
    strata_db(app, "revision", "-m", "initial", "--sqlite")
    rev = next((app / "migrations").glob("*.py"))
    rev.write_text(rev.read_text() + "\nNAME = op.f('ix_module_level')\n")
    assert lint_revisions(app / "migrations") == []
    assert strata_db(app, "check", "--sqlite").code == 0


@pytest.mark.parametrize("backend", BACKENDS)
def test_stairway_handles_branches_and_a_merge(app: Path, backend: str):
    """Two sibling revisions on the same parent, joined by a merge revision: a valid history, one head."""
    strata_db(app, "revision", "-m", "initial", "--sqlite")
    parent = first_revision(app)
    for rid, table in (("dddd00000001", "left_t"), ("eeee00000002", "right_t")):
        (app / "migrations" / f"x_{rid}.py").write_text(
            f"from alembic import op\nimport sqlalchemy as sa\nrevision = '{rid}'\ndown_revision = '{parent}'\n"
            "branch_labels = None\ndepends_on = None\n"
            f"def upgrade(): op.create_table('{table}', sa.Column('id', sa.Integer, primary_key=True))\n"
            f"def downgrade(): op.drop_table('{table}')\n"
        )
    (app / "migrations" / "x_ffff00000003.py").write_text(
        "revision = 'ffff00000003'\ndown_revision = ('dddd00000001', 'eeee00000002')\nbranch_labels = None\n"
        "depends_on = None\ndef upgrade(): pass\ndef downgrade(): pass\n"
    )
    assert lint_revisions(app / "migrations") == []
    p = Project.load(app)
    with scratch_database(backend == "postgres") as url:
        eng = scratch_engine(url)
        problems = check_migrations(eng, p.migrations_dir, p.metadata)
        assert set(inspect(eng).get_table_names()) >= {"left_t", "right_t", "things"}  # ends at head
        eng.dispose()
    assert not [x for x in problems if x.startswith("stairway") or "left tables" in x], problems


# --- Postgres: schemas, roles, the throwaway server ----------------------------------------------------------------


@needs_pg
def test_new_app_schema_ownership_isolation_and_cross_schema_reads(tmp_path: Path):
    """Two apps in one shared database: each gets a role and a schema; migrations land in the app's schema owned by
    its role; neither app role can see the other's schema; the admin (member of both) can join across them."""
    shared = f"strata_db_shared_{uuid.uuid4().hex[:8]}"
    apps = [f"a{uuid.uuid4().hex[:6]}", f"b{uuid.uuid4().hex[:6]}"]
    adm = create_engine(admin_url(), isolation_level="AUTOCOMMIT")
    url = create_engine(admin_url()).url.set(database=shared).render_as_string(hide_password=False)
    try:
        for name in apps:
            root = make_app(tmp_path / name, name=name)
            strata_db(root, "revision", "-m", "initial", "--pg")
            out = strata_db(root, "new-app", "--database", shared).stdout
            assert f"created schema {shared}.{name}" in out
            assert f"created role {name}" in out
            assert "created" not in strata_db(root, "new-app", "--database", shared).stdout  # idempotent
            strata_db(root, "migrate", "--url", url)  # no search_path in the URL
            assert strata_db(root, "current", "--url", url).stdout.strip() != "empty"
            eng = create_engine(url)
            with eng.begin() as c:
                c.execute(text(f'INSERT INTO "{name}".things (name) VALUES (:n)'), {"n": f"from {name}"})
            eng.dispose()
        eng = create_engine(url)
        with eng.connect() as c:
            a, b = apps
            owners = dict(
                c.execute(
                    text(
                        "SELECT schemaname, tableowner FROM pg_tables WHERE schemaname = ANY(:s) AND tablename = 'things'"
                    ),
                    {"s": apps},
                ).all()
            )
            assert owners == {a: a, b: b}
            versions = c.execute(
                text("SELECT schemaname FROM pg_tables WHERE tablename = 'alembic_version' ORDER BY 1")
            ).scalars()
            assert list(versions) == sorted(apps)  # a version table per schema
            assert c.execute(text("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")).scalar() == 0
            rows = c.execute(text(f'SELECT x.name, y.name FROM "{a}".things x CROSS JOIN "{b}".things y')).one()
            assert tuple(rows) == (f"from {a}", f"from {b}")  # cross-schema read as the admin
            for r, s in ((a, b), (b, a)):  # isolation: neither app role may use the other's schema
                assert (
                    c.execute(text("SELECT has_schema_privilege(:r, :s, 'USAGE')"), {"r": r, "s": s}).scalar() is False
                )
            c.rollback()
            with c.begin():
                c.execute(text(f'SET LOCAL ROLE "{a}"'))
                with pytest.raises(Exception, match="permission denied"):
                    c.execute(text(f'SELECT * FROM "{b}".things'))
        eng.dispose()
    finally:
        with adm.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{shared}" WITH (FORCE)'))
            for r in apps:
                c.execute(text(f'DROP ROLE IF EXISTS "{r}"'))
        adm.dispose()


def test_new_app_refuses_bad_names_and_needs_a_database(app: Path):
    assert "lower case" in strata_db(app, "new-app", "Bad-Name", check=False).stderr
    assert "STRATA_DB_DATABASE" in strata_db(app, "new-app", check=False).stderr


@needs_pg
def test_env_url_without_search_path_uses_the_migrated_schema(app: Path, monkeypatch: pytest.MonkeyPatch):
    """MYAPP_DATABASE_URL without a search_path: connect(app) must see the tables that Project.upgrade() made."""
    p = Project.load(app)
    strata_db(app, "revision", "-m", "initial", "--pg")
    with scratch_database(True) as url:
        monkeypatch.setenv("STRATA_DB_URL", url)  # no search_path in it
        eng = db.connect(p.app)
        p.upgrade(eng)
        with eng.begin() as c:
            c.execute(text("INSERT INTO things (name) VALUES ('x')"))  # unqualified, as app code is
            assert c.execute(text("SELECT current_schema()")).scalar() == p.schema
        assert current(eng, p.schema) is not None
        eng.dispose()


@needs_pg
def test_scratch_refuses_a_server_holding_the_shared_database(monkeypatch: pytest.MonkeyPatch):
    """Test and scratch databases never go on the real server: one that holds the shared database is refused."""
    shared = f"realshared_{uuid.uuid4().hex[:8]}"
    adm = create_engine(admin_url(), isolation_level="AUTOCOMMIT")
    with adm.connect() as c:
        c.execute(text(f'CREATE DATABASE "{shared}"'))
    try:
        monkeypatch.setenv("STRATA_DB_DATABASE", shared)
        with pytest.raises(scratch.RealServerRefused):
            scratch.refuse_real_server(admin_url())
        monkeypatch.setenv("STRATA_DB_TEST_ADMIN_URL", admin_url() + "&application_name=strata")  # not checked yet
        with pytest.raises(scratch.RealServerRefused):
            pg_available()
    finally:
        with adm.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{shared}" WITH (FORCE)'))
        adm.dispose()


@needs_pg
def test_pg_available_and_live_admin_url_under_tests():
    assert pg_available()
    assert scratch.live_admin_url() == admin_url()  # under tests, new-app goes to the throwaway server


def test_pg_not_available_without_a_server(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("STRATA_DB_TEST_ADMIN_URL", "postgresql://u@127.0.0.1:1/postgres?connect_timeout=1")
    assert not pg_available()  # named but unreachable


def test_private_server_reaps_what_a_killed_run_left(tmp_path: Path):
    dead = tmp_path / f"{testserver.PREFIX}dead"
    dead.mkdir()
    (dead / "owner").write_text("999999999")
    alive = tmp_path / f"{testserver.PREFIX}alive"
    alive.mkdir()
    (alive / "owner").write_text(str(os.getpid()))
    odd = tmp_path / f"{testserver.PREFIX}odd"
    odd.mkdir()  # no owner file: not ours to judge
    testserver.reap(root=str(tmp_path))
    assert not dead.exists()
    assert alive.exists()
    assert odd.exists()


def test_server_binaries_from_pgbin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PATH", "")
    assert testserver._version("/usr/lib/postgresql/17/bin") == 17  # pyright: ignore[reportPrivateUsage]
    initdb = tmp_path / ("initdb.exe" if os.name == "nt" else "initdb")
    initdb.write_text("#!/bin/sh\n")
    initdb.chmod(0o755)
    monkeypatch.setenv("PGBIN", str(tmp_path))
    assert testserver._bindir() == str(tmp_path)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.skipif(
    os.name == "nt" or os.geteuid() == 0,
    reason="initdb refuses to run as root; on Windows the server is on loopback, which the other Postgres tests cover",
)
@pytest.mark.skipif(
    not testserver.available() and os.environ.get("STRATA_TEST_PG") != "1",
    reason="no PostgreSQL server binaries (CI installs them and sets STRATA_TEST_PG=1)",
)
def test_private_server_when_no_admin_url():
    """Without STRATA_DB_TEST_ADMIN_URL a process gets its own server in a temp dir, deleted when it exits."""
    env = {k: v for k, v in os.environ.items() if k != "STRATA_DB_TEST_ADMIN_URL"}
    code = (
        "import os; from strata_python.db.scratch import admin_url, scratch_database; u = admin_url(); "
        "d = u.split('host=')[1].replace('%2F', '/'); print(d); assert os.path.isdir(d)\n"
        "with scratch_database(True) as s: assert 'strata_db_scratch' in s and d.replace('/', '%2F') in s"
    )
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    d = r.stdout.strip()
    assert os.path.basename(d).startswith(testserver.PREFIX)
    assert not os.path.exists(d)


# --- the fixtures, on a project of this test module's own ----------------------------------------------------------


@pytest.fixture(scope="session")
def db_project(tmp_path_factory: pytest.TempPathFactory) -> Project:
    """Overrides the plugin's (this repo has no [tool.strata-db]): a migrated app the fixtures below run on."""
    root = make_app(tmp_path_factory.mktemp("fixtures"), PARENT_CHILD)
    strata_db(root, "revision", "-m", "initial", "--sqlite")
    return Project.load(root)


def test_db_session_is_migrated_and_fresh_per_test(db_session: Session, db_engine: Engine, db_backend: str):
    assert db_backend in ("sqlite", "postgres")
    assert db_session.execute(text("SELECT count(*) FROM authors")).scalar() == 0
    db_session.execute(text("INSERT INTO authors (name) VALUES ('Ada')"))
    db_session.commit()
    with db_engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM authors")).scalar() == 1


def test_db_session_again_starts_empty(db_session: Session):
    assert db_session.execute(text("SELECT count(*) FROM authors")).scalar() == 0


def test_db_url_and_db_env(db_url: str, db_env: str, db_project: Project):
    assert os.environ["STRATA_DB_URL"] == db_env
    eng = db.connect(db_project.app)  # the app's own connect() lands on the test database
    assert "authors" in inspect(eng).get_table_names()
    eng.dispose()
    assert db_env.startswith(db_url)


def test_db_engine_enforces_foreign_keys(db_engine: Engine, db_backend: str):
    with pytest.raises(Exception, match=r"(?i)foreign key"), db_engine.begin() as c:
        c.execute(text("INSERT INTO notes (author_id) VALUES (42)"))


from strata_python.db.testing import test_migrations, test_schema_matches_models  # noqa: E402, F401  (collected here)


def test_upgrade_on_an_engine_without_a_schema(tmp_path: Path, db_project: Project):
    eng = db.connect(str(tmp_path / "x.db"))
    upgrade(eng, db_project.migrations_dir, db_project.metadata)
    assert current(eng) is not None
    eng.dispose()


# --- the rest of the guard, the command line and the plugin's Postgres path -----------------------------------------


@needs_pg
def test_guard_covers_async_connections():
    import asyncio

    name = f"realdb_{uuid.uuid4().hex[:8]}"
    adm = create_engine(admin_url(), isolation_level="AUTOCOMMIT")
    with adm.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        url = pg_url(name).replace("postgresql+psycopg", "postgresql")
        with pytest.raises(guard.LiveDatabaseRefused, match="AsyncConnection"):
            asyncio.run(psycopg.AsyncConnection.connect(url))

        async def throwaway() -> str:
            conn = await psycopg.AsyncConnection.connect(admin_url().replace("postgresql+psycopg", "postgresql"))
            name = conn.info.dbname
            await conn.close()
            return name

        assert asyncio.run(throwaway())  # the admin database is allowed
    finally:
        with adm.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        adm.dispose()


def test_guard_covers_psycopg2_when_an_app_has_it(monkeypatch: pytest.MonkeyPatch):
    """psycopg2 is never a dependency here, so a stand-in for the driver module (a true external) shows the wrap."""
    closed: list[bool] = []

    class Conn:
        def __init__(self, dbname: str) -> None:
            self.info = type("Info", (), {"dbname": dbname})()

        def close(self) -> None:
            closed.append(True)

    driver = type(sys)("psycopg2")
    driver.connect = Conn  # pyright: ignore[reportAttributeAccessIssue]
    monkeypatch.setitem(sys.modules, "psycopg2", driver)
    guard._guard_drivers()  # pyright: ignore[reportPrivateUsage]
    guard._guard_drivers()  # pyright: ignore[reportPrivateUsage]  # twice is harmless: wrapped once
    with pytest.raises(guard.LiveDatabaseRefused, match="psycopg2"):
        driver.connect("shared")  # pyright: ignore[reportAttributeAccessIssue]
    assert closed == [True]
    assert driver.connect("strata_db_x").info.dbname == "strata_db_x"  # pyright: ignore[reportAttributeAccessIssue]


def test_no_admin_url_means_no_admin_database(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("STRATA_DB_TEST_ADMIN_URL", raising=False)
    with pytest.raises(guard.LiveDatabaseRefused):
        guard.check_database("postgres")


def test_cli_outside_a_project_and_the_postgres_note(
    tmp_path_factory: pytest.TempPathFactory, app: Path, monkeypatch: pytest.MonkeyPatch
):
    bare = tmp_path_factory.mktemp("bare")  # no pyproject.toml here or above: new-app takes the name given
    assert "STRATA_DB_DATABASE" in strata_db(bare, "new-app", "x", check=False).stderr
    strata_db(app, "revision", "-m", "initial", "--sqlite")
    monkeypatch.setenv(
        "STRATA_DB_TEST_ADMIN_URL", "postgresql://u@127.0.0.1:1/postgres?connect_timeout=1"
    )  # unreachable
    assert "checked SQLite only" in strata_db(app, "check").stdout
    monkeypatch.setenv("STRATA_DB_DATABASE", "shared")
    assert "during tests" in strata_db(app, "current", "--live", check=False).stderr


APP_TESTS = """
from sqlalchemy import text

from strata_python.db.testing import test_migrations, test_schema_matches_models  # noqa: F401


def test_session(db_session, db_backend):
    assert db_backend == "postgres"
    db_session.execute(text("INSERT INTO authors (name) VALUES ('Ada')"))
    db_session.commit()


def test_url_and_env(db_url, db_env):
    assert db_url.startswith("postgresql") and "search_path%3Dpublic" in db_env
"""


@needs_pg
@pytest.mark.parametrize("how", ["--pg", "STRATA_DB_TEST", "engine"])
def test_fixtures_on_postgres(pytester: pytest.Pytester, how: str, monkeypatch: pytest.MonkeyPatch):
    """An app's own test run on Postgres, chosen each of the three ways: --pg, STRATA_DB_TEST, engine = "postgres"."""
    root = make_app(pytester.path, PARENT_CHILD)
    strata_db(root, "revision", "-m", "initial", "--pg")
    if how == "engine":
        pyproject = root / "pyproject.toml"
        pyproject.write_text(pyproject.read_text() + 'engine = "postgres"\n')
    if how == "STRATA_DB_TEST":
        monkeypatch.setenv("STRATA_DB_TEST", "postgres")
    pytester.makepyfile(test_app=APP_TESTS)
    result = pytester.runpytest_inprocess(*(["--pg"] if how == "--pg" else []), "-p", "no:randomly")
    result.assert_outcomes(passed=4)
