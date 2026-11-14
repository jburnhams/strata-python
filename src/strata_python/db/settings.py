"""An app's settings kept in its database: the `SettingsStore` for `strata_python.settings.Settings.bind`.

    # models.py: the table is part of the app's models, so its migrations create it
    from strata_python.db.settings import settings_table
    SETTINGS_TABLE = settings_table(Base.metadata)

    # at service start, after the migrations
    SETTINGS.bind(TableStore(engine, SETTINGS_TABLE))   # copies ~/.<app>/settings.json in once

One row per key, the value as JSON text (a string or a list of strings), so the table has the same shape on SQLite and
Postgres and in every app. Each update is one transaction with an upsert per key, so concurrent saves of different keys
never lose one (strata-models' service and its CLI write at the same time).
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from sqlalchemy import Column, MetaData, String, Table, Text, delete, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import Engine


def settings_table(metadata: MetaData, name: str = "settings") -> Table:
    """The settings table in the app's MetaData (its naming convention names the primary key)."""
    return Table(
        name,
        metadata,
        Column("key", String(200), primary_key=True),
        Column("value", Text, nullable=False),  # JSON
    )


class TableStore:
    """Settings in a `settings_table` (see the module docstring)."""

    def __init__(self, engine: Engine, table: Table) -> None:
        self.engine = engine
        self.table = table

    def load(self) -> dict[str, object]:
        with self.engine.connect() as c:
            rows = c.execute(select(self.table.c.key, self.table.c.value)).all()
        out: dict[str, object] = {}
        for key, value in rows:
            try:
                out[str(key)] = json.loads(value)
            except ValueError:
                continue  # a row edited by hand into something that is not JSON reads as missing, like a broken file
        return out

    def update(self, changes: Mapping[str, object]) -> dict[str, object]:
        t = self.table
        insert = postgresql.insert if self.engine.dialect.name == "postgresql" else sqlite.insert
        with self.engine.begin() as c:
            for k, v in changes.items():
                if v is None:
                    c.execute(delete(t).where(t.c.key == k))
                else:
                    stmt = insert(t).values(key=k, value=json.dumps(v))
                    c.execute(stmt.on_conflict_do_update(index_elements=[t.c.key], set_={"value": stmt.excluded.value}))
        return self.load()
