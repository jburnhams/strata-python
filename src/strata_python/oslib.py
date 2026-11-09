# pyright: basic, reportAttributeAccessIssue=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportDeprecated=false
# (relaxed for this file only: the Windows branches use msvcrt and ctypes.windll, which the Linux type stubs lack. Every other file is strict.)
"""OS differences in one place: file locks, process liveness and termination, priority, and how to start the CLI again.

POSIX (macOS, Linux) uses fcntl, os.kill and os.nice; Windows uses msvcrt, the Win32 API through ctypes and taskkill. Other modules should call these
instead of importing fcntl or calling os.kill(pid, 0) (on Windows that would terminate the process)."""

import contextlib
import os
import subprocess
import sys
import time
from collections.abc import Generator
from typing import IO, Any, cast

WIN = sys.platform == "win32"


@contextlib.contextmanager
def file_lock(f: IO[Any]) -> Generator[None]:
    """Exclusive lock on an open file, held for the duration of the block (other processes block until it is released)."""
    if WIN:
        import msvcrt

        f.seek(0)
        while True:
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                time.sleep(0.02)
        try:
            yield
        finally:
            f.seek(0)
            with contextlib.suppress(OSError):
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def pid_alive(pid: object) -> bool:
    """Is that process still running?"""
    try:
        pid = int(cast("Any", pid))
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if WIN:
        import ctypes

        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            ok = k.GetExitCodeProcess(h, ctypes.byref(code))
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def kill_tree(pid: int | str, sig: int = 15) -> None:
    """Terminate a worker and every process it started; errors are ignored."""
    if WIN:
        with contextlib.suppress(OSError):
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(int(pid))], capture_output=True)  # noqa: S603, S607  (fixed command, pid is an int)
        return
    try:
        kids = subprocess.run(["pgrep", "-P", str(pid)], stdout=subprocess.PIPE, text=True).stdout.split()  # noqa: S603, S607  (fixed command, pid is an int)
    except OSError:
        kids = []
    for k in kids:
        kill_tree(int(k), sig)
    with contextlib.suppress(OSError):
        os.kill(int(pid), sig)


def lower_priority(nice: int = 19) -> None:
    """Run this process at lower CPU priority (nice value on POSIX, below-normal/idle class on Windows). Children inherit it."""
    if WIN:
        import ctypes

        with contextlib.suppress(Exception):
            ctypes.windll.kernel32.SetPriorityClass(
                ctypes.windll.kernel32.GetCurrentProcess(), 0x40 if nice >= 15 else 0x4000
            )  # IDLE / BELOW_NORMAL
        return
    with contextlib.suppress(OSError, AttributeError):
        os.nice(nice)


def venv_python(venv: str) -> str:
    """The interpreter of a virtual environment: bin/python on POSIX, Scripts\\python.exe on Windows."""
    return os.path.join(venv, "Scripts", "python.exe") if WIN else os.path.join(venv, "bin", "python")


def cli_command(module: str) -> list[str]:
    """Command prefix that runs `module` (`python -m <module>`) with the interpreter running this process: works on POSIX and Windows, with or without a venv, and does not touch the environment."""
    return [sys.executable, "-m", module]
