"""The jobs and workflow routes through FastAPI's TestClient, on a real SQLite queue and store."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import MetaData

import strata_python.db as db
from strata_python import web
from strata_python.db.scratch import scratch_database
from strata_python.db.testing import scratch_engine
from strata_python.tasks.queue import Job, Queue, jobs_table
from strata_python.tasks.web import add_jobs
from strata_python.workflow import Context, MemoryStore, Pipeline, Record, gate, step
from strata_python.workflow.store import DbStore, workflow_table
from strata_python.workflow.web import add_workflow


def scan(ctx: Context) -> Any:
    ctx.measure(files=len(ctx.inputs["files"]))
    return {"files": ctx.inputs["files"]}


def plan(ctx: Context) -> Any:
    if ctx.output("scan")["files"] == ["bad"]:
        raise OSError("the folder went away")
    return {"chapters": len(ctx.output("scan")["files"])}


BOOK = Pipeline(
    "ingest",
    [
        step("scan", scan, keys=("files",)),
        gate("review", needs=("scan",)),
        step("plan", plan, needs=("scan", "review")),
    ],
)
BOOKS = {"book:1": ["a", "b"], "book:2": ["c"], "book:3": ["bad"]}


def inputs(subject: str) -> dict[str, Any]:
    if subject not in BOOKS:
        raise KeyError(f"there is no book {subject}")
    return {"files": BOOKS[subject]}


class App:
    def __init__(self, client: TestClient, queue: Queue, store: DbStore) -> None:
        self.client, self.queue, self.store = client, queue, store

    def work(self) -> None:
        def advance(job: Job) -> Any:
            subject = job.options["subject"]
            return BOOK.advance(self.store, subject, inputs(subject)).as_json()

        while self.queue.step({BOOK.kind: advance}) is not None:
            pass


@pytest.fixture
def app() -> Iterator[App]:
    meta = MetaData(naming_convention=db.NAMING_CONVENTION)
    events, jobs = workflow_table(meta), jobs_table(meta)
    with scratch_database(False) as url:
        eng = scratch_engine(url)
        meta.create_all(eng)
        q = Queue(eng, jobs, base_s=0, cap_s=0, retries=1)
        store = DbStore(eng, events, BOOK)
        fa = FastAPI()
        web.add_error_handlers(fa)
        add_jobs(fa, lambda: q)
        add_workflow(fa, BOOK, store=lambda: store, inputs=inputs, queue=lambda: q, who=lambda r: "ada")
        try:
            yield App(TestClient(fa), q, store)
        finally:
            eng.dispose()


def test_a_subject_is_run_reviewed_and_finished_through_the_routes(app: App):
    c = app.client
    run = c.get("/api/workflows/ingest/subjects/book:1").json()
    assert (run["state"], run["job"]) == ("blocked", None)
    assert [(s["name"], s["title"], s["gate"], s["state"]) for s in run["stages"]] == [
        ("scan", "Scan", False, "ready"),
        ("review", "Review", True, "blocked"),
        ("plan", "Plan", False, "blocked"),
    ]
    run = c.post("/api/workflows/ingest/subjects/book:1/run").json()
    assert (run["job"]["state"], run["job"]["kind"], run["job"]["domain"]) == (
        "queued",
        "workflow:ingest",
        "ingest:book:1",
    )
    assert c.post("/api/workflows/ingest/subjects/book:1/run").json()["job"]["id"] == run["job"]["id"]  # deduped
    app.work()
    run = c.get("/api/workflows/ingest/subjects/book:1").json()
    assert (run["state"], run["waiting"], run["job"]["state"]) == ("waiting", ["review"], "done")
    last = run["stages"][0]["last"]
    assert (last["status"], last["metrics"], last["data"]) == ("done", {"files": 2}, None)  # no answers in the view

    run = c.post("/api/workflows/ingest/subjects/book:1/stages/review/approve", json={"value": {"ok": True}}).json()
    assert run["stages"][1]["last"]["by"] == "ada"
    assert run["job"]["state"] == "queued"  # approving runs it on
    app.work()
    assert c.get("/api/workflows/ingest/subjects/book:1").json()["state"] == "done"
    history = c.get("/api/workflows/ingest/subjects/book:1/stages/plan").json()
    assert [(h["status"], h["data"]) for h in history] == [("done", {"chapters": 2})]

    run = c.post("/api/workflows/ingest/subjects/book:1/stages/review/clear").json()
    assert [s["state"] for s in run["stages"]] == ["done", "waiting", "blocked"]
    assert run["stages"][1]["last"]["status"] == "cleared"
    assert run["stages"][1]["last"]["by"] == "ada"


def test_errors_and_the_list(app: App):
    c = app.client
    assert c.get("/api/workflows/ingest/subjects/book:9").status_code == 404
    assert c.get("/api/workflows/ingest/subjects/book:1/stages/nothing").status_code == 404
    bad = c.post("/api/workflows/ingest/subjects/book:1/stages/review/approve", json={})
    assert (bad.status_code, bad.json()["detail"]) == (400, "Review cannot be approved before Scan is done")
    assert c.post("/api/workflows/ingest/subjects/book:1/stages/scan/approve", json={}).status_code == 400

    for s in BOOKS:
        c.post(f"/api/workflows/ingest/subjects/{s}/run")
    app.work()
    for s in ("book:1", "book:3"):
        c.post(f"/api/workflows/ingest/subjects/{s}/stages/review/approve", json={})
    app.work()
    page = c.get("/api/workflows/ingest/subjects", params={"limit": 2}).json()
    assert [(r["subject"], r["state"]) for r in page["items"]] == [("book:1", "done"), ("book:2", "waiting")]
    rest = c.get("/api/workflows/ingest/subjects", params={"limit": 2, "after": page["next"]}).json()
    assert ([(r["subject"], r["state"], r["failed"]) for r in rest["items"]], rest["next"]) == (
        [("book:3", "failed", ["plan"])],
        None,
    )
    waiting = c.get("/api/workflows/ingest/subjects", params={"state": ["waiting", "failed"]}).json()
    assert [r["subject"] for r in waiting["items"]] == ["book:2", "book:3"]
    BOOKS["gone"] = ["x"]
    try:
        c.post("/api/workflows/ingest/subjects/gone/run")
        app.work()
    finally:
        del BOOKS["gone"]
    assert [r["subject"] for r in c.get("/api/workflows/ingest/subjects").json()["items"]] == [
        "book:1",
        "book:2",
        "book:3",
    ]  # a subject the app no longer has is not listed


def test_job_routes(app: App):
    c = app.client
    first = jid = c.post("/api/workflows/ingest/subjects/book:3/run").json()["job"]["id"]
    other = app.queue.enqueue("other", {})
    assert c.get(f"/api/jobs/{jid}").json()["state"] == "queued"
    assert c.get("/api/jobs/999").status_code == 404
    assert c.post("/api/jobs/999/cancel").status_code == 404
    assert c.post(f"/api/jobs/{other}/cancel").json()["state"] == "cancelled"
    assert c.post(f"/api/jobs/{jid}/retry").status_code == 400  # queued, not failed
    app.work()  # scan, then the review waits
    assert c.get(f"/api/jobs/{jid}").json()["state"] == "done"
    jid = c.post("/api/workflows/ingest/subjects/book:3/stages/review/approve", json={}).json()["job"]["id"]
    app.work()  # plan fails; one retry allowed, then it fails for good
    failed = c.get(f"/api/jobs/{jid}").json()
    assert (failed["state"], failed["error"]) == ("failed", "OSError: the folder went away")
    assert c.post(f"/api/jobs/{jid}/retry").json()["state"] == "queued"
    assert c.get("/api/jobs/counts").json() == {"queued": 1, "cancelled": 1, "done": 1}
    page = c.get("/api/jobs", params={"state": ["queued", "cancelled"], "limit": 1}).json()
    assert ([t["id"] for t in page["items"]], page["next"]) == ([jid], str(jid))
    rest = c.get("/api/jobs", params={"state": ["queued", "cancelled"], "after": page["next"]}).json()
    assert [t["id"] for t in rest["items"]] == [other]
    assert [t["id"] for t in c.get("/api/jobs", params={"kind": "workflow:ingest"}).json()["items"]] == [jid, first]
    assert [t["id"] for t in c.get("/api/jobs", params={"domain": "ingest:book:3"}).json()["items"]] == [jid, first]


def test_without_a_queue_and_with_a_store_that_cannot_list():
    store = MemoryStore()
    fa = FastAPI()
    web.add_error_handlers(fa)
    add_workflow(fa, BOOK, store=lambda: store, inputs=inputs)
    c = TestClient(fa)
    run = c.post("/api/workflows/ingest/subjects/book:2/run").json()
    assert (run["state"], run["job"]) == ("blocked", None)  # nothing started without a queue
    BOOK.advance(store, "book:2", inputs("book:2"))
    assert [r["subject"] for r in c.get("/api/workflows/ingest/subjects").json()["items"]] == ["book:2"]

    class Latest:
        def __init__(self) -> None:
            self.rows: dict[str, Record] = {}

        def load(self, subject: str) -> dict[str, Record]:
            return dict(self.rows)

        def save(self, subject: str, record: Record) -> None:
            self.rows[record.stage] = record

    latest = Latest()
    fb = FastAPI()
    web.add_error_handlers(fb)
    add_workflow(fb, BOOK, store=lambda: latest, inputs=inputs)
    d = TestClient(fb)
    BOOK.advance(latest, "book:2", inputs("book:2"))
    assert [h["status"] for h in d.get("/api/workflows/ingest/subjects/book:2/stages/scan").json()] == ["done"]
    assert d.get("/api/workflows/ingest/subjects").status_code == 400
