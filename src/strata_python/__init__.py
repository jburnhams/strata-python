"""Common code for the strata Python web server apps (FastAPI + CLI): the parts every app was copying.

oslib        file locks, process liveness and termination, priority, how to start a CLI (POSIX and Windows)
settings     one JSON settings file per app with environment overrides and validation
web          reverse-proxy-safe page serving, health, settings and system routes, error handlers, static files, the uvicorn runner
cli          argparse commands every app has: doctor, set, serve
contracts    the shapes the apps' APIs share (Health, SystemInfo, ErrorBody, Task, Progress, Page, FileRef, Judgment)
client       ServiceClient: the standard-library base for calling a strata service
clients      one typed client per strata service (models)
tasks        retry rules, progress reporting, SingleFlight, StatusFile and Detached, Ticker; a durable queue with [db]
http_server  a real scripted HTTP server on loopback for tests of outbound requests
live         run a FastAPI app on a real loopback port for tests
testing      the fakes for HTTP, subprocesses and sleep, and CLI runners, shared by every app's tests
pytest plugin (automatic once installed): hermetic environment, no-network guard, `http_server`, `live_app` and the fake_* fixtures
check        `strata-check`, the one gate: lockfile, ruff, ruff format, pyright, ty, unit tests with coverage
public_check `strata-public-check`: no email addresses, private addresses, home paths or owner-listed names in what is published
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("strata-python")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0+unknown"
