"""The workflow's database store, its queue job and `move`, on real SQLite files and a real throwaway Postgres."""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, insert, select
from sqlalchemy.engine import Engine

import strata_python.db as db
from strata_python.db import guard, testserver
from strata_python.db.scratch import scratch_database
from strata_python.db.testing import scratch_engine
from strata_python.tasks.queue import Job, Queue, jobs_table
from strata_python.workflow import CLEARED, DONE, FAILED, Context, Pipeline, StateMachine, gate, step
from strata_python.workflow.store import DbStore, enqueue, move, workflow_table

PG = os.environ.get("STRATA_TEST_PG") == "1" or (os.name != "nt" and os.geteuid() != 0 and testserver.available())
PG = PG or bool(os.environ.get(guard.TEST_ADMIN_URL))
BACKENDS = ["sqlite", pytest.param("postgres", marks=pytest.mark.skipif(not PG, reason="no throwaway Postgres"))]


class Setup:
    def __init__(self, engine: Engine, meta: MetaData) -> None:
        self.engine = engine
        self.events = workflow_table(meta)
        self.jobs = jobs_table(meta)
        self.plans = Table(
            "plans",
            meta,
            Column("id", Integer, primary_key=True),
            Column("state", String(20)),
            Column("by", String(20)),
        )


@pytest.fixture(params=BACKENDS)
def setup(request: pytest.FixtureRequest) -> Iterator[Setup]:
    meta = MetaData(naming_convention=db.NAMING_CONVENTION)
    with scratch_database(request.param == "postgres") as url:
        eng = scratch_engine(url)
        s = Setup(eng, meta)
        meta.create_all(eng)
        try:
            yield s
        finally:
            eng.dispose()


RAN: list[str] = []


def inventory(ctx: Context) -> Any:
    RAN.append("inventory")
    ctx.measure(files=2, seconds=1.5)
    return {"files": ["a.mp3", "b.mp3"], "folder": ctx.inputs["folder"]}


def plan(ctx: Context) -> Any:
    RAN.append("plan")
    if ctx.app == "broken":
        raise OSError("the inbox is not mounted")
    return {"chapters": len(ctx.output("inventory")["files"]), "approved_by": ctx.output("book")}


BOOK = Pipeline(
    "ingest",
    [
        step("inventory", inventory, keys=("folder",), resources=("disk",)),
        gate("book", needs=("inventory",)),
        step("plan", plan, needs=("inventory", "book"), version=2),
    ],
)


def test_records_round_trip_and_are_append_only(setup: Setup):
    RAN.clear()
    store = DbStore(setup.engine, setup.events, BOOK)
    other = DbStore(setup.engine, setup.events, "other")
    inputs = {"folder": "/books/x"}
    out = BOOK.advance(store, "book:1", inputs)
    assert (out.state, out.waiting) == ("waiting", ["book"])
    rec = store.load("book:1")["inventory"]
    assert (rec.status, rec.data, rec.metrics) == (
        DONE,
        {"files": ["a.mp3", "b.mp3"], "folder": "/books/x"},
        {
            "files": 2,
            "seconds": 1.5,
        },
    )
    assert rec.key == out.records["inventory"].key
    assert rec.at > 0
    BOOK.approve(store, "book:1", "book", inputs, by="ada", value={"policy": 3})
    out = BOOK.advance(store, "book:1", inputs)
    assert out.state == "done"
    assert store.load("book:1")["plan"].data == {"chapters": 2, "approved_by": {"policy": 3}}
    assert RAN == ["inventory", "plan"]
    again = DbStore(setup.engine, setup.events, BOOK)
    assert BOOK.advance(again, "book:1", inputs).state == "done"  # a new store reads the same: nothing runs
    assert RAN == ["inventory", "plan"]

    assert BOOK.advance(store, "book:2", inputs, app="broken", only=["plan"]).state == "waiting"  # the gate first
    BOOK.approve(store, "book:2", "book", inputs, by="ada")
    with pytest.raises(OSError, match="not mounted"):
        BOOK.advance(store, "book:2", inputs, app="broken")
    failed = store.load("book:2")["plan"]
    assert (failed.status, failed.error, failed.data) == (FAILED, "OSError: the inbox is not mounted", None)

    BOOK.clear(store, "book:1", "book", by="ada")
    assert [(r.stage, r.status, r.by) for r in store.history("book:1")] == [
        ("inventory", DONE, None),
        ("book", DONE, "ada"),
        ("plan", DONE, None),
        ("book", CLEARED, "ada"),
        ("plan", CLEARED, "ada"),
    ]
    assert store.subjects() == ["book:1", "book:2"]
    assert other.load("book:1") == {}
    assert other.subjects() == []
    assert store.forget("book:2") == 3
    assert store.subjects() == ["book:1"]


def advance_job(job: Job) -> Any:
    """A handler as an app writes it: the store opened here, the inputs read from the app's own records now."""
    store = DbStore(ENGINE[0], EVENTS[0], BOOK)
    return BOOK.advance(store, job.options["subject"], {"folder": "/books/q"}, stopping=lambda: job.stopping).as_json()


ENGINE: list[Engine] = []
EVENTS: list[Table] = []


def test_a_queue_job_advances_a_subject(setup: Setup):
    RAN.clear()
    ENGINE[:] = [setup.engine]
    EVENTS[:] = [setup.events]
    q = Queue(setup.engine, setup.jobs)
    jid = enqueue(q, BOOK, "book:7", priority=2)
    assert enqueue(q, BOOK, "book:7") == jid  # one job per subject at a time
    job = q.get(jid)
    assert job is not None
    assert (job.kind, job.domain, job.options, job.resources, job.priority) == (
        "workflow:ingest",
        "ingest:book:7",
        {"subject": "book:7"},
        ["disk"],
        2,
    )
    ran = q.step({BOOK.kind: advance_job})
    assert ran is not None
    done = q.get(jid)
    assert done is not None
    assert done.state == "done"
    assert done.result == {
        "state": "waiting",
        "waiting": ["book"],
        "failed": [],
        "stages": {"inventory": "done", "book": "waiting", "plan": "blocked"},
    }
    assert RAN == ["inventory"]


PLAN = StateMachine("proposed", {"proposed": {"approved", "rejected"}, "approved": {"running"}, "running": {"done"}})


def test_move_is_one_conditional_update(setup: Setup):
    t = setup.plans
    with setup.engine.begin() as c:
        c.execute(insert(t).values(id=1, state="proposed"))
        assert not move(c, PLAN, t, t.c.id == 1, "running")  # only an approved plan may run
        assert move(c, PLAN, t, t.c.id == 1, "approved", values={"by": "ada"})
        assert move(c, PLAN, t, t.c.id == 1, "running")
        assert not move(c, PLAN, t, t.c.id == 1, "running")  # the second claim loses
        assert c.execute(select(t.c.state, t.c.by)).one() == ("running", "ada")
        with pytest.raises(ValueError, match="'lost' is not a state"):
            move(c, PLAN, t, t.c.id == 1, "lost")


def test_the_database_store_refuses_an_answer_json_cannot_hold(setup: Setup):
    p = Pipeline("mem", [step("a", lambda c: object())], json=False)
    with pytest.raises(TypeError):
        p.advance(DbStore(setup.engine, setup.events, p), "s", {})
