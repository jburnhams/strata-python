"""Queue handlers at module level, so a job process (`tasks.child`) can import them by name, as an app's would be."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Any

from strata_python.tasks import RetryLater, report_progress
from strata_python.tasks.queue import Job


def count(job: Job) -> dict[str, Any]:
    n = int(job.options.get("n", 3))
    for i in range(n):
        report_progress(i + 1, n, f"{i + 1} of {n}")
    return {"n": n, "pid": os.getpid()}


def outside(job: Job) -> None:
    report_progress(1, 2, "half")
    raise OSError(2, "No such file")


def bug(job: Job) -> None:
    raise TypeError("a bug")


def missing(job: Job) -> None:
    raise KeyError("book")


class Unbuildable(Exception):
    def __init__(self, a: str, b: str) -> None:
        super().__init__(f"{a} {b}")


def unbuildable(job: Job) -> None:
    raise Unbuildable("needs", "two")


def later(job: Job) -> None:
    raise RetryLater("file still being written", after_s=5)


def crash(job: Job) -> None:
    os._exit(3)


def spawn_and_wait(job: Job) -> None:
    """Start a tool (a grandchild of the worker), write its pid where the test reads it, then wait to be killed."""
    tool = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    with open(str(job.options["pids"]), "w") as f:
        f.write(f"{os.getpid()} {tool.pid}")
    time.sleep(120)


def chatty(job: Job) -> int:
    """Prints, and runs a tool that prints: neither may end up in the answers on the process's stdout."""
    print("a line from the handler")
    subprocess.run([sys.executable, "-c", "print('a line from a tool')"], check=True, timeout=20)
    return os.getpid()


def pid(job: Job) -> int:
    return os.getpid()
