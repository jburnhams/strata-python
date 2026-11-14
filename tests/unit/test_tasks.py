"""The task primitives with real threads, files and processes."""

import datetime
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from strata_python import oslib
from strata_python.tasks import (
    Conflict,
    Detached,
    ProgressReporter,
    RetryLater,
    SingleFlight,
    StatusFile,
    Ticker,
    backoff_s,
    next_state,
    report_progress,
    reporting,
    write_json_atomic,
)

# --- retry ----------------------------------------------------------------------------------------------------------


def test_backoff_doubles_to_a_cap_with_jitter():
    mid = lambda: 0.5  # noqa: E731 - no jitter at the middle of the range
    assert [backoff_s(n, rng=mid) for n in (1, 2, 3)] == [30, 60, 120]
    assert backoff_s(20, rng=mid) == 3600
    assert backoff_s(1, rng=lambda: 1.0) == pytest.approx(33)
    assert backoff_s(1, rng=lambda: 0.0) == pytest.approx(27)


def test_next_state_retries_outside_failures_and_fails_bugs_at_once():
    mid = lambda: 0.5  # noqa: E731
    d = next_state(0, TimeoutError("read timed out"), rng=mid)
    assert (d.state, d.attempts, d.after_s, d.error) == ("retry", 1, 30, "TimeoutError: read timed out")
    assert next_state(4, OSError("disk"), retries=5, rng=mid).state == "retry"
    assert next_state(5, OSError("disk"), retries=5).state == "failed"
    bug = next_state(0, TypeError("unsupported operand"))
    assert (bug.state, bug.after_s) == ("failed", None)
    assert next_state(0, ValueError("404"), retryable=lambda e: "404" not in str(e)).state == "failed"


def test_progress_resets_the_count_and_retry_later_names_its_pause():
    assert next_state(5, OSError("died near the end"), progressed=True, retries=5).attempts == 1
    d = next_state(0, RetryLater("rate limited", after_s=61))
    assert (d.state, d.after_s, d.error) == ("retry", 61, "RetryLater: rate limited")
    assert next_state(0, RetryLater(), rng=lambda: 0.5).after_s == 30
    assert next_state(5, RetryLater(), retries=5).state == "failed"


# --- progress -------------------------------------------------------------------------------------------------------


def test_reporter_throttles_but_keeps_stage_changes_and_the_end():
    written, t = [], [0.0]
    r = ProgressReporter(written.append, min_interval_s=1.0, clock=lambda: t[0])
    assert r.report(1, 10, "1 of 10")
    assert not r.report(2, 10)  # within the second: held
    t[0] = 0.5
    assert r.report(3, 10, stage="ocr")  # a new stage is written at once
    assert r.report(10, 10)  # the last step too
    t[0] = 5
    assert [p.done for p in written][:3] == [1, 3, 10]
    assert written[1].stage == "ocr"
    assert written[0].at.tzinfo is datetime.UTC


def test_report_progress_reaches_the_bound_reporter_and_flushes():
    written = []
    report_progress(1, 2)  # outside a runner: nothing happens
    with reporting(ProgressReporter(written.append, min_interval_s=60)):
        report_progress(1, 5)
        report_progress(2, 5, "page 2")  # held back by the interval...
    assert [p.text for p in written] == ["", "page 2"]  # ...and written when the block ends


# --- single flight --------------------------------------------------------------------------------------------------


def test_single_flight_runs_one_per_key_and_stops_cooperatively():
    flights: SingleFlight[str] = SingleFlight("scan")
    go = threading.Event()

    def scan(flight):
        report_progress(1, 3, "one")
        while not flight.stopping:
            go.wait(0.01)
        return "partial"

    f = flights.start("photos", scan)
    with pytest.raises(Conflict, match="scan photos is already running") as e:
        flights.start("photos", scan)
    assert e.value.payload["state"] == "running"
    other = flights.start("music", lambda flight: 42)
    assert other.join(5)
    assert (other.state, other.result) == ("done", 42)
    assert flights.running("photos")
    assert flights.stop("photos")
    assert f.join(5)
    assert (f.state, f.result, f.progress.text) == ("stopped", "partial", "one")
    assert not flights.stop("photos")
    assert flights.get("photos") is f
    assert set(flights.all()) == {"photos", "music"}
    assert f.as_json()["finished_at"] is not None
    again = flights.start("photos", lambda flight: 1)  # finished, so it can start again
    assert again.join(5)


def test_a_failed_flight_keeps_its_error():
    flights: SingleFlight[int] = SingleFlight("job")

    def boom(flight):
        raise OSError("disk full")

    f = flights.start(1, boom)
    assert f.join(5)
    assert (f.state, f.error) == ("failed", "OSError: disk full")
    assert not flights.running(2)
    assert flights.get(2) is None


# --- status files and detached processes ----------------------------------------------------------------------------


def test_status_file_round_trip_and_a_dead_writer(tmp_path):
    s = StatusFile(str(tmp_path / "x" / "status.json"))
    assert s.read() is None
    assert not s.running()
    s.write("running", project="a")
    d = s.read()
    assert (d["state"], d["pid"], d["alive"], d["project"]) == ("running", os.getpid(), True, "a")
    s.update(progress={"done": 1})
    assert s.read()["progress"] == {"done": 1}
    assert s.running()
    s.write("running", pid=999_999_999)
    assert s.read()["state"] == "died"
    (tmp_path / "x" / "status.json").write_text("[1]")
    assert s.read() is None


