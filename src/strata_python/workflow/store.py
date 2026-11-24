"""A pipeline's records in the app's database, a queue job per subject, and state machines moved with one UPDATE.
Needs the db extra (`strata-python[db]`).

    # models.py: part of the app's models, so its migrations create it
    from strata_python.workflow.store import workflow_table
    STAGES = workflow_table(Base.metadata)

    # the app
    store = DbStore(engine, STAGES, INGEST)
    enqueue(QUEUE, INGEST, "book:12")              # dedupes on the subject; holds every resource the stages name

    def advance_ingest(job: Job) -> dict:            # module level, so a job process imports it by name
        book = load_book(job.options["subject"])
        return INGEST.advance(store(), job.options["subject"], inputs_of(book), book, stopping=lambda: job.stopping).as_json()

    HANDLERS = {INGEST.kind: advance_ingest}

The table is append-only, as listory's `ingest_events` was: every attempt, approval and clear is a row, `load` folds
them into the latest per stage and `history` returns them all (the review screen's evidence, "what did it say
before"). One subject is advanced by one job at a time: `enqueue` dedupes on it, and the queue's resources keep two
jobs that share a disk or a GPU apart.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy import (
    Column,
    ColumnElement,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    and_,
    delete,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Connection, Engine, Row

from strata_python.tasks.queue import Queue
from strata_python.workflow.machine import StateMachine
from strata_python.workflow.pipeline import Pipeline, Record


def workflow_table(metadata: MetaData, name: str = "workflow_events") -> Table:
    """The records of every pipeline of the app, in the app's MetaData (its naming convention names the index)."""
    return Table(
        name,
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("pipeline", String(100), nullable=False),
        Column("subject", String(500), nullable=False),  # the app's name for what it is about: "book:12"
        Column("stage", String(100), nullable=False),
        Column("status", String(20), nullable=False),  # done, failed or cleared
        Column("key", String(64), nullable=True),
        Column("data", Text, nullable=True),  # JSON: the stage's answer, or a gate's value
        Column("metrics", Text, nullable=False, default="{}"),  # JSON
        Column("error", Text, nullable=True),
        Column("elapsed_s", Float, nullable=False, default=0.0),
        Column("at", Float, nullable=False),  # epoch seconds, the same on both backends (as the queue's)
        Column("by", String(200), nullable=True),  # who approved or cleared it
        Index(f"ix_{name}_subject", "pipeline", "subject", "id"),
    )


def _record(r: Row[Any]) -> Record:
    return Record(
        stage=str(r.stage),
        status=str(r.status),
        key=r.key,
        data=None if r.data is None else json.loads(r.data),
        metrics=json.loads(r.metrics or "{}"),
        error=r.error,
        elapsed_s=float(r.elapsed_s),
        at=float(r.at),
        by=r.by,
    )


class DbStore:
    """`Store` on the app's database, for one pipeline."""

    def __init__(self, engine: Engine, table: Table, pipeline: Pipeline | str) -> None:
        self.engine = engine
        self.t = table
        self.pipeline = pipeline if isinstance(pipeline, str) else pipeline.name

    def _of(self, subject: str) -> ColumnElement[bool]:
        return and_(self.t.c.pipeline == self.pipeline, self.t.c.subject == subject)

    def load(self, subject: str) -> dict[str, Record]:
        return {r.stage: r for r in self.history(subject)}

    def history(self, subject: str) -> list[Record]:
        """Every record of the subject, oldest first."""
        with self.engine.connect() as c:
            rows = c.execute(select(self.t).where(self._of(subject)).order_by(self.t.c.id)).all()
        return [_record(r) for r in rows]

    def save(self, subject: str, record: Record) -> None:
        with self.engine.begin() as c:
            c.execute(
                insert(self.t).values(
                    pipeline=self.pipeline,
                    subject=subject,
                    stage=record.stage,
                    status=record.status,
                    key=record.key,
                    data=None if record.data is None else json.dumps(record.data, allow_nan=False),
                    metrics=json.dumps(dict(record.metrics), allow_nan=False),
                    error=record.error,
                    elapsed_s=record.elapsed_s,
                    at=record.at,
                    by=record.by,
                )
            )

    def subjects(self) -> list[str]:
        """Every subject with a record in this pipeline, in the order they first had one."""
        first = func.min(self.t.c.id)
        q = (
            select(self.t.c.subject)
            .where(self.t.c.pipeline == self.pipeline)
            .group_by(self.t.c.subject)
            .order_by(first)
        )
        with self.engine.connect() as c:
            return [str(r[0]) for r in c.execute(q)]

    def forget(self, subject: str) -> int:
        """Delete every record of the subject (it was removed from the app); how many rows went."""
        with self.engine.begin() as c:
            return c.execute(delete(self.t).where(self._of(subject))).rowcount


def enqueue(
    queue: Queue,
    pipeline: Pipeline,
    subject: str,
    *,
    priority: int = 0,
    timeout_s: float | None = None,
    delay_s: float | None = None,
) -> int:
    """Queue a job that advances `subject` (kind `pipeline.kind`, options `{"subject": ...}`); the id of the one
    already queued or running for it, if there is one. It holds every resource the pipeline's stages name."""
    return queue.enqueue(
        pipeline.kind,
        {"subject": subject},
        domain=f"{pipeline.name}:{subject}",
        dedupe=True,
        resources=pipeline.resources,
        priority=priority,
        timeout_s=timeout_s,
        delay_s=delay_s,
    )


def move(
    conn: Connection,
    machine: StateMachine,
    table: Table,
    where: ColumnElement[bool],
    to: str,
    *,
    column: str = "state",
    values: Mapping[str, Any] | None = None,
) -> bool:
    """Move the row `where` names to `to` if it is in a state that may go there, in one conditional UPDATE (so two
    workers claiming the same approved plan cannot both win); True when it moved. `values` are set with it."""
    if to not in machine.states:
        raise ValueError(f"{to!r} is not a state of a {machine.noun}: one of {', '.join(sorted(machine.states))}")
    col = table.c[column]
    stmt = update(table).where(where, col.in_(sorted(machine.sources(to)))).values({column: to, **(values or {})})
    return conn.execute(stmt).rowcount == 1
