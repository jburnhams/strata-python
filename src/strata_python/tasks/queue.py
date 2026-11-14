"""A durable job queue in the app's own database (SQLite or the shared Postgres), with workers in this process.
Needs the db extra (`strata-python[db]`).

Seven apps had seven job systems. This one takes the schema and claim loop from strata-listory's `JobQueue` (the most
used), rewritten on SQLAlchemy Core so it runs on both backends; the API style of the strata-models store (an
injectable clock, a claim that is one conditional UPDATE, cursor paging, `recover`, `prune`, a deterministic `step`
for tests); and the retry rules of `tasks.retry`.

    # models.py: the table is part of the app's models, so its migrations create it
    from strata_python.tasks.queue import jobs_table
    JOBS = jobs_table(Base.metadata)

    # the app
    QUEUE = Queue(engine, JOBS)
    jid = QUEUE.enqueue("ocr", {"page": 3}, domain="book:12", dedupe=True, resources=["gpu"])

    def ocr(job: Job) -> dict:
        for i, line in enumerate(lines):
            if job.stopping: return {"lines": i}      # cancelled, timed out or shutting down
            report_progress(i, len(lines), f"line {i}")
        return {"lines": len(lines)}

    WORKER = Worker(QUEUE, {"ocr": ocr}, concurrency=2).start()   # a process per job; stop() at shutdown
    QUEUE.page(state="running")                                   # Page[Task] for the API
    QUEUE.cancel(jid)

What a job gets:
- **Retry**: a failure goes through `tasks.retry.next_state`: a programming error fails at once, anything else runs
  again after a backoff, up to `retries` failures in a row; an attempt that got further than the last (its progress
  `done` grew) counts as the first failure again. `RetryLater(after_s=...)` names its own pause.
- **Crash recovery**: a running job whose worker died (its pid is gone on this host, or no heartbeat for
  `stale_s`) is failed with `WorkerLost` by `recover()` (which `Worker.start` runs) and so retried like any failure.
  Each claim gets its own token and every write an attempt makes names it, so a worker that was only slow, and whose
  job has since been claimed again, finds its writes ignored and its heartbeat telling it to stop.
- **Dedupe**: `dedupe=True` returns the id of a queued or running job with the same kind, domain and options instead
  of adding another (best effort: two enqueues in the same instant from two processes can both add one).
- **Resources**: a job is not claimed while a running job holds one of its `resources` (the GPU, a book's folder).
  Claims are serialised (a lock in this process, a transaction-scoped advisory lock on Postgres, the write lock on
  SQLite), so two workers, in this process or another, never start two jobs that share a resource.
- **Cancel and timeout**: `cancel` ends a queued job at once and stops a running one; a job past its `timeout_s` is
  stopped and fails with TimeoutError (so it retries). A `Worker` runs each job in its own process group by default
  (`tasks.child`), so stopping kills the job and the tools it started; with `isolate=False` (and in `run`/`step`) it
  is cooperative: the handler sees `job.stopping` and returns, and one that never checks runs to the end.
- **on_fail**: `Queue(on_fail=...)` is called with the connection, the job and the error inside the transaction that
  fails it for good, for the app's cascade (listory marks the book's other jobs).

Times are float epoch seconds in the table (the same on both backends) and aware datetimes in `Task`.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import socket
import threading
import time
import uuid
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from sqlalchemy import (
    Boolean,
    Column,
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
    or_,
    select,
    text,
    update,
)
from sqlalchemy.engine import Connection, Engine, Row

from strata_python import oslib
from strata_python.contracts import FINISHED, Page, Progress, Task, utc
from strata_python.tasks import child
from strata_python.tasks.progress import ProgressReporter, reporting
from strata_python.tasks.retry import Decision, next_state

ACTIVE = ("queued", "running")
Json = dict[str, Any]
Handler = Callable[["Job"], Any]


class WorkerLost(RuntimeError):
    """The worker running a job stopped without finishing it (crashed, killed, or the machine went down)."""


class JobCancelled(RuntimeError):
    """Raised by `Queue.check(job)` when a running job has been asked to stop."""


def jobs_table(metadata: MetaData, name: str = "jobs") -> Table:
    """The queue's table in the app's MetaData (its naming convention names the keys and indexes)."""
    return Table(
        name,
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("kind", String(100), nullable=False),
        Column("domain", String(500), nullable=True),  # what the job is about in the app's terms: "book:12"
        Column("options", Text, nullable=False, default="{}"),  # JSON
        Column("dedupe", String(64), nullable=True),  # sha256 of kind, domain and options, when asked for
        Column("resources", Text, nullable=False, default="[]"),  # JSON list of names
        Column("state", String(20), nullable=False),
        Column("priority", Integer, nullable=False, default=0),
        Column("attempts", Integer, nullable=False, default=0),
        Column("run_after", Float, nullable=True),
        Column("timeout_s", Float, nullable=True),
        Column("created_at", Float, nullable=False),
        Column("started_at", Float, nullable=True),
        Column("finished_at", Float, nullable=True),
        Column("heartbeat_at", Float, nullable=True),
        Column("owner", String(200), nullable=True),  # host:pid of the worker running it
        Column("owner_pid", Integer, nullable=True),
        # A new token per claim: every update an attempt makes names it, so a worker whose job was recovered and
        # claimed again elsewhere cannot write over the new attempt.
        Column("claim_token", String(32), nullable=True),
        Column("cancel_requested", Boolean, nullable=False, default=False),
        Column("error", Text, nullable=True),
        Column("result", Text, nullable=True),  # JSON
        Column("progress", Text, nullable=True),  # JSON: a Progress
        Index(f"ix_{name}_claim", "state", "priority", "id"),
        Index(f"ix_{name}_dedupe", "dedupe"),
        Index(f"ix_{name}_domain", "domain"),
    )


def _loads(raw: str | None) -> Any:
    return None if raw is None else json.loads(raw)


@dataclass
class Job:
    """A job as a handler sees it. `stopping` turns true when it is cancelled, times out or the worker stops."""

    id: int
    kind: str
    domain: str | None
    options: Json
    state: str
    priority: int
    attempts: int
    resources: list[str]
    created_at: float
    started_at: float | None
    finished_at: float | None
    run_after: float | None
    timeout_s: float | None
    error: str | None
    result: Any
    progress: Progress | None
    cancel_requested: bool
    token: str | None = None
    stop_event: threading.Event = field(default_factory=threading.Event, repr=False)

    @classmethod
    def from_row(cls, r: Row[Any]) -> Job:
        p = _loads(r.progress)
        return cls(
            id=int(r.id),
            kind=str(r.kind),
            domain=r.domain,
            options=cast("Json", _loads(r.options) or {}),
            state=str(r.state),
            priority=int(r.priority),
            attempts=int(r.attempts),
            resources=cast("list[str]", _loads(r.resources) or []),
            created_at=float(r.created_at),
            started_at=r.started_at,
            finished_at=r.finished_at,
            run_after=r.run_after,
            timeout_s=r.timeout_s,
            error=r.error,
            result=_loads(r.result),
            progress=None if p is None else Progress.model_validate(p),
            cancel_requested=bool(r.cancel_requested),
            token=r.claim_token,
        )

    @property
    def stopping(self) -> bool:
        return self.cancel_requested or self.stop_event.is_set()

    def to_task(self) -> Task:
        created = utc(self.created_at)
        assert created is not None  # noqa: S101 - created_at is NOT NULL
        return Task(
            id=self.id,
            kind=self.kind,
            state=cast("Any", self.state),
            created_at=created,
            started_at=utc(self.started_at),
            finished_at=utc(self.finished_at),
            error=self.error,
            result=self.result,
            progress=self.progress,
            attempts=self.attempts,
            domain=self.domain,
        )


def dedupe_key(kind: str, domain: str | None, options: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps([kind, domain, options], sort_keys=True, default=str).encode()).hexdigest()


class Queue:
    def __init__(
        self,
        engine: Engine,
        table: Table,
        *,
        clock: Callable[[], float] = time.time,
        owner: str | None = None,
        retries: int = 5,
        base_s: float = 30.0,
        cap_s: float = 3600.0,
        on_fail: Callable[[Connection, Job, str], None] | None = None,
        batch: int = 200,
    ) -> None:
        self.engine = engine
        self.t = table
        self.clock = clock
        self.host = socket.gethostname()
        self.owner = owner or f"{self.host}:{os.getpid()}"
        self.retries = retries
        self.base_s = base_s
        self.cap_s = cap_s
        self.on_fail = on_fail
        self.batch = batch
        self._claim_lock = threading.Lock()

    # --- adding and reading ------------------------------------------------------------------------------------------

    def enqueue(
        self,
        kind: str,
        options: Mapping[str, Any] | None = None,
        *,
        domain: str | None = None,
        priority: int = 0,
        resources: Sequence[str] = (),
        dedupe: bool = False,
        delay_s: float | None = None,
        timeout_s: float | None = None,
    ) -> int:
        """Add a job and return its id (or, with `dedupe`, the id of the same job already queued or running).
        Higher `priority` runs first; `delay_s` holds it back that long."""
        opts = dict(options or {})
        key = dedupe_key(kind, domain, opts) if dedupe else None
        t = self.t
        with self.engine.begin() as c:
            if key is not None:
                found = c.execute(select(t.c.id).where(t.c.dedupe == key, t.c.state.in_(ACTIVE)).limit(1)).first()
                if found is not None:
                    return int(found.id)
            now = self.clock()
            res = c.execute(
                t.insert().values(
                    kind=kind,
                    domain=domain,
                    options=json.dumps(opts),
                    dedupe=key,
                    resources=json.dumps(sorted(set(resources))),
                    state="queued",
                    priority=priority,
                    attempts=0,
                    run_after=None if delay_s is None else now + delay_s,
                    timeout_s=timeout_s,
                    created_at=now,
                    cancel_requested=False,
                )
            )
            pk = res.inserted_primary_key
            assert pk is not None  # noqa: S101 - an insert always has one
            return int(pk[0])

    def get(self, job_id: int) -> Job | None:
        with self.engine.connect() as c:
            r = c.execute(select(self.t).where(self.t.c.id == job_id)).first()
        return None if r is None else Job.from_row(r)

    def page(
        self,
        *,
        state: str | Sequence[str] | None = None,
        kind: str | None = None,
        domain: str | None = None,
        after: str | None = None,
        limit: int = 50,
    ) -> Page[Task]:
        """Jobs newest first, `limit` at a time; pass the page's `next` back as `after` for the next page."""
        t = self.t
        q = select(t).order_by(t.c.id.desc()).limit(limit + 1)
        if state is not None:
            q = q.where(t.c.state.in_([state] if isinstance(state, str) else list(state)))
        if kind is not None:
            q = q.where(t.c.kind == kind)
        if domain is not None:
            q = q.where(t.c.domain == domain)
        if after:
            q = q.where(t.c.id < int(after))
        with self.engine.connect() as c:
            rows = c.execute(q).all()
        jobs = [Job.from_row(r) for r in rows[:limit]]
        more = len(rows) > limit
        return Page[Task](items=[j.to_task() for j in jobs], next=str(jobs[-1].id) if more and jobs else None)

    def counts(self) -> dict[str, int]:
        """How many jobs are in each state."""
        with self.engine.connect() as c:
            rows = c.execute(select(self.t.c.state, func.count()).group_by(self.t.c.state)).all()
        return {str(s): int(n) for s, n in rows}

    # --- claiming and running ----------------------------------------------------------------------------------------

    @contextlib.contextmanager
    def _serialised(self) -> Generator[Connection]:
        """One claim at a time: a lock in this process and, on Postgres, an advisory lock for this table that
        other processes wait on too (released when the transaction ends)."""
        with self._claim_lock, self.engine.begin() as c:
            if c.dialect.name == "postgresql":
                lock_id = int(hashlib.sha256(f"strata-queue:{self.t.fullname}".encode()).hexdigest()[:15], 16)
                c.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": lock_id})
            elif c.dialect.name == "sqlite":
                # A write that changes nothing takes SQLite's write lock for the rest of the transaction, so another
                # process (or another Queue on the same file) waits here, before it reads which resources are held.
                c.execute(update(self.t).where(text("0")).values(state=self.t.c.state))
            yield c

    def claim(self, kinds: Sequence[str] | None = None) -> Job | None:
        """Take the next job this worker may run (highest priority, then oldest; not before its `run_after`; none of
        its resources held by a running job) and mark it running. None when there is nothing to do. Candidates are read
        `batch` at a time, in claim order, so jobs waiting on a held resource never hide the ones behind them."""
        t = self.t
        now = self.clock()
        with self._serialised() as c:
            held: set[str] = set()
            for (raw,) in c.execute(select(t.c.resources).where(t.c.state == "running")).all():
                held.update(cast("list[str]", json.loads(raw)))
            base = (
                select(t)
                .where(t.c.state == "queued", or_(t.c.run_after.is_(None), t.c.run_after <= now))
                .order_by(t.c.priority.desc(), t.c.id)
                .limit(self.batch)
            )
            if kinds is not None:
                base = base.where(t.c.kind.in_(list(kinds)))
            q = base
            while rows := c.execute(q).all():
                for r in rows:
                    if held & set(cast("list[str]", json.loads(r.resources))):
                        continue
                    token = uuid.uuid4().hex
                    done = c.execute(
                        update(t)
                        .where(t.c.id == r.id, t.c.state == "queued")
                        .values(
                            state="running",
                            started_at=now,
                            heartbeat_at=now,
                            owner=self.owner,
                            owner_pid=os.getpid(),
                            cancel_requested=False,
                            claim_token=token,
                        )
                    )
                    if done.rowcount == 1:
                        return Job.from_row(c.execute(select(t).where(t.c.id == r.id)).one())
                last = rows[-1]  # the next batch starts after the last one read (priority desc, then id)
                q = base.where(or_(t.c.priority < last.priority, and_(t.c.priority == last.priority, t.c.id > last.id)))
        return None

    def _mine(self, job: Job) -> Any:
        """The row of this attempt of `job`: still running, under the claim this worker made."""
        return and_(self.t.c.id == job.id, self.t.c.state == "running", self.t.c.claim_token == job.token)

    def heartbeat(self, job: Job) -> bool:
        """Mark the job alive and pick up a cancel request; False when it should stop."""
        t = self.t
        with self.engine.begin() as c:
            c.execute(update(t).where(self._mine(job)).values(heartbeat_at=self.clock()))
            r = c.execute(select(t.c.cancel_requested, t.c.state, t.c.claim_token).where(t.c.id == job.id)).first()
        if r is None or r.state != "running" or r.claim_token != job.token or r.cancel_requested:
            job.cancel_requested = True
        return not job.stopping

    def report(self, job: Job, progress: Progress) -> None:
        """Store the job's progress (and count it as a heartbeat)."""
        t = self.t
        with self.engine.begin() as c:
            c.execute(
                update(t).where(self._mine(job)).values(progress=progress.model_dump_json(), heartbeat_at=self.clock())
            )
        job.progress = progress

    def check(self, job: Job) -> None:
        """Raise `JobCancelled` if the job has been asked to stop (for handlers that prefer an exception)."""
        if job.stopping:
            raise JobCancelled(f"job {job.id} was asked to stop")

    def finish(self, job: Job, result: Any = None) -> None:
        """The job is done (or `cancelled`, when it stopped because it was asked to)."""
        t = self.t
        state = "cancelled" if job.cancel_requested else "done"
        with self.engine.begin() as c:
            c.execute(
                update(t)
                .where(self._mine(job))
                .values(state=state, finished_at=self.clock(), result=json.dumps(result, default=str), error=None)
            )
        job.state = state

    def fail(
        self,
        job: Job,
        error: BaseException,
        *,
        progressed: bool = False,
        retryable: Callable[[BaseException], bool] | None = None,
    ) -> Decision:
        """Record a failed attempt: queued again after its backoff, or failed for good (then `on_fail` runs)."""
        t = self.t
        d = next_state(
            job.attempts,
            error,
            progressed=progressed,
            retries=self.retries,
            retryable=retryable,
            base_s=self.base_s,
            cap_s=self.cap_s,
        )
        if job.cancel_requested and not isinstance(error, TimeoutError):
            d = Decision("failed", d.attempts, None, d.error)  # a cancelled job is not run again
        now = self.clock()
        with self.engine.begin() as c:
            if d.state == "retry":
                values: Json = {
                    "state": "queued",
                    "run_after": now + (d.after_s or 0),
                    "owner": None,
                    "owner_pid": None,
                    "heartbeat_at": None,
                    "claim_token": None,
                }
            else:
                values = {"state": "cancelled" if job.cancel_requested else "failed", "finished_at": now}
            done = c.execute(update(t).where(self._mine(job)).values(attempts=d.attempts, error=d.error, **values))
            if done.rowcount == 0:  # this attempt was recovered (and maybe claimed again): nothing of it to record
                return d
            if d.state == "failed" and self.on_fail is not None:
                self.on_fail(c, job, d.error)
        job.state, job.attempts, job.error = str(values["state"]), d.attempts, d.error
        return d

    def cancel(self, job_id: int) -> str | None:
        """Cancel a queued job at once, or ask a running one to stop. Returns the job's state after, None if unknown."""
        t = self.t
        with self.engine.begin() as c:
            c.execute(
                update(t)
                .where(t.c.id == job_id, t.c.state == "queued")
                .values(state="cancelled", finished_at=self.clock())
            )
            c.execute(update(t).where(t.c.id == job_id, t.c.state == "running").values(cancel_requested=True))
            r = c.execute(select(t.c.state).where(t.c.id == job_id)).first()
        return None if r is None else str(r.state)

    def retry(self, job_id: int) -> bool:
        """Run a failed or cancelled job again, from a clean count (a person asked for it)."""
        t = self.t
        with self.engine.begin() as c:
            done = c.execute(
                update(t)
                .where(t.c.id == job_id, t.c.state.in_(("failed", "cancelled")))
                .values(state="queued", attempts=0, run_after=None, finished_at=None, cancel_requested=False)
            )
        return done.rowcount == 1

    # --- housekeeping ------------------------------------------------------------------------------------------------

    def recover(self, *, stale_s: float = 300.0) -> list[int]:
        """Fail (and so retry) running jobs whose worker is gone: its process is not alive on this host, or it has
        not sent a heartbeat for `stale_s`. Returns their ids. `Worker.start` runs it."""
        t = self.t
        now = self.clock()
        with self.engine.connect() as c:
            rows = c.execute(select(t).where(t.c.state == "running")).all()
        lost: list[int] = []
        for r in rows:
            mine = r.owner is not None and str(r.owner).rsplit(":", 1)[0] == self.host
            dead = mine and r.owner_pid is not None and not oslib.pid_alive(r.owner_pid)
            stale = r.heartbeat_at is None or now - float(r.heartbeat_at) > stale_s
            if dead or stale:
                self.fail(Job.from_row(r), WorkerLost(f"worker {r.owner} stopped while running it"))
                lost.append(int(r.id))
        return lost

    def prune(self, *, older_than_s: float) -> int:
        """Delete finished jobs that ended more than `older_than_s` ago; returns how many."""
        t = self.t
        cutoff = self.clock() - older_than_s
        with self.engine.begin() as c:
            done = c.execute(delete(t).where(and_(t.c.state.in_(sorted(FINISHED)), t.c.finished_at < cutoff)))
        return int(done.rowcount)

    def run(self, job: Job, handler: Handler) -> Job:
        """Run `handler(job)` for a claimed job and record the outcome. Progress it reports is stored at most once a
        second; a failure is retried or failed by the rules above."""
        before = job.progress.done if job.progress is not None else 0.0
        started = time.monotonic()
        try:
            with reporting(ProgressReporter(lambda p: self.report(job, p))):
                result = handler(job)
        except Exception as e:
            now = job.progress.done if job.progress is not None else 0.0
            self.fail(job, e, progressed=now > before)
            return job
        if job.cancel_requested:
            self.finish(job, result)  # stopped because it was asked to: cancelled, with what it had done
        elif job.timeout_s is not None and time.monotonic() - started > job.timeout_s:
            now = job.progress.done if job.progress is not None else 0.0
            self.fail(job, TimeoutError(f"took longer than {job.timeout_s:g} s"), progressed=now > before)
        elif job.stop_event.is_set():
            self.release(job)  # the worker is shutting down: the job runs again, from the start, next time
        else:
            self.finish(job, result)
        return job

    def release(self, job: Job) -> None:
        """Put a running job back in the queue without counting an attempt (its worker is stopping)."""
        t = self.t
        with self.engine.begin() as c:
            c.execute(
                update(t)
                .where(self._mine(job))
                .values(
                    state="queued", owner=None, owner_pid=None, heartbeat_at=None, started_at=None, claim_token=None
                )
            )
        job.state = "queued"

    def step(self, handlers: Mapping[str, Handler]) -> Job | None:
        """Claim one job of a kind in `handlers` and run it here, now: a deterministic run loop for tests and CLIs."""
        job = self.claim(list(handlers))
        return None if job is None else self.run(job, handlers[job.kind])


