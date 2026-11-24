"""A pipeline's routes, the same in every app, so one step list and review screen in strata-react serves them all.
The queue parts need the db extra; with no queue the routes still answer, and nothing is started.

    add_workflow(app, INGEST, store=lambda: DbStore(db.engine(), STAGES, INGEST), inputs=inputs_of,
                 queue=lambda: QUEUE)

    GET  /api/workflows/ingest/subjects?state=waiting&after=&limit=    Page[WorkflowRun], in the order they began
    GET  /api/workflows/ingest/subjects/{subject}                      WorkflowRun: each stage's state and last record,
                                                                       and the job advancing it
    GET  /api/workflows/ingest/subjects/{subject}/stages/{stage}       list[StageEvent]: that stage's history, answers included
    POST /api/workflows/ingest/subjects/{subject}/run                  queue its job; WorkflowRun
    POST /api/workflows/ingest/subjects/{subject}/stages/{stage}/approve   body {value}: approve a gate, then run; WorkflowRun
    POST /api/workflows/ingest/subjects/{subject}/stages/{stage}/clear     forget it and its dependents, then run; WorkflowRun

`inputs(subject)` gives the subject's inputs now (read from the app's own records), and raises KeyError for a subject
the app does not have (a 404 through `web.add_error_handlers`). `who(request)` names the person approving or
clearing; the apps have no logins yet, so it defaults to "person". The job is found by its domain
(`workflow.store.enqueue` sets `<pipeline>:<subject>`), so `WorkflowRun.job` is the queue's `Task` and strata-react's
`useJob` polls it like any other job."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, FastAPI, Query, Request
from pydantic import BaseModel

from strata_python.contracts import Page, StageEvent, StageRun, Task, WorkflowRun, utc
from strata_python.workflow.pipeline import Pipeline, Record, Store

if TYPE_CHECKING:
    from strata_python.tasks.queue import Queue

Inputs = Callable[[str], Mapping[str, Any]]


class Approval(BaseModel):
    """The body of an approval: `value` is kept with it (the choice made, the policy judged by). A request body, so a
    plain BaseModel (`contracts.ApiModel` would give the schema an -Input and an -Output twin)."""

    value: Any = None


def event(r: Record, *, data: bool = False) -> StageEvent:
    at = utc(r.at)
    assert at is not None  # noqa: S101 - a record always has a time
    return StageEvent(
        stage=r.stage,
        status=r.status,
        at=at,
        elapsed_s=r.elapsed_s,
        metrics=dict(r.metrics),
        error=r.error,
        by=r.by,
        data=r.data if data else None,
    )


def workflow_run(
    pipeline: Pipeline, store: Store, subject: str, inputs: Mapping[str, Any], job: Task | None = None
) -> WorkflowRun:
    """A subject's `WorkflowRun` (for an app's own route that answers one)."""
    out = pipeline.status(store, subject, inputs)
    return WorkflowRun(
        pipeline=pipeline.name,
        subject=subject,
        state=out.state,
        waiting=out.waiting,
        failed=out.failed,
        stages=[
            StageRun(
                name=s.name,
                title=s.title,
                gate=s.gate,
                needs=list(s.needs),
                state=out.states[s.name],
                last=None if (r := out.records.get(s.name)) is None else event(r),
            )
            for s in pipeline.stages
        ],
        job=job,
    )


def add_workflow(
    app: FastAPI,
    pipeline: Pipeline,
    *,
    store: Callable[[], Store],
    inputs: Inputs,
    queue: Callable[[], Queue] | None = None,
    who: Callable[[Request], str] = lambda r: "person",
    prefix: str = "/api/workflows",
) -> APIRouter:
    """Add the pipeline's routes (above) to `app`; the router. `queue` returns a `tasks.queue.Queue` (a function, so a
    database opened lazily works); without it `run` only answers where the subject stands."""
    r = APIRouter(prefix=f"{prefix}/{pipeline.name}", tags=["workflows"])

    def job_of(subject: str) -> Task | None:
        if queue is None:
            return None
        page = queue().page(domain=f"{pipeline.name}:{subject}", limit=1)
        return page.items[0] if page.items else None

    def view(subject: str) -> WorkflowRun:
        return workflow_run(pipeline, store(), subject, inputs(subject), job_of(subject))

    def start(subject: str) -> WorkflowRun:
        if queue is not None:
            from strata_python.workflow.store import enqueue  # the db extra, only when there is a queue

            enqueue(queue(), pipeline, subject)
        return view(subject)

    @r.get("/subjects", response_model=Page[WorkflowRun])
    def list_runs(  # pyright: ignore[reportUnusedFunction]
        state: Annotated[list[str] | None, Query()] = None,
        after: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> Page[WorkflowRun]:
        s = store()
        names = _subjects(s)
        start_at = names.index(after) + 1 if after in names else 0
        items: list[WorkflowRun] = []
        last = start_at
        for last, subject in enumerate(names[start_at:], start=start_at):  # noqa: B007 - read after the loop
            try:
                run = workflow_run(pipeline, s, subject, inputs(subject), job_of(subject))
            except KeyError:  # a subject the app no longer has: its records stay, it is not listed
                continue
            if state is None or run.state in state:
                items.append(run)
            if len(items) == limit:
                break
        more = len(items) == limit and last + 1 < len(names)
        return Page[WorkflowRun](items=items, next=names[last] if more else None)

    @r.get("/subjects/{subject}")
    def get_run(subject: str) -> WorkflowRun:  # pyright: ignore[reportUnusedFunction]
        return view(subject)

    @r.get("/subjects/{subject}/stages/{stage}")
    def stage_history(subject: str, stage: str) -> list[StageEvent]:  # pyright: ignore[reportUnusedFunction]
        name = pipeline[stage].name
        inputs(subject)  # 404 for a subject the app does not have
        return [event(x, data=True) for x in _history(store(), subject) if x.stage == name]

    @r.post("/subjects/{subject}/run")
    def run_subject(subject: str) -> WorkflowRun:  # pyright: ignore[reportUnusedFunction]
        return start(subject)

    @r.post("/subjects/{subject}/stages/{stage}/approve")
    def approve_stage(  # pyright: ignore[reportUnusedFunction]
        subject: str, stage: str, request: Request, body: Approval | None = None
    ) -> WorkflowRun:
        value = None if body is None else body.value
        pipeline.approve(store(), subject, stage, inputs(subject), by=who(request), value=value)
        return start(subject)

    @r.post("/subjects/{subject}/stages/{stage}/clear")
    def clear_stage(subject: str, stage: str, request: Request) -> WorkflowRun:  # pyright: ignore[reportUnusedFunction]
        inputs(subject)
        pipeline.clear(store(), subject, stage, by=who(request))
        return start(subject)

    app.include_router(r)
    return r


def _subjects(store: Store) -> list[str]:
    """Every subject the store knows, if it can list them (`DbStore.subjects`, `MemoryStore`)."""
    fn = getattr(store, "subjects", None)
    if fn is not None:
        return list(fn())
    raise ValueError("this pipeline's store cannot list its subjects: give it a subjects() method")


def _history(store: Store, subject: str) -> list[Record]:
    fn = getattr(store, "history", None)
    if fn is not None:
        return list(fn(subject))
    return list(store.load(subject).values())  # a store that keeps only the latest: that is its history
