"""Run a function every so often on a daemon thread: the backup sweep, the prefetch loop, the GPU poller.

Five apps wrote this loop by hand (`while not stop.wait(every): fn()`), and most of them let one exception end the
loop for the rest of the server's life. A `Ticker` catches it, hands it to `on_error` (by default the log) and keeps
going, and stops promptly: `stop()` wakes the wait instead of sleeping out the interval.

    with Ticker(300, sweep, name="sweep"):     # or .start() at startup and .stop() at shutdown
        serve(app)
"""

from __future__ import annotations

import logging
import threading
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
        """`immediately` runs `fn` once at start instead of after the first interval."""
        self.every_s = every_s
        self.fn = fn
        self.name = name
        self.on_error = on_error
        self.immediately = immediately
        self.ticks = 0  # runs completed, failed ones included
        self.last_error: BaseException | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _tick(self) -> None:
        try:
            self.fn()
            self.last_error = None
        except Exception as e:
            self.last_error = e
            if self.on_error is not None:
                self.on_error(e)
            else:
                log.exception("%s failed; trying again in %s s", self.name, self.every_s)
        finally:
            self.ticks += 1

    def _loop(self) -> None:
        if self.immediately and not self._stop.is_set():
            self._tick()
        while not self._stop.wait(self.every_s):
            self._tick()

    def start(self) -> Ticker:
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=self.name, daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float | None = 10.0) -> None:
        """Stop after the run in progress, if any; waits up to `timeout` for it."""
        self._stop.set()
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
