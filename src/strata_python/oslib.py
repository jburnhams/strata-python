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


# ioprio_set's syscall number per Linux architecture (Python has no wrapper). Elsewhere `ionice` does the same.
_IOPRIO_SET = {"x86_64": 251, "aarch64": 30, "riscv64": 30, "i686": 289, "i386": 289, "armv7l": 314, "ppc64le": 273}
_IOPRIO_IDLE = 3 << 13  # IOPRIO_CLASS_IDLE in the top bits of the priority value; idle has no level
MAC = sys.platform == "darwin"


def lower_priority(nice: int = 19, io: bool = False) -> None:
    """Run this process at lower CPU priority (nice value on POSIX, below-normal/idle class on Windows). Children
    inherit it. Best effort: a refusal is skipped.

    `io=True` also lowers its disk priority, for batch jobs that read many large files (hashing an archive, exiftool
    over a folder) and should not slow interactive use of the same disk: the idle I/O class on Linux (children
    inherit it), background mode on Windows (CPU and I/O; this process only, Windows does not pass it on), I/O
    throttling on macOS (this process)."""
    if WIN:
        import ctypes

        with contextlib.suppress(Exception):
            k32 = ctypes.windll.kernel32
            k32.SetPriorityClass(k32.GetCurrentProcess(), 0x40 if nice >= 15 else 0x4000)  # IDLE / BELOW_NORMAL
            if io:
                k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00100000)  # PROCESS_MODE_BACKGROUND_BEGIN
        return
    with contextlib.suppress(OSError, AttributeError):
        os.nice(nice)
    if io:
        _lower_io_priority()


def _lower_io_priority(pid: int = 0) -> None:
    """Idle I/O for `pid` (0: this process). Other pids are for the tests, which must not lower their own."""
    import ctypes
    import ctypes.util
    import platform
    import shutil

    if MAC:  # pragma: no cover - the Linux leg runs the tests that count; macOS is best effort
        with contextlib.suppress(Exception):
            libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            libc.setiopolicy_np(0, 0, 3)  # IOPOL_TYPE_DISK, IOPOL_SCOPE_PROCESS, IOPOL_THROTTLE
        return
    number = _IOPRIO_SET.get(platform.machine()) if sys.platform.startswith("linux") else None
    if number is not None:
        with contextlib.suppress(Exception):
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.syscall(number, 1, pid, _IOPRIO_IDLE) == 0:  # IOPRIO_WHO_PROCESS
                return
    ionice = shutil.which("ionice")
    if ionice:  # another architecture or a refused syscall: util-linux's ionice does the same for our pid
        with contextlib.suppress(OSError):
            subprocess.run([ionice, "-c3", "-p", str(pid or os.getpid())], capture_output=True, check=False)  # noqa: S603 (ionice from PATH, our pid)


def venv_python(venv: str) -> str:
    """The interpreter of a virtual environment: bin/python on POSIX, Scripts\\python.exe on Windows."""
    return os.path.join(venv, "Scripts", "python.exe") if WIN else os.path.join(venv, "bin", "python")


def cli_command(module: str) -> list[str]:
    """Command prefix that runs `module` (`python -m <module>`) with the interpreter running this process: works on POSIX and Windows, with or without a venv, and does not touch the environment."""
    return [sys.executable, "-m", module]


def detached_kwargs() -> dict[str, Any]:
    """`subprocess.Popen` keyword arguments that start a process outside this one's session (POSIX) or console and
    process group (Windows), so it keeps running when the server that started it restarts, and a Ctrl+C or a stopped
    service unit aimed at the server does not reach it. `kill_tree` still stops it and its children."""
    if WIN:
        return {"creationflags": 0x00000200 | 0x00000008}  # CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS
    return {"start_new_session": True}


def load_average() -> tuple[float, float, float] | None:
    """The 1, 5 and 15 minute load averages, or None where the OS has none (Windows)."""
    try:
        return os.getloadavg()
    except (AttributeError, OSError):
        return None
