"""strata-db as a person runs it, `python -m strata_python.db` in the app's folder, for the flows that change an
app's models between steps (a module is imported once per process, so these need a process per step)."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from strata_python.db import Project, connect
from strata_python.db.scratch import scratch_database
from strata_python.testing import CliResult

PG = os.environ.get("STRATA_TEST_PG") == "1" or bool(os.environ.get("STRATA_DB_TEST_ADMIN_URL"))
if not PG and os.name != "nt" and os.geteuid() != 0:
    from strata_python.db import testserver

    PG = testserver.available()
BACKENDS = ["sqlite", pytest.param("postgres", marks=pytest.mark.skipif(not PG, reason="no throwaway Postgres"))]

MODELS_V1 = """
from sqlalchemy.orm import Mapped, mapped_column
from strata_python.db import make_base
Base = make_base()

class Thing(Base):
    __tablename__ = "things"
    id: Mapped[int] = mapped_column(primary_key=True, init=False)
    name: Mapped[str]
"""
MODELS_V2 = (
    MODELS_V1
    + """    size: Mapped[int | None] = mapped_column(default=None)
    colour: Mapped[str] = mapped_column(server_default="red", default="red")
"""
)
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


def strata_db(cwd: Path, *args: str, check: bool = True) -> CliResult:
    p = subprocess.run([sys.executable, "-m", "strata_python.db", *args], cwd=cwd, capture_output=True, text=True)
    result = CliResult(p.stdout, p.stderr, p.returncode)
    if check and p.returncode:
        raise AssertionError(f"strata-db {args} exited {p.returncode}:\n{result.out}")
    return result


@pytest.fixture
def app(tmp_path: Path) -> Path:
    (tmp_path / "things").mkdir()
    (tmp_path / "things" / "__init__.py").write_text("")
    (tmp_path / "things" / "models.py").write_text(textwrap.dedent(MODELS_V1))
    strata_db(tmp_path, "init", "--app", f"t{uuid.uuid4().hex[:8]}", "--models", "things.models:Base")
    return tmp_path


def test_help(tmp_path: Path):
    assert "strata-db" in strata_db(tmp_path, "--help").stdout


@pytest.mark.parametrize("backend", BACKENDS)
def test_autogenerate_lifecycle(app: Path, backend: str):
    flag = "--pg" if backend == "postgres" else "--sqlite"
    assert "wrote migrations/" in strata_db(app, "revision", "-m", "initial", flag).stdout
    assert "no changes" in strata_db(app, "revision", "-m", "again", flag).stdout  # nothing written twice
    assert len(list((app / "migrations").glob("*.py"))) == 1
    strata_db(app, "check", flag)
    # Change the model without a revision: check must fail ...
    (app / "things" / "models.py").write_text(textwrap.dedent(MODELS_V2))
    r = strata_db(app, "check", flag, check=False)
    assert r.code == 1
    assert "FAIL" in r.stdout
    # ... until the revision is generated: an ALTER that runs on both engines
    strata_db(app, "revision", "-m", "add size and colour", flag)
    rev = next(f for f in (app / "migrations").glob("*add_size*.py")).read_text()
    assert "batch_alter_table" in rev
    assert "add_column" in rev
    r = strata_db(app, "check", check=False)  # both engines when reachable
    assert r.code == 0, r.out


@pytest.mark.parametrize("backend", BACKENDS)
def test_batch_migration_of_a_referenced_table_with_data(app: Path, backend: str):
    """Recreating a parent table (SQLite batch mode) while child rows point at it must not trip the foreign keys,
    and the data must survive. The scratch-database checks have no rows, so this needs its own test."""
    models = app / "things" / "models.py"
    models.write_text(textwrap.dedent(PARENT_CHILD))
    flag = "--pg" if backend == "postgres" else "--sqlite"
    strata_db(app, "revision", "-m", "initial", flag)
    with scratch_database(backend == "postgres") as url:
        strata_db(app, "migrate", "--url", url)
        if not url.startswith("sqlite"):  # strata-db migrate put the tables in the app's schema
            url += ("&" if "?" in url else "?") + "options=-csearch_path%3D" + Project.load(app).schema
        eng = connect(url) if url.startswith("sqlite") else create_engine(url)
        with eng.begin() as c:
            c.exec_driver_sql("INSERT INTO authors (id, name) VALUES (1, 'Ada')")
            c.exec_driver_sql("INSERT INTO notes (id, author_id) VALUES (1, 1)")
        eng.dispose()
        models.write_text(textwrap.dedent(PARENT_CHILD).replace("String(50)", "String(200)"))
        strata_db(app, "revision", "-m", "longer names", flag)
        r = strata_db(app, "migrate", "--url", url, check=False)
        assert r.code == 0, r.stderr[-2000:]
        eng = connect(url) if url.startswith("sqlite") else create_engine(url)
        with eng.connect() as c:
            assert (
                c.exec_driver_sql("SELECT a.name FROM notes n JOIN authors a ON a.id = n.author_id").scalar() == "Ada"
            )
            if backend == "sqlite":
                assert c.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1  # enforcement is back on
        eng.dispose()
