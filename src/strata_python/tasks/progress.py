"""Progress from deep inside a task, without passing a callback down every call.

A handler calls `report_progress(done, total, text)` wherever it is; the task runner (a `SingleFlight`, the durable
queue, or the app's own loop) binds a `ProgressReporter` around the call, which writes at most once a second (listory's
rule: a progress write per page of a 900-page book would be most of the job's database traffic) but always writes a
stage change and the last step. Outside any runner `report_progress` does nothing, so the same function runs from a
test or a CLI.

    with reporting(ProgressReporter(lambda p: store.set_progress(job_id, p))):
        handler(...)            # anywhere inside: report_progress(3, 40, "Page 3 of 40")
"""

from __future__ import annotations

import contextlib
import contextvars
import datetime
import threading
import time
from collections.abc import Callable, Generator

from strata_python.contracts import Progress

_current: contextvars.ContextVar[ProgressReporter | None] = contextvars.ContextVar("strata_progress", default=None)


class ProgressReporter:
    """Throttled progress writes. `write` gets a `Progress`; it is called from the task's own thread."""

    def __init__(
        self,
        write: Callable[[Progress], None],
        *,
        min_interval_s: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime.datetime] = lambda: datetime.datetime.now(datetime.UTC),
    ) -> None:
        self._write = write
        self.min_interval_s = min_interval_s
        self._clock = clock
        self._now = now
        self._last_at: float | None = None
        self._last_stage: str | None = None
        self._pending: Progress | None = None
        self.latest: Progress | None = None
        self._lock = threading.Lock()

    def report(self, done: float, total: float | None = None, text: str = "", stage: str | None = None) -> bool:
        """Record a step; True when it was written now. A step that is not written is kept, and `flush` writes it."""
        p = Progress(done=done, total=total, text=text, stage=stage, at=self._now())
        with self._lock:
            self.latest = p
            t = self._clock()
            due = (
                self._last_at is None
                or t - self._last_at >= self.min_interval_s
                or stage != self._last_stage
                or (total is not None and done >= total)
            )
            if not due:
                self._pending = p
                return False
            self._last_at, self._last_stage, self._pending = t, stage, None
        self._write(p)
        return True

    def flush(self) -> None:
        """Write the last step if it was held back (call when the task ends, so its final state is stored)."""
        with self._lock:
            p, self._pending = self._pending, None
            if p is not None:
                self._last_at = self._clock()
        if p is not None:
            self._write(p)


@contextlib.contextmanager
def reporting(reporter: ProgressReporter) -> Generator[ProgressReporter]:
    """Bind `reporter` for the block (this thread and context only), flushing it at the end."""
    token = _current.set(reporter)
    try:
        yield reporter
    finally:
        _current.reset(token)
        reporter.flush()


def report_progress(done: float, total: float | None = None, text: str = "", stage: str | None = None) -> None:
    """Report progress to the runner of the current task; nothing happens outside one."""
    r = _current.get()
    if r is not None:
        r.report(done, total, text, stage)
