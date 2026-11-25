"""strata_python.db.vectors on a real SQLite file and a real throwaway Postgres with pgvector: an embedding column that
round-trips, refuses the wrong length, and the nearest rows by cosine distance with filters, joins and a cut-off,
the same on both."""

from __future__ import annotations

import math
import os
from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy import ForeignKey, MetaData
from sqlalchemy.engine import Engine
from sqlalchemy.exc import StatementError
from sqlalchemy.orm import DeclarativeBase, Mapped, MappedAsDataclass, Session, mapped_column

from strata_python.db import NAMING_CONVENTION, guard, testserver
from strata_python.db.scratch import scratch_database
from strata_python.db.testing import scratch_engine
from strata_python.db.vectors import Vector, cosine_distance, hnsw_index, nearest

# As in test_db.py: CI sets STRATA_TEST_PG=1 (and installs pgvector), so a missing Postgres fails there instead of skipping.
PG = os.environ.get("STRATA_TEST_PG") == "1" or (os.name != "nt" and os.geteuid() != 0 and testserver.available())
PG = PG or bool(os.environ.get(guard.TEST_ADMIN_URL))
BACKENDS = ["sqlite", pytest.param("postgres", marks=pytest.mark.skipif(not PG, reason="no throwaway Postgres"))]


class Base(MappedAsDataclass, DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Picture(Base):
    __tablename__ = "pictures"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    app: Mapped[str]


class Item(Base):
    __tablename__ = "items"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    picture_id: Mapped[int] = mapped_column(ForeignKey("pictures.id"))
    model: Mapped[str]
    vector: Mapped[list[float] | None] = mapped_column(Vector(3))
    __table_args__ = (hnsw_index("items_vector_hnsw", "vector"),)


ROWS = [  # id, picture, model, vector
    (1, 1, "a", [1.0, 0.0, 0.0]),
    (2, 1, "a", [0.9, 0.1, 0.0]),
    (3, 2, "a", [0.0, 1.0, 0.0]),
    (4, 2, "b", [1.0, 0.0, 0.0]),
    (5, 2, "a", [-1.0, 0.0, 0.0]),
    (6, 1, "a", None),
]


@pytest.fixture(params=BACKENDS)
def engine(request: pytest.FixtureRequest) -> Iterator[Engine]:
    with scratch_database(request.param == "postgres") as url:
        eng = scratch_engine(url)
        if eng.dialect.name == "postgresql":
            with eng.begin() as c:
                c.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
        Base.metadata.create_all(eng)
        with Session(eng) as s:
            s.add_all([Picture(id=1, app="x"), Picture(id=2, app="y")])
            s.flush()
            s.add_all([Item(id=i, picture_id=p, model=m, vector=v) for i, p, m, v in ROWS])
            s.commit()
        try:
            yield eng
        finally:
            eng.dispose()


def test_a_vector_comes_back_as_it_went_in(engine: Engine) -> None:
    with Session(engine) as db:
        got = db.get(Item, 2)
        assert got is not None
        assert got.vector == pytest.approx([0.9, 0.1, 0.0])
        assert db.get(Item, 6).vector is None  # type: ignore[union-attr]


def test_a_vector_of_the_wrong_length_is_refused_at_the_write(engine: Engine) -> None:
    with Session(engine) as db:
        db.add(Item(id=9, picture_id=1, model="a", vector=[1.0, 2.0]))
        with pytest.raises(StatementError, match=r"2 numbers in a Vector\(3\)"):
            db.flush()


def test_nearest_orders_by_cosine_distance_and_filters(engine: Engine) -> None:
    with Session(engine) as db:
        hits = nearest(db, Item.vector, [1.0, 0.0, 0.0], k=3, where=[Item.model == "a"])
        assert [(r.id, round(d, 4)) for r, d in hits] == [(1, 0.0), (2, round(1 - 0.9 / math.sqrt(0.82), 4)), (3, 1.0)]
        assert [r.id for r, _ in nearest(db, Item.vector, [1, 0, 0], k=10, max_distance=0.5)] in ([1, 4, 2], [4, 1, 2])
        far = nearest(db, Item.vector, [1, 0, 0], k=1, where=[Item.id == 5])
        assert [(r.id, round(d, 4)) for r, d in far] == [(5, 2.0)]


def test_nearest_through_a_join(engine: Engine) -> None:
    with Session(engine) as db:
        stmt = sa.select(Item).join(Picture, Picture.id == Item.picture_id).where(Picture.app == "y")
        assert [r.id for r, _ in nearest(db, Item.vector, [0, 1, 0], k=5, select=stmt)][:2] == [3, 4]


def test_cosine_distance() -> None:
    assert cosine_distance([1, 0], [1, 0]) == 0.0
    assert cosine_distance([1, 0], [0, 2]) == pytest.approx(1.0)
    assert cosine_distance([1, 0], [-3, 0]) == pytest.approx(2.0)
    assert cosine_distance([0, 0], [1, 0]) == 1.0  # a zero vector sorts after every real match
    with pytest.raises(ValueError, match="zip"):
        cosine_distance([1, 0], [1, 0, 0])


def test_works_from_a_connection_that_searches_only_the_apps_schema(engine: Engine) -> None:
    """An app's connection sees only its own schema (strata_python.db's search_path): the type and operator must not need `public`."""
    if engine.dialect.name != "postgresql":
        return
    with engine.begin() as c:
        c.execute(sa.text("CREATE SCHEMA IF NOT EXISTS myapp"))
    with Session(engine) as db:
        db.execute(sa.text("SET LOCAL search_path TO myapp"))
        Base.metadata.create_all(db.connection())  # the DDL names public.vector too
        db.add(Picture(id=1, app="x"))
        db.flush()
        db.add(Item(id=1, picture_id=1, model="a", vector=[0.0, 1.0, 0.0]))
        db.flush()
        hits = nearest(db, Item.vector, [1.0, 0.0, 0.0], k=1, where=[Item.model == "a"])
        assert [(r.id, round(d, 3)) for r, d in hits] == [(1, 1.0)]  # myapp's own row, not public's
