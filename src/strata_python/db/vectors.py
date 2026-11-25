"""Embeddings in a column, and the nearest rows to one, on Postgres (pgvector) and SQLite alike.

    from strata_python.db.vectors import Vector, hnsw_index, nearest

    class FaceEmbedding(Base):
        __tablename__ = "face_embeddings"
        id: Mapped[int] = mapped_column(primary_key=True, init=False)
        model: Mapped[str]
        vector: Mapped[list[float]] = mapped_column(Vector(512))
        __table_args__ = (hnsw_index("face_embeddings_vector_hnsw", "vector"),)

    hits = nearest(db, FaceEmbedding.vector, query, k=20, where=[FaceEmbedding.model == "buffalo_l"])
    for row, distance in hits: ...                      # the rows, closest first, with the cosine distance (0..2)

Three apps keep embeddings (bright-horizons' faces, strata-stills' CLIP vectors, strata-models' face store) and each
wrote its own `pgvector.sqlalchemy.Vector` column, which does not exist on SQLite, so their unit tests could not hold
an embedding at all. Here the column is `vector(dim)` on Postgres (the extension, created once per database by its
superuser) and JSON text on SQLite, and `nearest` orders by pgvector's `<=>` in the database on Postgres and by the
same cosine distance in Python on SQLite: tests get the real answers on a handful of rows, the service gets the index.
No dependency on the `pgvector` package (or numpy): the column speaks pgvector's text form, `[1,2,3]`.

`hnsw_index` is the index pgvector searches by cosine distance; SQLite ignores its Postgres options, so the same
model makes a plain index there. Filtering with `where` on Postgres narrows an approximate HNSW scan, which can then
return fewer than `k` rows; pgvector 0.8 rescans when `SET hnsw.iterative_scan = relaxed_order` is on (`nearest` sets
it for the transaction when the installed pgvector has it)."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Sequence
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import InstrumentedAttribute, Session
from sqlalchemy.types import TypeDecorator, UserDefinedType


class _PgVector(UserDefinedType[list[float]]):
    """pgvector's `vector(dim)`, read and written in its text form."""

    cache_ok = True

    def __init__(self, dim: int | None) -> None:
        self.dim = dim

    def get_col_spec(self, **kw: Any) -> str:
        return f"vector({self.dim})" if self.dim else "vector"


class Vector(TypeDecorator[list[float]]):
    """A list of floats: `vector(dim)` on Postgres, JSON text elsewhere. `dim` is checked on the way in, so a wrong
    model's embedding fails at the write, not at the first search."""

    impl = sa.Text
    cache_ok = True

    def __init__(self, dim: int | None = None) -> None:
        super().__init__()
        self.dim = dim

    def load_dialect_impl(self, dialect: Dialect) -> sa.types.TypeEngine[Any]:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(_PgVector(self.dim))
        return dialect.type_descriptor(sa.Text())

    def process_bind_param(self, value: Sequence[float] | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        v = [float(x) for x in value]
        if self.dim and len(v) != self.dim:
            raise ValueError(f"a vector of {len(v)} numbers in a Vector({self.dim}) column")
        return "[" + ",".join(repr(x) for x in v) + "]"  # pgvector's text form, which is also JSON

    def process_result_value(self, value: Any, dialect: Dialect) -> list[float] | None:
        if value is None:
            return None
        if isinstance(value, str):
            return [float(x) for x in json.loads(value)]
        return [float(x) for x in cast("Iterable[Any]", value)]  # a driver that already parsed it


def hnsw_index(
    name: str, column: str, *, ops: str = "vector_cosine_ops", m: int = 16, ef_construction: int = 64
) -> sa.Index:
    """An HNSW index for cosine search on Postgres (a plain index on SQLite, which ignores the options). Put it in the
    model's `__table_args__`; Alembic's autogenerate writes it into the migration."""
    return sa.Index(
        name,
        column,
        postgresql_using="hnsw",
        postgresql_with={"m": m, "ef_construction": ef_construction},
        postgresql_ops={column: ops},
    )


def cosine_distance(a: Sequence[float], b: Sequence[float]) -> float:
    """1 - cosine similarity, as pgvector's `<=>` gives it: 0 for the same direction, 2 for opposite. A zero vector is
    at distance 1 from everything (pgvector answers NaN; 1 sorts it after every real match instead of first)."""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 1.0
    return 1.0 - dot / (na * nb)


def _iterative_scan(db: Session) -> bool:
    """Whether the installed pgvector has `hnsw.iterative_scan` (0.8 and later; an older one refuses the name)."""
    v = db.execute(sa.text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")).scalar()
    try:
        return tuple(int(x) for x in str(v).split(".")[:2]) >= (0, 8)
    except ValueError:
        return False


def nearest(
    db: Session,
    column: InstrumentedAttribute[list[float]],
    query: Sequence[float],
    *,
    k: int = 10,
    where: Iterable[sa.ColumnElement[bool]] = (),
    max_distance: float | None = None,
    select: sa.Select[Any] | None = None,
) -> list[tuple[Any, float]]:
    """The `k` rows of `column`'s model closest to `query` by cosine distance, closest first, as `(row, distance)`.
    `where` filters (joins go in `select`, a `select(Model)` with them added); `max_distance` drops anything further.
    Postgres orders in the database with `<=>`, which uses an `hnsw_index`; elsewhere every matching row is read and
    ordered in Python, which is right for tests and small tables, not for a large SQLite one."""
    q = [float(x) for x in query]
    entity = column.class_
    stmt = select if select is not None else sa.select(entity)
    for w in where:
        stmt = stmt.where(w)
    if db.get_bind().dialect.name == "postgresql":
        if _iterative_scan(db):
            db.execute(sa.text("SET LOCAL hnsw.iterative_scan = relaxed_order"))  # filtered scans still fill k
        text = "[" + ",".join(repr(x) for x in q) + "]"
        literal = sa.cast(sa.bindparam(None, text, type_=sa.Text), _PgVector(None))
        dist = cast("sa.ColumnElement[float]", column.op("<=>", return_type=sa.Float)(literal))
        if max_distance is not None:
            stmt = stmt.where(dist <= max_distance)
        rows = db.execute(stmt.add_columns(dist.label("distance")).order_by(dist).limit(k)).all()
        return [(r[0], float(r[-1])) for r in rows]
    out: list[tuple[Any, float]] = []
    for r in db.execute(stmt).all():
        v = getattr(r[0], column.key)
        if v is None:
            continue
        d = cosine_distance(q, v)
        if max_distance is None or d <= max_distance:
            out.append((r[0], d))
    out.sort(key=lambda t: t[1])
    return out[:k]
