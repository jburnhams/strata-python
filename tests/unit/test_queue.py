"""The durable queue on real SQLite files and a real throwaway Postgres, with real worker threads."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator

import pytest
from sqlalchemy import MetaData

import strata_python.db as db
from strata_python.contracts import Progress
from strata_python.db import guard, testserver
from strata_python.db.scratch import scratch_database
from strata_python.db.testing import scratch_engine
from strata_python.tasks import RetryLater, report_progress
from strata_python.tasks.queue import Job, JobCancelled, Queue, Worker, WorkerLost, dedupe_key, jobs_table

# As in test_db.py: CI sets STRATA_TEST_PG=1, so a missing Postgres fails there instead of skipping.
PG = os.environ.get("STRATA_TEST_PG") == "1" or (os.name != "nt" and os.geteuid() != 0 and testserver.available())
PG = PG or bool(os.environ.get(guard.TEST_ADMIN_URL))
BACKENDS = ["sqlite", pytest.param("postgres", marks=pytest.mark.skipif(not PG, reason="no throwaway Postgres"))]


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture(params=BACKENDS)
def setup(request: pytest.FixtureRequest) -> Iterator[tuple[Queue, Clock, MetaData]]:
    meta = MetaData(naming_convention=db.NAMING_CONVENTION)
    table = jobs_table(meta)
    with scratch_database(request.param == "postgres") as url:
        eng = scratch_engine(url)
        meta.create_all(eng)
        clock = Clock()
        try:
            yield Queue(eng, table, clock=clock, base_s=10, cap_s=100), clock, meta
        finally:
            eng.dispose()


def test_enqueue_claim_finish_and_page(setup):
    q, clock, _ = setup
    a = q.enqueue("ocr", {"page": 1}, domain="book:1")
    b = q.enqueue("ocr", {"page": 2}, domain="book:1", priority=5)
    c = q.enqueue("faces", {}, delay_s=60)
    assert q.claim(["faces"]) is None  # held back by its delay
    job = q.claim()
    assert job is not None
    assert (job.id, job.state, job.options, job.domain) == (b, "running", {"page": 2}, "book:1")  # priority first
    q.finish(job, {"lines": 3})
    got = q.get(b)
    assert (got.state, got.result, got.finished_at) == ("done", {"lines": 3}, clock.t)
    assert q.get(999) is None
    first = q.page(limit=2)
    assert [t.id for t in first.items] == [c, b]
    assert first.next == str(b)
    rest = q.page(limit=2, after=first.next)
    assert ([t.id for t in rest.items], rest.next) == ([a], None)
    assert [t.id for t in q.page(state="done").items] == [b]
    assert [t.id for t in q.page(kind="faces").items] == [c]
    assert [t.id for t in q.page(domain="book:1", state=["queued", "done"]).items] == [b, a]
    task = q.page(state="done").items[0]
    assert (task.state, task.kind, task.result, task.finished_at.timestamp()) == ("done", "ocr", {"lines": 3}, clock.t)
    assert q.counts() == {"queued": 2, "done": 1}
    clock.t += 61
    assert q.claim(["faces"]).id == c


def test_dedupe_returns_the_same_job_while_it_is_active(setup):
    q, _, _ = setup
    a = q.enqueue("scan", {"folder": "/x"}, dedupe=True)
    assert q.enqueue("scan", {"folder": "/x"}, dedupe=True) == a
    assert q.enqueue("scan", {"folder": "/y"}, dedupe=True) != a
    assert q.enqueue("scan", {"folder": "/x"}) != a  # without dedupe: always a new one
    q.finish(q.claim(), None)
    assert q.enqueue("scan", {"folder": "/x"}, dedupe=True) != a  # finished, so a new run
    assert dedupe_key("k", None, {"a": 1, "b": 2}) == dedupe_key("k", None, {"b": 2, "a": 1})


def test_a_held_resource_keeps_the_next_job_waiting(setup):
    q, _, _ = setup
    gpu1 = q.enqueue("ocr", resources=["gpu"])
    gpu2 = q.enqueue("faces", resources=["gpu", "photos"])
    other = q.enqueue("hash", resources=["disk"])
    first = q.claim()
    assert first.id == gpu1
    assert q.claim().id == other  # the GPU is held, so the hash job goes first
    assert q.claim() is None
    q.finish(first)
    assert q.claim().id == gpu2


def test_jobs_waiting_on_a_resource_do_not_hide_the_ones_behind_them(setup):
    """Review on PR #25: candidates were read 200 at a time and no further, so 200 jobs waiting for the GPU stalled
    every job after them. Here the batch is 2 and four jobs wait."""
    q, _, _ = setup
    q.batch = 2
    holder = q.enqueue("ocr", resources=["gpu"], priority=9)
    for i in range(4):  # two priorities, so the next batch has to start after (priority, id), not just id
        q.enqueue("ocr", resources=["gpu"], priority=i % 2)
    free = q.enqueue("hash", resources=["disk"])
    assert q.claim().id == holder
    assert q.claim().id == free  # found in the third batch, behind four that wait for the GPU
    assert q.claim() is None


def test_claims_from_another_queue_wait_for_the_one_in_progress(setup):
    """Review on PR #25: a lock in this process does not reach another process or Queue. The second claimer must wait
    while the first is between reading the held resources and claiming (the write lock on SQLite, the advisory lock
    on Postgres)."""
    q, clock, _ = setup
    q.enqueue("ocr", resources=["gpu"])
    q.enqueue("faces", resources=["gpu"])
    other = Queue(q.engine, q.t, clock=clock, owner="another-host:1")
    got: list[Job | None] = []
    with q._serialised():
        t = threading.Thread(target=lambda: got.append(other.claim()))
        t.start()
        t.join(0.5)
        assert t.is_alive()  # waiting on the claim in progress, not reading the resources around it
    t.join(20)
    assert len(got) == 1
    assert got[0] is not None
    assert q.claim() is None  # the GPU is now held by the other queue's job


def test_a_recovered_attempt_cannot_write_over_its_replacement(setup):
    """Review on PR #25: a worker that was only slow, whose job was recovered and claimed again, must not finish,
    fail, release or report on the new attempt; its heartbeat tells it to stop."""
    q, clock, _ = setup
    failed: list[int] = []
    q.on_fail = lambda _c, job, _e: failed.append(job.id)
    jid = q.enqueue("ocr")
    slow = q.claim()
    clock.t += 301
    assert q.recover(stale_s=300) == [jid]
    clock.t += 3600  # past the backoff
    fresh = Queue(q.engine, q.t, clock=clock, owner="another-host:1").claim()
    assert fresh is not None
    assert (fresh.id, fresh.token != slow.token) == (jid, True)
    q.report(slow, Progress(done=9, total=10))
    q.finish(slow, {"old": True})
    q.release(slow)
    q.retries = 0
    q.fail(slow, OSError("late"))
    got = q.get(jid)
    assert (got.state, got.result, got.progress, got.token) == ("running", None, None, fresh.token)
    assert failed == []
    assert not q.heartbeat(slow)  # its job is someone else's now
    assert slow.stopping
    assert q.heartbeat(fresh)


def test_failures_retry_with_backoff_then_fail_and_call_on_fail(setup):
    q, clock, _ = setup
    seen = []
    q.on_fail = lambda conn, job, error: seen.append((job.id, error))
    q.retries = 2
    jid = q.enqueue("fetch")
    d = q.fail(q.claim(), OSError("connection reset"))
    assert (d.state, d.attempts) == ("retry", 1)
    row = q.get(jid)
    assert (row.state, row.attempts, row.error) == ("queued", 1, "OSError: connection reset")
    assert q.claim() is None  # its backoff (10 s, with jitter)
    clock.t += 12
    q.fail(q.claim(), OSError("again"))
    clock.t += 30
    d = q.fail(q.claim(), OSError("and again"))
    assert (d.state, q.get(jid).state, q.get(jid).finished_at) == ("failed", "failed", clock.t)
    assert seen == [(jid, "OSError: and again")]
    assert q.retry(jid)
    assert (q.get(jid).state, q.get(jid).attempts) == ("queued", 0)
    assert not q.retry(jid)
    bug = q.enqueue("bug")
    q.claim(["bug"])
    assert q.fail(q.get(bug), TypeError("bad operand")).state == "failed"  # a programming error: at once


def test_step_runs_handlers_with_progress_retry_later_and_progress_resets(setup):
    q, clock, _ = setup
    calls = []

    def ocr(job: Job):
        calls.append(job.attempts)
        report_progress(len(calls) * 10, 100, f"line {len(calls) * 10}")
        if len(calls) == 1:
            raise RetryLater("model loading", after_s=5)
        if len(calls) < 4:
            raise OSError("died")  # each attempt got further, so the count stays at one
        return {"ok": True}

    jid = q.enqueue("ocr")
    assert q.step({"ocr": ocr}).state == "queued"
    assert q.get(jid).run_after == clock.t + 5
    assert q.step({"ocr": ocr}) is None
    for _ in range(3):
        clock.t += 1000
        q.step({"ocr": ocr})
    row = q.get(jid)
    assert (row.state, row.result, row.progress.text) == ("done", {"ok": True}, "line 40")
    assert calls == [0, 1, 1, 1]


def test_cancel_queued_at_once_and_running_cooperatively(setup):
    q, _, _ = setup
    queued = q.enqueue("x")
    assert q.cancel(queued) == "cancelled"
    assert q.cancel(12345) is None
    running = q.enqueue("x")

    def slow(job: Job):
        q.cancel(job.id)
        assert not q.heartbeat(job)  # the heartbeat picks up the request
        assert job.stopping
        return {"done": 2}

    q.step({"x": slow})
    assert (q.get(running).state, q.get(running).result) == ("cancelled", {"done": 2})
    third = q.enqueue("x")

    def raising(job: Job):
        q.cancel(job.id)
        q.heartbeat(job)
        q.check(job)

    q.step({"x": raising})
    assert q.get(third).state == "cancelled"  # raised JobCancelled: not retried
    with pytest.raises(JobCancelled):
        q.check(q.get(third))


def test_recover_fails_jobs_whose_worker_is_gone(setup):
    q, clock, _ = setup
    a = q.enqueue("x")
    b = q.enqueue("y")
    c = q.enqueue("z")
    dead = Queue(q.engine, q.t, clock=clock, owner=f"{q.host}:999999999")
    ja = dead.claim(["x"])
    with q.engine.begin() as conn:  # the dead owner's pid, as claim recorded its own
        conn.execute(q.t.update().where(q.t.c.id == ja.id).values(owner_pid=999_999_999))
    elsewhere = Queue(q.engine, q.t, clock=clock, owner="another-host:1")
    elsewhere.claim(["y"])
    mine = q.claim(["z"])
    assert q.recover(stale_s=300) == [a]  # b's host is not this one, and it is not stale yet
    assert (q.get(a).state, q.get(a).error) == (
        "queued",
        f"WorkerLost: worker {q.host}:999999999 stopped while running it",
    )
    clock.t += 301
    q.heartbeat(mine)
    assert q.recover(stale_s=300) == [b]
    assert q.get(c).state == "running"
    assert issubclass(WorkerLost, RuntimeError)


def test_prune_deletes_old_finished_jobs(setup):
    q, clock, _ = setup
    old = q.enqueue("x")
    q.finish(q.claim())
    clock.t += 1000
    keep = q.enqueue("x")
    q.finish(q.claim())
    q.enqueue("x")  # queued: never pruned
    assert q.prune(older_than_s=500) == 1
    assert q.get(old) is None
    assert q.get(keep) is not None


def test_worker_threads_run_jobs_and_time_out_and_stop(setup):
    q, _, _ = setup
    q.clock = time.time  # the worker's heartbeat compares real time
    gate = threading.Event()
    done = []

    def quick(job: Job):
        done.append(job.options["n"])
        return job.options["n"]

    def forever(job: Job):
        while not job.stopping:
            gate.wait(0.01)
        return "stopped"

    for n in range(6):
        q.enqueue("quick", {"n": n})
    late = q.enqueue("forever", timeout_s=0.2)
    w = Worker(q, {"quick": quick, "forever": forever}, concurrency=2, poll_s=0.02, heartbeat_s=0.05).start()
    deadline = time.monotonic() + 20
    while q.get(late).attempts == 0 or len(done) < 6:
        assert time.monotonic() < deadline, q.counts()
        time.sleep(0.02)
    assert sorted(done) == list(range(6))
    assert q.get(late).error == "TimeoutError: took longer than 0.2 s"
    shutdown = q.enqueue("forever")
    while q.get(shutdown).state != "running":
        assert time.monotonic() < deadline
        time.sleep(0.02)
    w.stop(timeout=10)
    assert q.get(shutdown).state == "queued"  # released, not failed: it runs again next start
    assert q.get(shutdown).attempts == 0
    assert w.ran >= 7