class Worker:
    """Threads that claim and run jobs until stopped. Each running job gets a heartbeat every `heartbeat_s`, which also
    picks up a cancel request and asks a job past its timeout to stop.

    `isolate=True` (the default) runs jobs in job processes of their own (`tasks.child`), one per thread and kept
    between jobs, so a quick job costs milliseconds: a cancel, a timeout or `stop` kills the process and every tool it
    started at once (the next job gets a fresh one), a crash or a leak dies with it, and it dies with the server.
    `max_jobs` replaces a process after that many jobs. The handlers must be module-level functions (the process
    imports them by name); `isolate=False` runs them on this process's threads and asks them to stop through
    `job.stopping`. `lower_priority=True` runs the job processes at idle CPU and disk priority."""

    def __init__(
        self,
        queue: Queue,
        handlers: Mapping[str, Handler],
        *,
        concurrency: int = 1,
        poll_s: float = 1.0,
        heartbeat_s: float = 10.0,
        stale_s: float = 300.0,
        isolate: bool = True,
        lower_priority: bool = False,
        max_jobs: int | None = None,
    ) -> None:
        self.queue = queue
        self.handlers = dict(handlers)
        self.isolate = isolate
        self.lower_priority = lower_priority
        self.max_jobs = max_jobs
        self.slots: list[child.JobProcess] = []  # one job process per thread, kept between jobs
        # Checked now, so a handler a child could not import fails at start-up, not in the first job.
        self._specs = {k: child.handler_spec(fn) for k, fn in self.handlers.items()} if isolate else {}
        self.concurrency = concurrency
        self.poll_s = poll_s
        self.heartbeat_s = heartbeat_s
        self.stale_s = stale_s
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._running: dict[int, Job] = {}
        self._lock = threading.Lock()
        self.ran = 0  # jobs this worker has run, failed ones included

    def start(self) -> Worker:
        self.queue.recover(stale_s=self.stale_s)
        self._stop.clear()
        self._threads = [
            threading.Thread(target=self._loop, name=f"strata-worker-{i}", daemon=True) for i in range(self.concurrency)
        ]
        self._threads.append(threading.Thread(target=self._beat, name="strata-worker-heartbeat", daemon=True))
        for th in self._threads:
            th.start()
        return self

    def stop(self, timeout: float = 30.0) -> None:
        """Ask running jobs to stop, then wait up to `timeout` for the threads. A job still running at the end is
        recovered by the next `start` (its heartbeat goes stale, or its pid is gone)."""
        self._stop.set()
        with self._lock:
            for job in self._running.values():
                job.stop_event.set()
        deadline = time.monotonic() + timeout
        for th in self._threads:
            th.join(max(deadline - time.monotonic(), 0))

    def _loop(self) -> None:
        slot = None
        if self.isolate:
            slot = child.JobProcess(self._process_spec(), max_jobs=self.max_jobs)
            with self._lock:
                self.slots.append(slot)
            slot.ensure()  # started now, so the first job does not wait for imports
        try:
            self._claim_and_run(slot)
        finally:
            if slot is not None:
                slot.close()

    def _claim_and_run(self, slot: child.JobProcess | None) -> None:
        while not self._stop.is_set():
            job = self.queue.claim(list(self.handlers))
            if job is None:
                self._stop.wait(self.poll_s)
                continue
            with self._lock:
                self._running[job.id] = job
            try:
                if slot is not None:
                    self._run_isolated(job, slot)
                else:
                    self.queue.run(job, self.handlers[job.kind])
            finally:
                with self._lock:
                    self._running.pop(job.id, None)
                self.ran += 1

    def _beat(self) -> None:
        while not self._stop.wait(self.heartbeat_s):
            with self._lock:
                jobs = list(self._running.values())
            for job in jobs:
                with contextlib.suppress(Exception):  # a missed beat is retried next time; recover() allows stale_s
                    self.queue.heartbeat(job)
                if (
                    job.timeout_s is not None
                    and job.started_at is not None
                    and self.queue.clock() - job.started_at > job.timeout_s
                ):
                    job.stop_event.set()

    def _process_spec(self) -> Json:
        q = self.queue
        return {
            "url": q.engine.url.render_as_string(hide_password=False),
            "table": q.t.name,
            "schema": q.t.schema,
            "lower_priority": self.lower_priority,
        }

    def _run_isolated(self, job: Job, slot: child.JobProcess) -> None:
        """Run a claimed job in this slot's job process and record what happened (see `tasks.child`)."""
        q = self.queue
        before = job.progress.done if job.progress is not None else 0.0
        started = time.monotonic()

        def should_stop() -> str:
            if job.cancel_requested:
                return "cancel"
            if job.timeout_s is not None and time.monotonic() - started > job.timeout_s:
                return "timeout"
            return "stop" if job.stop_event.is_set() else ""

        cmd = {"id": job.id, "token": job.token, "handler": self._specs[job.kind]}
        outcome, ended = slot.run(cmd, should_stop)
        if outcome is not None and outcome.get("gone"):
            return  # this attempt was recovered before the process got to it: nothing of it to record
        fresh = q.get(job.id)  # the process wrote progress to the row: read it back for `progressed`
        if fresh is not None and fresh.token == job.token:
            job.progress = fresh.progress
        progressed = (job.progress.done if job.progress is not None else 0.0) > before
        if ended == "cancel":
            q.finish(job, None)  # recorded as cancelled, since it was asked to stop
        elif ended == "timeout":
            q.fail(job, TimeoutError(f"took longer than {job.timeout_s:g} s"), progressed=progressed)
        elif ended == "stop":
            q.release(job)  # the worker is shutting down: the job runs again, from the start, next time
        elif outcome is None:
            q.fail(job, WorkerLost("its job process exited before it finished"), progressed=progressed)
        elif outcome.get("ok"):
            q.finish(job, outcome.get("result"))
        else:
            q.fail(job, child.rebuild_error(outcome), progressed=progressed)
