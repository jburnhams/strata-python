"""Declarative bases for apps' models.

    from strata_python.db import make_base
    Base = make_base()                    # one per app, in its models module

    class Note(Base):
        __tablename__ = "notes"
        id: Mapped[int] = mapped_column(primary_key=True, init=False)
        text: Mapped[str]

MappedAsDataclass makes each model a typed record (constructor, repr, eq from the annotations). The naming
convention gives every index and constraint a stable name, which Alembic needs to alter or drop them later,
especially on SQLite (batch mode recreates tables and must name what it recreates). It must stay exactly as it is:
the apps' existing revisions and databases carry these names, and a change would make every one of them drift.
`Base` is a ready-made shared one for scripts that only ever load one app's models.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase, MappedAsDataclass

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


if TYPE_CHECKING:
    # What every make_base() returns, for the type checker only: subclassing it gives a model the dataclass
    # constructor (`Note(text="hi")` checked against the annotations). A real class here would be a declarative base
    # of its own, with a registry every app's models would share.
    class DataclassBase(MappedAsDataclass, DeclarativeBase): ...


def make_base() -> type[DataclassBase]:
    """A new declarative base with its own MetaData (so two apps' models never mix in one process)."""

    class AppBase(MappedAsDataclass, DeclarativeBase):
        metadata = MetaData(naming_convention=NAMING_CONVENTION)

    return cast("type[DataclassBase]", AppBase)  # the same shape as DataclassBase, a different class


Base = make_base()
