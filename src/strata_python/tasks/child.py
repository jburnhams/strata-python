"""Queue jobs in long-lived processes of their own: what `Worker(isolate=True)` (the default) runs them in.

strata-listory runs each job in a child process in its own process group (its `start_job`, `watch_supervisor`,
`kill_job`), because a thread can only be asked to stop: a cancel, a timeout or a server shutdown then ends the job
and every tool it started (an ffmpeg, a model run) at once, a crash or a leak dies with the job instead of the server,
and the parent dying takes the job with it (the lifeline in `oslib.start_supervised`). This is that, for every app,
without listory's cost of a new interpreter per job: each `Worker` slot keeps one `JobProcess`, which imports the
app's handlers and opens its database once and then runs job after job, so a quick job costs a pipe round trip and
a read of its row. Only a kill (cancel, timeout, stop, or a job that took the process down) costs a new process,
and `max_jobs` replaces a process after that many jobs, for code that leaks.

The parent claims a job and writes `{"id", "token", "handler"}` as one line on the child's stdin; the child imports
the handler by name (`module:qualname`, so it must be a module-level function), runs it with progress going straight
to the job's row, and answers with one line: the result, or the error's type and message, which the parent rebuilds
so the retry rules (`tasks.retry`) treat it as they would in-process. The answers travel on the child's original
stdout; its fd 1 is pointed at stderr, so what handlers and their tools print goes to the server's log, not into
the protocol. stdin closing (the parent died) ends the process group at once, even mid-job.

In the child `job.stopping` stays False: the parent kills the process instead of asking."""

from __future__ import annotations

import builtins
import importlib
import json
import os
import queue as queue_mod
import subprocess
import sys
import threading
from collections.abc import Callable
from typing import IO, Any, cast

from sqlalchemy import MetaData, create_engine

from strata_python import oslib
from strata_python.tasks.progress import ProgressReporter, reporting
from strata_python.tasks.retry import RetryLater

ENV = "STRATA_JOB"
# Starts every answer. On Windows a tool the handler starts inherits the process's real stdout handle (moving fd 1
# does not move it), so other lines can reach the parent on the same pipe: those go on to the log.
MARK = "\x02strata-job:"  # not \x1c-\x1e: str.splitlines breaks lines there
Json = dict[str, Any]


def handler_spec(fn: Any) -> str:
    """The name a child imports `fn` by; ValueError when it cannot (a lambda, a closure, a method, `__main__`)."""
    spec = f"{getattr(fn, '__module__', '')}:{getattr(fn, '__qualname__', '')}"
    try:
        found = resolve(spec)
    except (ImportError, AttributeError, ValueError):
        found = None
    if found is not fn or spec.startswith("__main__:"):
        raise ValueError(
            f"handler {spec} cannot be imported by name in a job process: make it a module-level function, "
            "or run this kind in the server's process with Worker(isolate=False)"
        )
    return spec


def resolve(spec: str) -> Any:
    module, _, name = spec.partition(":")
    if not module or not name or "<" in name:
        raise ValueError(f"not a module:function name: {spec}")
    obj: Any = importlib.import_module(module)
    for part in name.split("."):
        obj = getattr(obj, part)
    return obj


def error_outcome(e: BaseException) -> Json:
    """An error as the parent rebuilds it: its type's name, the builtin class it derives from, and its message."""
    base = next(c for c in type(e).__mro__ if c.__module__ == "builtins")
    message = str(e.args[0]) if len(e.args) == 1 else str(e)  # KeyError('x') round-trips as 'x', not "'x'"
    out: Json = {"ok": False, "type": type(e).__name__, "base": base.__name__, "message": message}
    if isinstance(e, RetryLater):
        out["retry_later"] = True
        out["after_s"] = e.after_s
    return out


def rebuild_error(d: Json) -> BaseException:
    """The exception a child reported, as near the original as the parent can make it: the same builtin class (so a
    TypeError is still a programming error and fails at once), under the same name (so the job's error reads the
    same), else a RuntimeError of that name."""
    message = str(d.get("message", ""))
    if d.get("retry_later"):
        return RetryLater(message, after_s=d.get("after_s"))
    name = str(d.get("type") or "RuntimeError")
    base = getattr(builtins, str(d.get("base")), None)
    if not (isinstance(base, type) and issubclass(base, Exception)):
        base = RuntimeError
    for cls in (base if base.__name__ == name else type(name, (base,), {}), type(name, (RuntimeError,), {})):
        try:
            return cast("BaseException", cls(message))
        except Exception:  # noqa: S112 - a class whose constructor needs more arguments: try the next one
            continue
    return RuntimeError(message)  # pragma: no cover - RuntimeError's subclass always takes a message


def route(line: str, answers: queue_mod.Queue[str | None], log: IO[str]) -> None:
    """An answer to `answers`; anything else on the pipe (a tool's output) to `log`."""
    if line.startswith(MARK):
        answers.put(line[len(MARK) :])
    else:
        log.write(line)
        log.flush()


