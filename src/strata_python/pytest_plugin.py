"""The pytest plugin (loaded automatically once strata-python is installed): hermetic tests and the real-over-mock fixtures.

Configure it in the app's `[tool.pytest.ini_options]`:

    strata_env_prefix = "MYAPP"            # MYAPP_HOME and MYAPP_DATA are pointed into a tmp dir for every test
    strata_tuning_vars = ["MYAPP_GREETING"] # removed from the environment for every test
    strata_env = ["MYAPP_SWEEP_SECONDS=0", "MYAPP_PROJECTS={env}/projects"]  # set for every test; {env} is isolated_env's dir
    strata_auto_integration = true          # tests under tests/integration get the `integration` marker (the default)

Without `strata_env_prefix` the plugin changes nothing but the no-network guard and the fixtures, so it is safe to have installed.

  isolated_env (autouse)  HOME/USERPROFILE and the app's home and data variables point into a tmp dir (outside the test's own tmp_path), so nothing reads or writes the
                          developer's real settings or data; override variables are removed. Returns that dir.
  no_network (autouse)    an outbound connection fails the test at once; loopback and unix sockets are fine.
  http_server             a real scripted HTTP server on loopback (strata_python.http_server); fails the test at teardown if a request reached no route.
  live_app                `with live_app(app) as base_url:` runs a FastAPI app on a real loopback port (strata_python.live).
  fake_urlopen            `urllib.request.urlopen` scripted: `fake_urlopen.reply(http_error(429), {...})`, then assert on `.calls`.
  fake_popen              `subprocess.Popen` starts nothing; `.instances` lists what was asked (`.cmd`, `.kw`), fresh for each test.
  fake_run                `subprocess.run` / `check_output`: `.returns["ffprobe"] = b"..."` for output, `.code = 1` to fail, `.calls`.
  no_sleep                `time.sleep` returns at once; `.delays` records the waits.
The fakes are a last resort for a true external edge (strata_python.testing says when); prefer `http_server` and real programs."""

import contextlib
import socket
import subprocess
import time
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI

from strata_python import http_server as hs
from strata_python import testing
from strata_python.live import live_server


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addini(
        "strata_env_prefix", "environment prefix of the app under test (APP -> APP_HOME, APP_DATA)", default=""
    )
    parser.addini("strata_tuning_vars", "environment variables removed for every test", type="args", default=[])
    parser.addini(
        "strata_env", "KEY=VALUE set for every test; {env} is the isolated environment's dir", type="args", default=[]
    )
    parser.addini(
        "strata_auto_integration", "mark tests under tests/integration with `integration`", type="bool", default=True
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "integration: runs processes or servers end to end (tests/integration)")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if not config.getini("strata_auto_integration"):
        return
    for item in items:
        if "integration" in item.path.parent.parts:
            item.add_marker(pytest.mark.integration)


@pytest.fixture(autouse=True)
def isolated_env(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    base = tmp_path_factory.mktemp("env")
    monkeypatch.setenv("HOME", str(base / "home"))
    monkeypatch.setenv("USERPROFILE", str(base / "home"))
    prefix = str(request.config.getini("strata_env_prefix"))
    if prefix:
        monkeypatch.setenv(f"{prefix}_HOME", str(base / "app-home"))
        monkeypatch.setenv(f"{prefix}_DATA", str(base / "data"))
    for k in request.config.getini("strata_tuning_vars"):
        monkeypatch.delenv(k, raising=False)
    for pair in request.config.getini("strata_env"):
        key, sep, value = str(pair).partition("=")
        if not sep:
            raise pytest.UsageError(f"strata_env entries are KEY=VALUE, not {pair!r}")
        monkeypatch.setenv(key, value.replace("{env}", str(base)))
    return base


def _host(address: object) -> object:
    """The host of an inet address tuple, None for anything else (a unix socket path)."""
    return cast("tuple[object, ...]", address)[0] if isinstance(address, tuple) else None


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    real = socket.socket.connect

    def guarded(self: socket.socket, address: Any, *a: Any, **kw: Any) -> Any:
        if self.family in (socket.AF_INET, socket.AF_INET6) and _host(address) not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"tests must not use the network (tried {address!r})")
        return real(self, address, *a, **kw)

    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture
def http_server() -> Iterator[hs.ScriptedServer]:
    state = hs.start()
    yield state
    hs.stop(state)
    if state.unexpected:  # not an assert: python -O would strip it
        pytest.fail(f"requests no route answered: {[(r.method, r.path) for r in state.unexpected]}", pytrace=False)


@pytest.fixture
def live_app() -> Callable[[FastAPI], contextlib.AbstractContextManager[str]]:
    return live_server


@pytest.fixture
def fake_urlopen(monkeypatch: pytest.MonkeyPatch) -> testing.FakeUrlopen:
    fake = testing.FakeUrlopen()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return fake


@pytest.fixture
def fake_popen(monkeypatch: pytest.MonkeyPatch) -> type[testing.FakePopen]:
    fake = testing.FakePopen.fresh()
    monkeypatch.setattr(subprocess, "Popen", fake)
    return fake


@pytest.fixture
def fake_run(monkeypatch: pytest.MonkeyPatch) -> testing.FakeRun:
    fake = testing.FakeRun()
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr(subprocess, "check_output", fake.check_output)
    return fake


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> testing.Sleeper:
    sleeper = testing.Sleeper()
    monkeypatch.setattr(time, "sleep", sleeper)
    return sleeper
