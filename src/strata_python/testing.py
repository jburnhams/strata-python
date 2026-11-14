"""Shared test helpers for the strata apps: scripted stand-ins for the outside world and CLI runners.

Every app carried its own `tests/utils/fakes.py` (five drifted copies of the same code) and the same `cli` fixtures; they
live here once. The pytest plugin exposes the fakes as fixtures (`fake_urlopen`, `fake_popen`, `fake_run`, `no_sleep`),
so most tests never import this module; import it for `http_error`, `FakeResponse` and the CLI runners.

The fakes are a last resort for quick unit tests of many cases at a true external edge (a web API, an external
program). Prefer the real thing: the `http_server` fixture for HTTP, the actual program for subprocesses (AGENTS.md,
Testing policy).

  FakeResponse     what `urlopen` returns: `.read()`, `.status`, `.headers`, a context manager
  FakeUrlopen      a FIFO of scripted replies (bodies, FakeResponses, exceptions); `.calls` records each request
  http_error       an `HTTPError` with a JSON (or bytes) body, to queue as a reply
  FakePopen        `subprocess.Popen` that starts nothing; `.instances` lists every one made, `.returncode` is settable
  FakeRun          `subprocess.run` / `check_output`; `.returns` maps a command-line substring to stdout, `.code` fails
  Sleeper          `time.sleep` that returns at once and records `.delays`
  run_cli          an argparse `main(argv)` in this process -> CliResult; run_module the same as a real `python -m`
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from email.message import Message
from pathlib import Path
from typing import Any, ClassVar

Body = bytes | str | Any  # bytes and str are sent as they are; anything else as JSON


def _encode(body: Body) -> bytes:
    if isinstance(body, bytes):
        return body
    if isinstance(body, str):
        return body.encode()
    return json.dumps(body).encode()


def _maybe_json(data: object) -> object:
    """A request body as the test wants to assert on it: parsed JSON when it is JSON, else as sent."""
    if not isinstance(data, (bytes, str)):
        return data
    try:
        return json.loads(data)
    except ValueError:
        return data


class FakeResponse:
    """What `urllib.request.urlopen` returns. `body` is bytes, str or anything JSON-serialisable."""

    def __init__(self, body: Body = b"", status: int = 200, headers: Mapping[str, str] | None = None) -> None:
        self.body = _encode(body)
        self._stream = io.BytesIO(
            self.body
        )  # reads consume it, so a chunked read loop reaches EOF as on a real response
        self.status = status
        self.headers: dict[str, str] = dict(headers or {})

    def read(self, n: int = -1) -> bytes:
        return self._stream.read(n)

    def getcode(self) -> int:
        return self.status

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def http_error(code: int, body: Body = None, url: str = "http://fake") -> urllib.error.HTTPError:
    """An `HTTPError` whose `.read()` gives `body` (JSON unless bytes; `{}` when None). Queue it as a reply."""
    return urllib.error.HTTPError(url, code, "error", Message(), io.BytesIO(_encode({} if body is None else body)))


@dataclass
class Call:
    """One request `FakeUrlopen` was asked to make. `call["url"]` works too, as in the apps' old copies."""

    url: str
    method: str | None
    timeout: float | None
    headers: dict[str, str]  # names lower-cased
    body: object  # parsed JSON when it is JSON

    def __getitem__(self, key: str) -> object:
        return getattr(self, key)


class FakeUrlopen:
    """A scripted `urlopen`. Queue replies in order, run the code, then assert on `.calls`:

        fake_urlopen.reply(http_error(429), {"ok": 1})   # the retry sees a 429, then a body

    A reply is a body, a `FakeResponse` or an exception (raised). A request with nothing queued fails the test rather
    than reaching the network."""

    def __init__(self) -> None:
        self.replies: list[object] = []
        self.calls: list[Call] = []

    def reply(self, *items: object) -> FakeUrlopen:
        self.replies.extend(items)
        return self

    def __call__(
        self, req: str | urllib.request.Request, data: bytes | None = None, timeout: float | None = None, **kw: object
    ) -> FakeResponse:
        if isinstance(req, str):
            call = Call(req, None, timeout, {}, _maybe_json(data))
        else:
            headers = {k.lower(): v for k, v in req.header_items()}
            call = Call(req.full_url, req.get_method(), timeout, headers, _maybe_json(req.data))
        self.calls.append(call)
        if not self.replies:
            raise AssertionError(f"unexpected HTTP request to {call.url} (no reply queued)")
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply if isinstance(reply, FakeResponse) else FakeResponse(reply)


