"""Running work outside a request: the small pieces every app wrote for itself, then (with the db extra) a durable
queue built on them.

  retry      next_state, backoff_s, RetryLater, PROGRAMMING_ERRORS: when a failure runs again
  progress   report_progress anywhere inside a task; ProgressReporter writes it at most once a second
  flight     SingleFlight: one in-process run per key, Conflict on a second start (409), a cooperative stop
  status     StatusFile and Detached: a process that outlives the server, with its state in an atomic JSON file
  ticker     Ticker: a function every N seconds on a daemon thread, surviving its own errors
  queue      Queue and Worker (needs strata-python[db]): durable jobs in the app's database; import it directly
"""

from strata_python.contracts import Progress
from strata_python.files import write_json_atomic
from strata_python.tasks.flight import Conflict, Flight, SingleFlight
from strata_python.tasks.progress import ProgressReporter, report_progress, reporting
from strata_python.tasks.retry import PROGRAMMING_ERRORS, Decision, RetryLater, backoff_s, describe, next_state
from strata_python.tasks.status import Detached, StatusFile
from strata_python.tasks.ticker import Ticker

__all__ = [
    "PROGRAMMING_ERRORS",
    "Conflict",
    "Decision",
    "Detached",
    "Flight",
    "Progress",
    "ProgressReporter",
    "RetryLater",
    "SingleFlight",
    "StatusFile",
    "Ticker",
    "backoff_s",
    "describe",
    "next_state",
    "report_progress",
    "reporting",
    "write_json_atomic",
]