def test_write_json_atomic_leaves_no_temp_file_on_failure(tmp_path):
    path = tmp_path / "a.json"
    write_json_atomic(str(path), {"a": 1})
    loop: dict = {}
    loop["self"] = loop
    with pytest.raises(ValueError, match="Circular"):
        write_json_atomic(str(path), loop)
    assert json.loads(path.read_text()) == {"a": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["a.json"]


CHILD = """
import sys, time
from strata_python.tasks import StatusFile
s = StatusFile.from_env()
s.update(progress={"done": 1})
if sys.argv[1] == "wait":
    time.sleep(60)
s.update(state="done", output="out.mp4")
print("finished")
"""


def test_detached_runs_a_real_process_that_reports_through_its_file(tmp_path):
    script = tmp_path / "child.py"
    script.write_text(CHILD)
    job = Detached([sys.executable, str(script), "quick"], str(tmp_path / "s.json"), str(tmp_path / "logs" / "a.log"))
    pid = job.start(project="p")
    deadline = time.monotonic() + 20
    while (job.read() or {}).get("state") != "done":
        assert time.monotonic() < deadline, job.read()
        time.sleep(0.05)
    d = job.read()
    assert (d["output"], d["progress"], d["project"], d["pid"]) == ("out.mp4", {"done": 1}, "p", pid)
    assert "finished" in (tmp_path / "logs" / "a.log").read_text()
    assert not job.stop()  # not running


def test_a_child_that_finishes_at_once_is_not_written_over(tmp_path):
    """Review on PR #25: the parent wrote "running" after starting the child, so a child that was quicker could have
    its "done" replaced and then read as died. The parent now writes under the file's lock, taken before the child
    exists, and the child's update waits for it."""
    script = tmp_path / "child.py"
    script.write_text(CHILD)
    status = StatusFile(str(tmp_path / "s.json"))
    job = Detached([sys.executable, str(script), "quick"], status, str(tmp_path / "a.log"))
    pid = job.start(project="p")
    deadline = time.monotonic() + 20
    while (job.read() or {}).get("state") == "running":
        assert time.monotonic() < deadline
        time.sleep(0.02)
    d = job.read()
    assert (d["state"], d["output"], d["pid"]) == ("done", "out.mp4", pid)


def test_status_file_updates_wait_for_its_lock_across_processes(tmp_path):
    status = StatusFile(str(tmp_path / "s.json"))
    status.write("running", n=1)
    code = "import sys; from strata_python.tasks import StatusFile\nStatusFile(sys.argv[1]).update(state='done')\n"
    with status.locked():
        p = subprocess.Popen([sys.executable, "-c", code, status.path])
        time.sleep(1.0)
        assert p.poll() is None  # waiting for the lock
        assert status._load()["state"] == "running"
        write_json_atomic(status.path, {**status._load(), "n": 2})
    assert p.wait(20) == 0
    assert (status.read()["state"], status.read()["n"]) == ("done", 2)  # merged on top, nothing lost


def test_detached_refuses_a_second_start_and_stops_the_first(tmp_path):
    script = tmp_path / "child.py"
    script.write_text(CHILD)
    job = Detached([sys.executable, str(script), "wait"], str(tmp_path / "s.json"), str(tmp_path / "a.log"))
    pid = job.start()
    with pytest.raises(Conflict, match="already running"):
        job.start()
    assert job.stop()
    assert job.read()["state"] == "stopped"
    assert not oslib.pid_alive(pid) or job.read()["state"] == "stopped"
    job2 = Detached(
        [sys.executable, str(script), "wait"], StatusFile(str(tmp_path / "s.json")), str(tmp_path / "a.log")
    )
    job2.start()  # the file says stopped, so it can start again
    assert job2.stop()


def test_status_from_env_needs_a_parent(monkeypatch):
    monkeypatch.delenv("STRATA_STATUS_FILE", raising=False)
    with pytest.raises(RuntimeError, match="not started by Detached"):
        StatusFile.from_env()


# --- ticker ---------------------------------------------------------------------------------------------------------


def test_ticker_survives_its_errors_and_stops_promptly():
    calls, errors = [], []

    def tick():
        calls.append(1)
        if len(calls) == 2:
            raise OSError("backup disk missing")

    t = Ticker(0.01, tick, name="sweep", on_error=errors.append, immediately=True)
    with t:
        assert t.start() is t  # already running: the same one
        deadline = time.monotonic() + 5
        while len(calls) < 4:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert t.running
    assert not t.running
    assert [str(e) for e in errors] == ["backup disk missing"]
    assert t.last_error is None
    assert t.ticks >= 4
    slow = Ticker(3600, tick, name="hourly").start()
    started = time.monotonic()
    slow.stop()
    assert time.monotonic() - started < 2  # woke from the hour's wait


def test_ticker_logs_when_no_handler_is_given(caplog):
    def tick():
        raise OSError("nope")

    t = Ticker(0.01, tick, name="poller", immediately=True).start()
    deadline = time.monotonic() + 5
    while not t.ticks:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    t.stop()
    assert "poller failed" in caplog.text
    assert isinstance(t.last_error, OSError)
