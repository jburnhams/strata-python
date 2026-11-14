"""Long work in its own process that outlives the server, with its state in a file anyone can read.

strata-360 starts its renders, backups and stitches with `subprocess.Popen` (16 places), keeps their pids in fifteen
`*_JOBS` dicts and finds them again after a restart through `status.json` files written by five copies of
`write_status`. `StatusFile` is the file: atomic writes, the writer's pid, and a `read` that knows whether that pid is
still alive. `Detached` starts the process (outside the server's session, so restarting the server does not kill it),
writes the file, and stops it with its children.

    RENDER = Detached(cmd=[*cli_command("strata360"), "render", project], status=f"{project}/render.status.json",
                      log=f"{project}/render.log")
    RENDER.start(project=project)       # Conflict when one is already running for that file
    RENDER.read()                       # {"state": "running", "pid": ..., "alive": True, ...}

    # in the child, which finds its file in STRATA_STATUS_FILE:
    status = StatusFile.from_env()
    status.update(progress={"done": 3, "total": 40})
    status.update(state="done", output=path)   # update merges; write replaces the file

States: whatever the writer says (`running`, `done`, `failed`, `stopped`), except that a file saying `running` whose
pid is gone reads as `died`: the process ended without saying so (killed, crashed, the machine rebooted)."""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import subprocess
import tempfile
from collections.abc import Generator, Mapping, Sequence
from typing import Any, cast

from strata_python import oslib
from strata_python.tasks.flight import Conflict

ENV = "STRATA_STATUS_FILE"
Json = dict[str, Any]


def write_json_atomic(path: str, data: object) -> None:
    """Write JSON so a reader sees the old file or the new one, never half of either."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".status.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf8") as f:
            json.dump(data, f, indent=1, default=str)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


class StatusFile:
    def __init__(self, path: str) -> None:
        self.path = path

    @classmethod
    def from_env(cls) -> StatusFile:
        """The file a `Detached` parent named in `STRATA_STATUS_FILE`."""
        path = os.environ.get(ENV)
        if not path:
            raise RuntimeError(f"{ENV} is not set: this process was not started by Detached")
        return cls(path)

    @contextlib.contextmanager
    def locked(self) -> Generator[None]:
        """Hold the file's lock (`<path>.lock`, across processes): `write` and `update` take it, so a merge never
        loses another writer's fields. Not re-entrant: inside it, write the file with `write_json_atomic`."""
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with open(self.path + ".lock", "a+") as f, oslib.file_lock(f):
            yield

    def write(self, state: str, **fields: Any) -> Json:
        """Replace the file: `state`, this process's pid (unless given), the time, and `fields`."""
        data: Json = {"state": state, "pid": os.getpid(), "at": _now(), **fields}
        with self.locked():
            write_json_atomic(self.path, data)
        return data

    def update(self, **fields: Any) -> Json:
        """Merge `fields` into the file as it is (the state and pid stay unless given)."""
        with self.locked():
            data: Json = {**(self._load() or {}), **fields, "at": _now()}
            data.setdefault("pid", os.getpid())
            write_json_atomic(self.path, data)
        return data

    def _load(self) -> Json | None:
        try:
            with open(self.path, encoding="utf8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return None
        return cast("Json", d) if isinstance(d, dict) else None

    def read(self) -> Json | None:
        """The file with `alive` added (is its pid running?), and `running` read as `died` when it is not; None when
        there is no file."""
        d = self._load()
        if d is None:
            return None
        d["alive"] = oslib.pid_alive(d.get("pid"))
        if d.get("state") == "running" and not d["alive"]:
            d["state"] = "died"
        return d

    def running(self) -> bool:
        d = self.read()
        return d is not None and d.get("state") == "running"


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


class Detached:
    """A command run as its own process, tracked through a `StatusFile`. The child gets the file's path in
    `STRATA_STATUS_FILE` and writes its own progress and end state there; its output goes to `log` (appended)."""

    def __init__(
        self,
        cmd: Sequence[str],
        status: StatusFile | str,
        log: str,
        *,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        self.cmd = [str(c) for c in cmd]
        self.status = status if isinstance(status, StatusFile) else StatusFile(status)
        self.log = log
        self.env = env
        self.cwd = cwd
        self._proc: subprocess.Popen[bytes] | None = None

    def running(self) -> bool:
        """Is the process still running? (A child of this server that has exited is reaped here, so it does not
        linger as a zombie that would still look alive.)"""
        if self._proc is not None and self._proc.poll() is not None:
            self._proc = None
        return self.status.running()

    def read(self) -> Json | None:
        self.running()
        return self.status.read()

    def start(self, **fields: Any) -> int:
        """Start the command and return its pid; `Conflict` when the status file says one is still running."""
        if self.running():
            raise Conflict(f"{' '.join(self.cmd[:3])} is already running", self.status.read())
        os.makedirs(os.path.dirname(os.path.abspath(self.log)), exist_ok=True)
        env = {**os.environ, **(self.env or {}), ENV: os.path.abspath(self.status.path)}
        detach = oslib.detached_kwargs()
        # The file is written under its lock, taken before the child exists: a child that reports (or finishes) at
        # once waits for it, so its update lands on top of this one instead of under it.
        with self.status.locked(), open(self.log, "ab") as out:
            proc: subprocess.Popen[bytes] = subprocess.Popen(  # noqa: S603 - the app's own command
                self.cmd,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=self.cwd,
                start_new_session=bool(detach.get("start_new_session", False)),
                creationflags=int(detach.get("creationflags", 0)),
            )
            data = {"state": "running", "pid": proc.pid, "at": _now(), "cmd": self.cmd, "log": self.log, **fields}
            write_json_atomic(self.status.path, data)
        self._proc = proc
        return proc.pid

    def stop(self, timeout: float = 10.0) -> bool:
        """Stop the process and everything it started; True when one was running. The file then says `stopped`."""
        d = self.status.read()
        if d is None or d.get("state") != "running":
            return False
        oslib.kill_tree(int(d["pid"]))
        if self._proc is not None:
            try:
                self._proc.wait(timeout)
            except subprocess.TimeoutExpired:
                oslib.kill_tree(int(d["pid"]), 9)
                self._proc.wait(timeout)
            self._proc = None
        self.status.update(state="stopped")
        return True
