"""The durable queue on real SQLite files and a real throwaway Postgres, with real worker threads."""

from __future__ import annotations

import io
import json
import os
import queue as queue_mod
import threading
import time
from collections.abc import Iterator

import job_handlers
import pytest
from sqlalchemy import MetaData

import strata_python.db as db
from strata_python import oslib
from strata_python.contracts import Progress
from strata_python.db import guard, testserver
from strata_python.db.scratch import scratch_database
from strata_python.db.testing import scratch_engine
from strata_python.tasks import RetryLater, child, report_progress
from strata_python.tasks.queue import ACTIVE, Job, JobCancelled, Queue, Worker, WorkerLost, dedupe_key, jobs_table

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
    w = Worker(
        q, {"quick": quick, "forever": forever}, concurrency=2, poll_s=0.02, heartbeat_s=0.05, isolate=False
    ).start()
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


# --- jobs in their own process (Worker(isolate=True), tasks.child) ---------------------------------------------------


def wait_for(cond, what, timeout=30.0):
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, what()
        time.sleep(0.02)


def test_isolated_jobs_run_in_their_own_process_and_fail_as_they_would_in_this_one(setup):
    q, _, _ = setup
    q.clock = time.time
    ids = {k: q.enqueue(k, {"n": 4} if k == "count" else {}) for k in HANDLERS}
    w = Worker(q, HANDLERS, concurrency=3, poll_s=0.02, heartbeat_s=0.05).start()
    try:
        wait_for(
            lambda: all(
                q.get(i).state != "running" and q.get(i).attempts + (q.get(i).state == "done") > 0 for i in ids.values()
            ),
            q.counts,
        )
    finally:
        w.stop(timeout=20)
    got = {k: q.get(i) for k, i in ids.items()}
    assert got["count"].state == "done"
    assert got["count"].result["n"] == 4
    assert got["count"].result["pid"] != os.getpid()  # it ran in a process of its own
    assert (got["count"].progress.done, got["count"].progress.text) == (4, "4 of 4")
    assert (got["outside"].state, got["outside"].error) == (
        "queued",
        "FileNotFoundError: [Errno 2] No such file",
    )  # retried
    assert got["outside"].progress.text == "half"
    assert (got["bug"].state, got["bug"].error) == ("failed", "TypeError: a bug")  # a bug fails at once, as here
    assert (got["missing"].state, got["missing"].error) == ("queued", "KeyError: 'book'")  # reads as it would here
    assert got["unbuildable"].error == "Unbuildable: needs two"
    later = got["later"]
    assert (later.state, later.error) == ("queued", "RetryLater: file still being written")
    assert later.run_after - time.time() == pytest.approx(5, abs=3)  # its own pause, not the backoff
    assert (got["crash"].state, got["crash"].error) == (
        "queued",
        "WorkerLost: its job process exited before it finished",
    )


HANDLERS = {
    "count": job_handlers.count,
    "outside": job_handlers.outside,
    "bug": job_handlers.bug,
    "missing": job_handlers.missing,
    "unbuildable": job_handlers.unbuildable,
    "later": job_handlers.later,
    "crash": job_handlers.crash,
}


def _pids(path):
    wait_for(lambda: path.exists() and len(path.read_text().split()) == 2, lambda: "no pids yet")
    return [int(p) for p in path.read_text().split()]


@pytest.mark.parametrize("how", ["cancel", "timeout", "stop"])
def test_cancel_timeout_and_stop_kill_an_isolated_job_and_its_tools(setup, tmp_path, how):
    """A thread can only be asked; a job process and the tool it started are killed, at once."""
    q, _, _ = setup
    q.clock = time.time
    pids = tmp_path / "pids"
    jid = q.enqueue("spawn", {"pids": str(pids)}, timeout_s=1.5 if how == "timeout" else None)
    w = Worker(q, {"spawn": job_handlers.spawn_and_wait}, poll_s=0.02, heartbeat_s=0.05).start()
    try:
        worker, tool = _pids(pids)
        assert oslib.pid_alive(worker)
        assert oslib.pid_alive(tool)
        if how == "cancel":
            q.cancel(jid)
        if how == "stop":
            w.stop(timeout=20)
        wait_for(lambda: q.get(jid).state != "running", q.counts)
    finally:
        w.stop(timeout=20)
    wait_for(lambda: not oslib.pid_alive(worker) and not oslib.pid_alive(tool), lambda: (worker, tool))
    job = q.get(jid)
    if how == "cancel":
        assert job.state == "cancelled"
    elif how == "timeout":
        assert (job.state, job.error) == ("queued", "TimeoutError: took longer than 1.5 s")
    else:
        assert (job.state, job.attempts) == ("queued", 0)  # released: it runs again at the next start


def test_isolated_workers_refuse_handlers_a_process_cannot_import(setup):
    q, _, _ = setup

    def local(job: Job) -> None: ...

    for fn in (local, lambda job: None):
        with pytest.raises(ValueError, match="cannot be imported by name"):
            Worker(q, {"x": fn})
    Worker(q, {"x": local}, isolate=False)  # in this process it is fine