class FakePopen:
    """Stands in for `subprocess.Popen`: records the command and keyword arguments, never starts anything.

    The `fake_popen` fixture hands each test its own subclass, so `.instances` starts empty and never leaks between
    tests. A test finishes a process by setting `fake_popen.instances[-1].returncode = 0`. File objects passed as
    stdin/stdout/stderr are recorded by name, not kept: holding them open would stop a test deleting its folder on
    Windows."""

    instances: ClassVar[list[FakePopen]] = []

    def __init__(self, cmd: Sequence[object], **kw: object) -> None:
        self.cmd = [str(c) for c in cmd]
        self.kw = {k: getattr(v, "name", v) if hasattr(v, "fileno") else v for k, v in kw.items()}
        self.pid = 4242 + len(type(self).instances)
        self.returncode: int | None = None
        type(self).instances.append(self)

    @classmethod
    def fresh(cls) -> type[FakePopen]:
        """A subclass with its own empty `.instances`."""
        return type("FakePopen", (cls,), {"instances": []})

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        return 0 if self.returncode is None else self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    def __enter__(self) -> FakePopen:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class FakeRun:
    """Stands in for `subprocess.run` and `check_output`: `.calls` holds each command as a list of strings;
    `.returns["ffprobe"] = b"..."` gives the stdout of any command whose line contains that text (default `b""`,
    returned as the test set it, bytes or str); `.code = 1` makes every command fail (raising only with `check=True`,
    as `subprocess.run` does; `check_output` always checks)."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.returns: dict[str, bytes | str] = {}
        self.code = 0

    def _out(self, cmd: Sequence[object]) -> bytes | str:
        line = " ".join(map(str, cmd))
        return next((v for k, v in self.returns.items() if k in line), b"")

    def __call__(self, cmd: Sequence[object], *a: object, **kw: object) -> subprocess.CompletedProcess[Any]:
        self.calls.append([str(c) for c in cmd])
        out = self._out(cmd)
        if self.code and kw.get("check"):
            raise subprocess.CalledProcessError(self.code, self.calls[-1], out)
        return subprocess.CompletedProcess(self.calls[-1], self.code, out, b"")

    def check_output(self, cmd: Sequence[object], *a: object, **kw: object) -> bytes | str:
        self.calls.append([str(c) for c in cmd])
        out = self._out(cmd)
        if self.code:
            raise subprocess.CalledProcessError(self.code, self.calls[-1], out)
        return out


@dataclass
class Sleeper:
    """`time.sleep` that returns at once; `.delays` records what was asked, so a test can assert on a backoff."""

    delays: list[float] = field(default_factory=lambda: [])

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


@dataclass
class CliResult:
    stdout: str
    stderr: str
    code: int

    @property
    def out(self) -> str:
        return self.stdout + self.stderr


def run_cli(main: Callable[[list[str]], int | None], *args: object, check: bool = True) -> CliResult:
    """Run an argparse-style `main(argv)` in this process, capturing its output and exit code (a `SystemExit` counts).
    Raises AssertionError with the output on a non-zero exit unless `check=False`."""
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main([str(a) for a in args]) or 0
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (1 if e.code else 0)
            if isinstance(e.code, str):
                err.write(e.code + "\n")
    result = CliResult(out.getvalue(), err.getvalue(), code)
    if check and code:
        raise AssertionError(f"{' '.join(map(str, args))} exited {code}:\n{result.out}")
    return result


def run_module(
    module: str, *args: object, src: str | Path | None = None, check: bool = True, timeout: float = 300
) -> CliResult:
    """The same through a real `python -m module` process, for entry-point smoke tests and anything that must not
    share this process's state. `src` is put on PYTHONPATH (an app's `src` folder when it is not installed)."""
    env = dict(os.environ, PYTHONWARNINGS="ignore")
    if src is not None:
        env["PYTHONPATH"] = os.pathsep.join([str(src), *filter(None, [os.environ.get("PYTHONPATH")])])
    p = subprocess.run(  # noqa: S603 - the interpreter running the tests, on the app's own module
        [sys.executable, "-m", module, *map(str, args)], capture_output=True, text=True, env=env, timeout=timeout
    )
    result = CliResult(p.stdout, p.stderr, p.returncode)
    if check and p.returncode:
        raise AssertionError(f"python -m {module} exited {p.returncode}:\n{result.out}")
    return result