def run_one(queue: Any, cmd: Json, handlers: dict[str, Any]) -> Json:
    """Run the attempt `cmd` names and say what happened. `handlers` caches the imported functions."""
    job = queue.get(int(cmd["id"]))
    if job is None or job.state != "running" or job.token != cmd["token"]:
        return {"gone": True}  # recovered and claimed again before it got here: nothing to run or record
    try:
        name = str(cmd["handler"])
        handler = handlers.get(name) or handlers.setdefault(name, resolve(name))
        with reporting(ProgressReporter(lambda p: queue.report(job, p))):
            result = handler(job)
        return {"ok": True, "result": json.loads(json.dumps(result, default=str))}
    except Exception as e:  # the job's failure is its outcome; the parent decides what it means
        return error_outcome(e)


def main(
    commands: IO[str] | None = None,
    replies: IO[str] | None = None,
    on_eof: Callable[[], object] = oslib.terminate_own_group,
) -> int:
    """The job process: run each job named on stdin until it closes. `commands`, `replies` and `on_eof` are for
    running it in a test's own process."""
    spec = cast("Json", json.loads(os.environ.pop(ENV)))
    os.environ.pop(oslib.SUPERVISED_ENV, None)  # the lifeline is watched here; the job's own children are not
    if replies is None:
        replies = os.fdopen(os.dup(1), "w", encoding="utf8", buffering=1)
        os.dup2(2, 1)  # print() and tools write to the server's log, never into the answers
    lines: IO[str] = sys.stdin if commands is None else commands
    if spec.get("lower_priority"):
        oslib.lower_priority(io=True)
    from strata_python.tasks.queue import Queue, jobs_table  # here, not at import: queue imports this module

    engine = create_engine(str(spec["url"]))
    if engine.dialect.name == "sqlite":
        from strata_python.db.engine import sqlite_pragmas

        sqlite_pragmas(engine)
    queue = Queue(engine, jobs_table(MetaData(schema=spec.get("schema")), str(spec["table"])))
    inbox: queue_mod.Queue[str | None] = queue_mod.Queue()

    def read() -> None:  # a thread, so the parent closing the pipe is seen while a job runs, not after it
        for line in lines:
            inbox.put(line)
        inbox.put(None)
        on_eof()

    threading.Thread(target=read, name="strata-job-commands", daemon=True).start()
    handlers: dict[str, Any] = {}
    try:
        while (line := inbox.get()) is not None:
            replies.write(MARK + json.dumps(run_one(queue, cast("Json", json.loads(line)), handlers)) + "\n")
            replies.flush()
    finally:
        engine.dispose()
    return 0


class JobProcess:
    """The parent's end of one job process: started on first use, reused for each job, replaced after a kill."""

    def __init__(self, spec: Json, *, max_jobs: int | None = None) -> None:
        self.spec = spec
        self.max_jobs = max_jobs
        self.proc: subprocess.Popen[str] | None = None
        self.jobs = 0  # run by the current process
        self.started = 0  # processes started, for the overhead test and the status page
        self._answers: queue_mod.Queue[str | None] = queue_mod.Queue()

    def ensure(self) -> subprocess.Popen[str]:
        if self.proc is not None and self.proc.poll() is None and (self.max_jobs is None or self.jobs < self.max_jobs):
            return self.proc
        self.close()
        env = {
            **os.environ,
            ENV: json.dumps(self.spec),  # the environment, not argv: the URL can hold a password
            "PYTHONPATH": os.pathsep.join(p for p in sys.path if p),  # so it imports what this process can
        }
        proc = oslib.start_supervised([sys.executable, "-m", "strata_python.tasks.child"], env=env, text=True)
        answers: queue_mod.Queue[str | None] = queue_mod.Queue()
        out = proc.stdout
        assert out is not None  # noqa: S101 - start_supervised(text=True) pipes stdout

        def read() -> None:
            for line in out:
                route(line, answers, sys.stderr)
            answers.put(None)

        threading.Thread(target=read, name="strata-job-answers", daemon=True).start()
        self.proc, self._answers, self.jobs = proc, answers, 0
        self.started += 1
        return proc

    def run(self, cmd: Json, should_stop: Callable[[], str]) -> tuple[Json | None, str]:
        """Send one job and wait for its answer. Returns (answer, "") or, when `should_stop()` names a reason
        first, (None, reason) with the process killed; (None, "") when the process died without answering."""
        proc = self.ensure()
        assert proc.stdin is not None  # noqa: S101 - start_supervised pipes stdin
        try:
            proc.stdin.write(json.dumps(cmd) + "\n")
            proc.stdin.flush()
        except OSError:
            self.close()
            return None, ""
        self.jobs += 1
        while True:
            if reason := should_stop():
                self.close()
                return None, reason
            try:
                line = self._answers.get(timeout=0.05)
            except queue_mod.Empty:
                continue
            if line is None:
                self.close()  # it died mid-job; what it started goes with its group
                return None, ""
            return cast("Json", json.loads(line)), ""

    def close(self) -> None:
        if self.proc is not None:
            oslib.kill_group(self.proc)
            self.proc = None


if __name__ == "__main__":
    sys.exit(main())
