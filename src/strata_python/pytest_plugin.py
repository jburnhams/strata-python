"""The pytest plugin (loaded automatically once strata-python is installed): hermetic tests and the real-over-mock fixtures.

Configure it in the app's `[tool.pytest.ini_options]`:

    strata_env_prefix = "MYAPP"            # MYAPP_HOME and MYAPP_DATA are pointed into a tmp dir for every test
    strata_tuning_vars = ["MYAPP_GREETING"] # removed from the environment for every test

Without `strata_env_prefix` the plugin changes nothing but the no-network guard and the fixtures, so it is safe to have installed.

  isolated_env (autouse)  HOME/USERPROFILE and the app's home and data variables point into a tmp dir (outside the test's own tmp_path), so nothing reads or writes the
                          developer's real settings or data; override variables are removed. Returns that dir.
  no_network (autouse)    an outbound connection fails the test at once; loopback and unix sockets are fine.
  http_server             a real scripted HTTP server on loopback (strata_python.http_server); fails the test at teardown if a request reached no route.
  live_app                `with live_app(app) as base_url:` runs a FastAPI app on a real loopback port (strata_python.live)."""

import contextlib
import socket
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI

from strata_python import http_server as hs
from strata_python.live import live_server


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addini(
        "strata_env_prefix", "environment prefix of the app under test (APP -> APP_HOME, APP_DATA)", default=""
    )
    parser.addini("strata_tuning_vars", "environment variables removed for every test", type="args", default=[])


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
