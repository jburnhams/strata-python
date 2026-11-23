"""Run a function every so often on a daemon thread: the backup sweep, the prefetch loop, the GPU poller.

Five apps wrote this loop by hand (`while not stop.wait(every): fn()`), and most of them let one exception end the
loop for the rest of the server's life. A `Ticker` catches it, hands it to `on_error` (by default the log) and keeps
going, and stops promptly: `stop()` wakes the wait instead of sleeping out the interval.

    with Ticker(300, sweep, name="sweep"):     # or .start() at startup and .stop() at shutdown
        serve(app)

A loop whose next wait depends on what it found (a prefetcher that waits a short time after work, longer when there
was none, much longer when rate-limited) returns that wait from `fn` in seconds; `None`, or anything that is not a
number, keeps `every_s`, as does a failure. `poke()` runs it now when something else knows there is work (a page
offered new videos). `next_at` says when it runs next, for a status page.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from types import TracebackType

log = logging.getLogger(__name__)


class Ticker:
    def __init__(
        self,
        every_s: float,
        fn: Callable[[], object],
        *,
        name: str,
        on_error: Callable[[BaseException], None] | None = None,
        immediately: bool = False,
    ) -> None:
        """`immediately` runs `fn` once at start instead of after the first interval. A number `fn` returns is the
        wait before the next run (so a `fn` that returns a count for its own reasons needs a wrapper)."""
        self.every_s = every_s
        self.fn = fn
        self.name = name
        self.on_error = on_error
        self.immediately = immediately
        self.ticks = 0  # runs completed, failed ones included
        self.last_error: BaseException | None = None
        self.next_at: float | None = None  # time.time() of the next run while waiting, None while running or stopped
        self._stop = threading.Event()
        self._wake = threading.Event()  # set by poke() and stop(): ends the wait early
        self._thread: threading.Thread | None = None

    def _tick(self) -> float:
        """Run `fn` once; the wait before the next run."""
        wait = self.every_s
        try:
            out = self.fn()
            self.last_error = None
            if isinstance(out, int | float) and not isinstance(out, bool) and not math.isnan(out):
                wait = max(0.0, float(out))  # a negative wait means now; inf waits for a poke or stop
        except Exception as e:
            self.last_error = e
            if self.on_error is not None:
                self.on_error(e)
            else:
                log.exception("%s failed; trying again in %s s", self.name, self.every_s)
        finally:
            self.ticks += 1
        return wait

    def _loop(self) -> None:
        wait = self._tick() if self.immediately and not self._stop.is_set() else self.every_s
        while not self._stop.is_set():
            self.next_at = time.time() + wait if math.isfinite(wait) else None
            self._wake.wait(None if math.isinf(wait) else wait)
            self._wake.clear()  # before the run: a poke that arrives during it runs fn again straight after
            self.next_at = None
            if self._stop.is_set():
                break
            wait = self._tick()

    def poke(self) -> None:
        """Run `fn` now instead of at the end of the wait (or straight after the run in progress); a no-op when
        stopped."""
        if self.running:
            self._wake.set()

    def start(self) -> Ticker:
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()
        self._wake.clear()
        self._thread = threading.Thread(target=self._loop, name=self.name, daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float | None = 10.0) -> None:
        """Stop after the run in progress, if any; waits up to `timeout` for it."""
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def __enter__(self) -> Ticker:
        return self.start()

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.stop()
