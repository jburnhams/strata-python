"""One run at a time per key, in a background thread of this process: the "start a scan of this folder" button.

strata-stills kept a `jobs` dict, a `batches` dict and `fullres._jobs`, strata-360 fifteen `*_JOBS` dicts, each with
its own lock, its own "already running" check and its own stop flag. `SingleFlight` is that once: a second start of a
running key raises `Conflict` (which `web.add_error_handlers` answers as 409), `stop` asks the run to end at its next
check, and the last run of each key stays readable for the status endpoint.

    SCANS: SingleFlight[str] = SingleFlight("scan")

    def scan(flight: Flight[str]) -> int:
        for i, f in enumerate(files):
            if flight.stopping: return i
            report_progress(i, len(files), f"{i} of {len(files)}")
        return len(files)

    SCANS.start(folder, scan)          # in the route; Conflict -> 409 if that folder is already being scanned
    SCANS.get(folder)                  # Flight: state, progress, result or error, times

The thread is a daemon: a run in progress dies with the server. Work that must outlive a restart is a `Detached`
process or a job on the durable queue."""

from __future__ import annotations

import datetime
import logging
import threading
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from typing import Any, Generic, Literal, TypeVar

from strata_python.contracts import Progress
from strata_python.tasks.progress import ProgressReporter, reporting
from strata_python.tasks.retry import describe

K = TypeVar("K", bound=Hashable)
FlightState = Literal["running", "done", "failed", "stopped"]
log = logging.getLogger(__name__)


class Conflict(RuntimeError):
    """That run is already going. `.payload` is what is running, for the 409's body."""

    def __init__(self, message: str, payload: Any = None) -> None:
        super().__init__(message)
        self.payload = payload


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


@dataclass
class Flight(Generic[K]):
    """One run of a key. The run's function reads `.stopping`; everyone else reads the rest."""

    key: K
    name: str
    state: FlightState = "running"
    started_at: datetime.datetime = field(default_factory=_now)
    finished_at: datetime.datetime | None = None
    progress: Progress | None = None
    result: Any = None
    error: str | None = None
    stop_event: threading.Event = field(default_factory=threading.Event, repr=False)
    thread: threading.Thread | None = field(default=None, repr=False)

    @property
    def stopping(self) -> bool:
        """Has someone asked this run to stop? Check it between steps and return."""
        return self.stop_event.is_set()

    @property
    def running(self) -> bool:
        return self.state == "running"

    def join(self, timeout: float | None = None) -> bool:
        """Wait for the run to end; True when it has."""
        if self.thread is not None:
            self.thread.join(timeout)
        return not self.running

    def as_json(self) -> dict[str, Any]:
        """The run as a status endpoint answers it."""
        return {
            "key": self.key,
            "state": self.state,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "progress": None if self.progress is None else self.progress.model_dump(mode="json"),
            "error": self.error,
        }


class SingleFlight(Generic[K]):
    def __init__(self, name: str) -> None:
        self.name = name
        self._flights: dict[K, Flight[K]] = {}
        self._lock = threading.Lock()

    def start(self, key: K, fn: Callable[[Flight[K]], Any]) -> Flight[K]:
        """Run `fn(flight)` in a new thread unless `key` is already running (then `Conflict`). Progress reported
        inside it (`report_progress`) lands on `flight.progress` at once: it is memory, not worth throttling."""
        with self._lock:
            old = self._flights.get(key)
            if old is not None and old.running:
                raise Conflict(f"{self.name} {key} is already running", old.as_json())
            flight: Flight[K] = Flight(key, self.name)
            self._flights[key] = flight

        def write(p: Progress) -> None:
            flight.progress = p

        def run() -> None:
            try:
                with reporting(ProgressReporter(write, min_interval_s=0)):
                    flight.result = fn(flight)
                flight.state = "stopped" if flight.stopping else "done"
            except Exception as e:  # the run's failure is its state; the server goes on
                flight.error = describe(e)
                flight.state = "failed"
                log.exception("%s %s failed", self.name, key)
            finally:
                flight.finished_at = _now()

        flight.thread = threading.Thread(target=run, name=f"{self.name}:{key}", daemon=True)
        flight.thread.start()
        return flight

    def stop(self, key: K) -> bool:
        """Ask `key`'s run to stop; True when one was running. It ends at its next check of `.stopping`."""
        flight = self._flights.get(key)
        if flight is None or not flight.running:
            return False
        flight.stop_event.set()
        return True

    def get(self, key: K) -> Flight[K] | None:
        """The current or last run of `key`."""
        return self._flights.get(key)

    def running(self, key: K) -> bool:
        flight = self._flights.get(key)
        return flight is not None and flight.running

    def all(self) -> dict[K, Flight[K]]:
        return dict(self._flights)
