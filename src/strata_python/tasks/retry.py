"""When a failed task should run again, and after how long.

Taken from the strata-360 pipeline's retry rules, the most worked-out of the apps' copies: a programming error never
retries (it fails the same way every time and hides the bug), an outside failure backs off exponentially, and an
attempt that made progress before it failed resets the count, so a long job that dies near the end of each of many
nights is not given up on after five of them. The durable queue (`tasks.queue`), the HTTP client and tubes' fetcher
share it.

    try:
        work()
    except Exception as e:
        decision = next_state(job.attempts, e, progressed=job.progress_moved, retries=5)
        if decision.state == "retry": requeue(job, after_s=decision.after_s, attempts=decision.attempts)
        else: fail(job, decision.error)
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

PROGRAMMING_ERRORS: tuple[type[BaseException], ...] = (
    AttributeError,
    AssertionError,
    ImportError,
    NameError,
    NotImplementedError,
    RecursionError,
    SyntaxError,
    TypeError,
)
"""Errors in the code itself: running it again gives the same error, so they fail at once."""


class RetryLater(Exception):
    """Raised by a task that knows it should run again later (a 429 with Retry-After, a file still being written).
    It counts as an attempt; `after_s` replaces the computed backoff."""

    def __init__(self, reason: str = "retry later", *, after_s: float | None = None) -> None:
        super().__init__(reason)
        self.after_s = after_s


def backoff_s(
    attempts: int,
    *,
    base_s: float = 30.0,
    cap_s: float = 3600.0,
    jitter: float = 0.1,
    rng: Callable[[], float] = random.random,
) -> float:
    """The pause before attempt `attempts + 1`: `base_s`, doubling per attempt, at most `cap_s`, spread by
    ±`jitter` so tasks that failed together do not all come back in the same second."""
    pause = min(base_s * 2 ** max(attempts - 1, 0), cap_s)
    return max(pause * (1 + jitter * (2 * rng() - 1)), 0.0)


@dataclass(frozen=True)
class Decision:
    """What to do with a failed attempt: `retry` after `after_s` with `attempts` counted so far, or `failed`."""

    state: Literal["retry", "failed"]
    attempts: int
    after_s: float | None
    error: str


def describe(error: BaseException) -> str:
    """One line for a failure: the type and the message (`TimeoutError: read timed out`)."""
    text = str(error)
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


def next_state(
    attempts: int,
    error: BaseException,
    *,
    progressed: bool = False,
    retries: int = 5,
    retryable: Callable[[BaseException], bool] | None = None,
    base_s: float = 30.0,
    cap_s: float = 3600.0,
    rng: Callable[[], float] = random.random,
) -> Decision:
    """Decide after a failed attempt. `attempts` is how many had failed before this one; `progressed` says this one
    got further than the last (it then counts as the first failure again); `retries` is how many failures in a row
    are allowed before giving up; `retryable` can refuse an error class the task knows is permanent (a 404)."""
    message = describe(error)
    if isinstance(error, PROGRAMMING_ERRORS):
        return Decision("failed", attempts + 1, None, message)
    count = 1 if progressed else attempts + 1
    if isinstance(error, RetryLater):
        if count > retries:
            return Decision("failed", count, None, message)
        after = error.after_s if error.after_s is not None else backoff_s(count, base_s=base_s, cap_s=cap_s, rng=rng)
        return Decision("retry", count, after, message)
    if (retryable is not None and not retryable(error)) or count > retries:
        return Decision("failed", count, None, message)
    return Decision("retry", count, backoff_s(count, base_s=base_s, cap_s=cap_s, rng=rng), message)
