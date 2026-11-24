"""The queue's routes, the same in every app, so one jobs screen in strata-react serves them all. Needs the db extra.

    add_jobs(app, lambda: QUEUE)       # a function, so a queue on a database opened lazily (add_database) works

    GET  /api/jobs?state=running&kind=ocr&domain=book:12&after=<cursor>&limit=50   Page[Task], newest first
    GET  /api/jobs/counts                                                          {state: count}
    GET  /api/jobs/{id}                                                            Task (404 when there is none)
    POST /api/jobs/{id}/cancel                                                     Task: cancelled, or running and asked to stop
    POST /api/jobs/{id}/retry                                                      Task: queued again (400 unless failed or cancelled)

Each app had its own spelling of these (listory's `/api/jobs`, reels' and tubes' status routes); strata-react's
`useJob`, `JobStatus` and `useJobAction` read a `Task`, so with these routes a screen needs no app-specific glue.
Errors go through `web.add_error_handlers` (KeyError 404, ValueError 400)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, FastAPI, Query

from strata_python.contracts import Page, Task, TaskState
from strata_python.tasks.queue import Queue


def job_task(queue: Queue, job_id: int) -> Task:
    job = queue.get(job_id)
    if job is None:
        raise KeyError(f"there is no job {job_id}")
    return job.to_task()


def add_jobs(app: FastAPI, queue: Callable[[], Queue], *, prefix: str = "/api/jobs") -> APIRouter:
    """Add the queue's routes (above) to `app`; the router, for an app that wants to see them."""
    r = APIRouter(prefix=prefix, tags=["jobs"])

    @r.get("", response_model=Page[Task])
    def list_jobs(  # pyright: ignore[reportUnusedFunction]
        state: Annotated[list[TaskState] | None, Query()] = None,
        kind: str | None = None,
        domain: str | None = None,
        after: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> Page[Task]:
        return queue().page(state=state, kind=kind, domain=domain, after=after, limit=limit)

    @r.get("/counts")
    def job_counts() -> dict[str, int]:  # pyright: ignore[reportUnusedFunction]
        return queue().counts()

    @r.get("/{job_id}")
    def get_job(job_id: int) -> Task:  # pyright: ignore[reportUnusedFunction]
        return job_task(queue(), job_id)

    @r.post("/{job_id}/cancel")
    def cancel_job(job_id: int) -> Task:  # pyright: ignore[reportUnusedFunction]
        q = queue()
        if q.cancel(job_id) is None:
            raise KeyError(f"there is no job {job_id}")
        return job_task(q, job_id)

    @r.post("/{job_id}/retry")
    def retry_job(job_id: int) -> Task:  # pyright: ignore[reportUnusedFunction]
        q = queue()
        task = job_task(q, job_id)
        if not q.retry(job_id):
            raise ValueError(f"job {job_id} is {task.state}: only a failed or cancelled job can be run again")
        return job_task(q, job_id)

    app.include_router(r)
    return r