def test_child_main_runs_the_named_attempts_and_skips_a_replaced_one(setup, monkeypatch):
    """The job process's loop, in this process so its lines are measured; the tests above run it for real."""
    q, _, _ = setup
    a = q.enqueue("count", {"n": 2})
    b = q.enqueue("bug")
    ja, jb = q.claim(), q.claim()
    spec = {"url": q.engine.url.render_as_string(hide_password=False), "table": q.t.name, "schema": None}
    monkeypatch.setenv(child.ENV, json.dumps(spec))
    cmds = [
        {"id": a, "token": ja.token, "handler": child.handler_spec(job_handlers.count)},
        {"id": a, "token": "another-attempt", "handler": child.handler_spec(job_handlers.count)},
        {"id": b, "token": jb.token, "handler": child.handler_spec(job_handlers.bug)},
    ]
    eof = threading.Event()
    replies = io.StringIO()
    assert child.main(io.StringIO("".join(json.dumps(c) + "\n" for c in cmds)), replies, eof.set) == 0
    assert eof.wait(5)
    lines = replies.getvalue().splitlines()
    assert all(line.startswith(child.MARK) for line in lines)
    assert [json.loads(line[len(child.MARK) :]) for line in lines] == [
        {"ok": True, "result": {"n": 2, "pid": os.getpid()}},
        {"gone": True},
        {"ok": False, "type": "TypeError", "base": "TypeError", "message": "a bug"},
    ]
    assert q.get(a).progress.done == 2


def test_job_processes_are_reused_and_replaced_only_when_killed(setup, tmp_path):
    """Jonathan: quick jobs cannot pay for a new interpreter each. One process per thread runs job after job; a cancel
    costs a new one; `max_jobs` replaces one on purpose. What handlers print stays out of the answers."""
    q, _, _ = setup
    q.clock = time.time
    handlers = {"pid": job_handlers.pid, "chatty": job_handlers.chatty, "spawn": job_handlers.spawn_and_wait}
    w = Worker(q, handlers, poll_s=0.01, heartbeat_s=0.05).start()
    try:
        wait_for(lambda: len(w.slots) == 1 and w.slots[0].proc is not None, lambda: w.slots)
        slot = w.slots[0]
        n = 40
        started = time.monotonic()
        ids = [q.enqueue("pid") for _ in range(n)]
        wait_for(lambda: q.get(ids[-1]).state == "done", q.counts)
        per_job = (time.monotonic() - started) / n
        pids = {q.get(i).result for i in ids}
        assert len(pids) == 1  # one process ran them all
        assert pids != {os.getpid()}
        assert per_job < 0.25, per_job  # a pipe round trip and a few row writes, not an interpreter start
        chat = q.enqueue("chatty")
        wait_for(lambda: q.get(chat).state not in ACTIVE, lambda: (q.counts(), q.get(chat).error))
        assert (q.get(chat).state, q.get(chat).error) == ("done", None)
        assert q.get(chat).result in pids  # printing did not break the protocol or the process
        long = q.enqueue("spawn", {"pids": str(tmp_path / "pids")})
        _pids(tmp_path / "pids")
        q.cancel(long)
        wait_for(lambda: q.get(long).state == "cancelled", q.counts)
        after = q.enqueue("pid")
        wait_for(lambda: q.get(after).state == "done", q.counts)
        assert q.get(after).result not in pids  # the killed process was replaced
        assert slot.started == 2
    finally:
        w.stop(timeout=20)
    assert slot.proc is None  # stopped with the worker


def test_max_jobs_replaces_a_process_after_that_many(setup):
    q, _, _ = setup
    q.clock = time.time
    w = Worker(q, {"pid": job_handlers.pid}, poll_s=0.01, heartbeat_s=0.05, max_jobs=2).start()
    try:
        ids = [q.enqueue("pid") for _ in range(5)]
        wait_for(lambda: q.get(ids[-1]).state == "done", q.counts)
    finally:
        w.stop(timeout=20)
    got = [q.get(i).result for i in ids]
    assert got[0] == got[1] != got[2] == got[3] != got[4]


def test_only_marked_lines_are_answers_the_rest_goes_to_the_log():
    """On Windows a tool's output can reach the answer pipe (it inherits the real stdout handle): CI caught it."""
    answers: queue_mod.Queue[str | None] = queue_mod.Queue()
    logged = io.StringIO()
    child.route("a line from a tool\n", answers, logged)
    child.route(child.MARK + '{"ok": true}\n', answers, logged)
    assert logged.getvalue() == "a line from a tool\n"
    assert answers.get_nowait() == '{"ok": true}\n'
    assert answers.empty()


def test_rebuilt_errors_keep_their_class_and_name():
    e = child.rebuild_error(child.error_outcome(job_handlers.Unbuildable("a", "b")))
    assert (type(e).__name__, str(e), isinstance(e, Exception)) == ("Unbuildable", "a b", True)
    e = child.rebuild_error(child.error_outcome(UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")))
    assert (type(e).__name__, isinstance(e, RuntimeError)) == ("UnicodeDecodeError", True)  # needs five arguments
    assert type(child.rebuild_error({"type": "X", "base": "print", "message": "m"})).__mro__[1] is RuntimeError
    with pytest.raises(ValueError, match="not a module:function"):
        child.resolve("nothing")
