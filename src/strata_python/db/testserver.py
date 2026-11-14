"""A private, throwaway PostgreSQL server for tests and scratch databases, so they never touch the real one.

When `STRATA_DB_TEST_ADMIN_URL` is not set, the first test or scratch database a process needs starts its own
cluster: `initdb` into a new temp directory, `postgres` listening only on a Unix socket in that directory (no TCP;
on Windows, loopback on a free port), fsync off. It costs about a second, once per process. It is stopped and deleted
when the process exits; a small watchdog process does the same if the process is killed instead (SIGKILL, a crashed
test run), and the next start removes anything a watchdog could not.

The server binaries (initdb, pg_ctl, postgres) come from PATH, `PGBIN`, `pg_config --bindir`, or the usual install
folders (/usr/lib/postgresql/*/bin, C:\\Program Files\\PostgreSQL\\*\\bin). Extensions installed system-wide
(pgvector) work as usual, and you are its superuser.

The URL is exported as `STRATA_DB_TEST_ADMIN_URL`, so subprocesses (strata-db, app scripts under test) reuse it.
CI may set `STRATA_DB_TEST_ADMIN_URL` to a service container instead, and nothing is started.
"""

from __future__ import annotations

import atexit
import getpass
import glob
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from urllib.parse import quote

from strata_python.oslib import WIN, pid_alive

from .guard import TEST_ADMIN_URL

PREFIX = "strata-db-pg-"
_url: str | None = None

# Runs in its own process, so it outlives a killed test run: wait for the owner to go, then stop and delete.
WATCHDOG = """
import shutil, subprocess, sys, time
from strata_python.oslib import pid_alive
owner, pg_ctl, d = int(sys.argv[1]), sys.argv[2], sys.argv[3]
while pid_alive(owner):
    time.sleep(2)
subprocess.run([pg_ctl, "-D", d + "/data", "-m", "immediate", "stop"], capture_output=True)
shutil.rmtree(d, ignore_errors=True)
"""


def _version(path: str) -> int:
    m = re.search(r"[\\/](\d+)[\\/]bin", path)
    return int(m.group(1)) if m else 0


def _has_initdb(d: str) -> bool:
    return shutil.which("initdb", path=d) is not None


def _bindir() -> str | None:
    initdb = shutil.which("initdb")
    if initdb and shutil.which("pg_ctl"):
        return os.path.dirname(initdb)
    if (d := os.environ.get("PGBIN")) and _has_initdb(d):  # set on GitHub's Windows runners
        return d
    if pg_config := shutil.which("pg_config"):
        try:
            d = subprocess.run(  # noqa: S603  (pg_config found on PATH, fixed argument)
                [pg_config, "--bindir"], capture_output=True, text=True, check=True
            ).stdout.strip()
            if _has_initdb(d):
                return d
        except (OSError, subprocess.CalledProcessError):
            pass
    patterns = ["C:/Program Files/PostgreSQL/*/bin"] if WIN else ["/usr/lib/postgresql/*/bin"]
    found = sorted((d for p in patterns for d in glob.glob(p) if _has_initdb(d)), key=_version)
    return found[-1] if found else None


def available() -> bool:
    return _bindir() is not None


def _tmp_root() -> str:
    # A Unix socket path must fit in ~107 bytes: a long TMPDIR falls back to /tmp.
    t = tempfile.gettempdir()
    return t if WIN or len(t) < 60 else "/tmp"  # noqa: S108  (a private 0700 mkdtemp below it, not a fixed file)


def _tool(bindir: str, name: str) -> str:
    return shutil.which(name, path=bindir) or os.path.join(bindir, name)


def _stop(d: str, bindir: str | None) -> None:
    data = os.path.join(d, "data")
    if bindir and os.path.exists(os.path.join(data, "postmaster.pid")):
        subprocess.run(  # noqa: S603  (the server's own pg_ctl on its own data dir)
            [_tool(bindir, "pg_ctl"), "-D", data, "-m", "immediate", "-w", "stop"], capture_output=True
        )
    shutil.rmtree(d, ignore_errors=True)


def reap(bindir: str | None = None, root: str | None = None) -> None:
    """Stop and delete private servers whose owning process has gone (left by a killed run), in `root` (default: the
    folder servers are started in)."""
    bindir = bindir or _bindir()
    for d in glob.glob(os.path.join(root or _tmp_root(), PREFIX + "*")):
        try:
            with open(os.path.join(d, "owner"), encoding="utf-8") as f:
                owner = int(f.read().strip())
        except (OSError, ValueError):
            continue
        if not pid_alive(owner):
            _stop(d, bindir)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _initdb(bindir: str, data: str, user: str) -> subprocess.CompletedProcess[str]:
    cmd = [_tool(bindir, "initdb"), "-D", data, "-A", "trust", "-U", user, "--no-sync", "-E", "UTF8"]
    # Same collation as a current server (builtin C.UTF-8, PostgreSQL 17+); an older initdb falls back to its default.
    r = subprocess.run([*cmd, "--locale-provider=builtin", "--builtin-locale=C.UTF-8"], capture_output=True, text=True)  # noqa: S603
    if r.returncode:
        shutil.rmtree(data, ignore_errors=True)
        r = subprocess.run(cmd, capture_output=True, text=True)  # noqa: S603
    return r


def _watch(owner: int, bindir: str, d: str) -> None:
    # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: the watchdog must not die with the test run's console
    flags = 0x00000008 | 0x00000200 if WIN else 0
    subprocess.Popen(  # noqa: S603  (this interpreter, a fixed script)
        [sys.executable, "-c", WATCHDOG, str(owner), _tool(bindir, "pg_ctl"), d],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=not WIN,
        creationflags=flags,
    )


def url() -> str:
    """Admin URL of this process's private server, starting it on first use."""
    global _url
    if _url:
        return _url
    bindir = _bindir()
    if not bindir:
        raise RuntimeError(
            "no PostgreSQL server binaries (initdb, pg_ctl) found for a private test server: install the PostgreSQL "
            f"server package, or set {TEST_ADMIN_URL} to a throwaway server"
        )
    reap(bindir)
    d = tempfile.mkdtemp(prefix=PREFIX, dir=_tmp_root())
    with open(os.path.join(d, "owner"), "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    data, user = os.path.join(d, "data"), getpass.getuser()
    r = _initdb(bindir, data, user)
    if r.returncode:
        shutil.rmtree(d, ignore_errors=True)
        raise RuntimeError(f"initdb failed for the private test server:\n{r.stderr}")
    if WIN:  # no Unix sockets for libpq here: loopback only, on a port nobody else has
        port = _free_port()
        listen = f"-c listen_addresses=127.0.0.1 -p {port} -c unix_socket_directories=''"
        where = f"127.0.0.1:{port}/postgres"
    else:
        listen = f"-k {d} -c listen_addresses=''"
        where = f"localhost/postgres?host={quote(d, safe='')}"  # also parses as a WHATWG URL
    opts = f"{listen} -c fsync=off -c synchronous_commit=off -c full_page_writes=off -c max_connections=200"
    log = os.path.join(d, "log")
    r = subprocess.run(  # noqa: S603  (the server's own pg_ctl)
        [_tool(bindir, "pg_ctl"), "-D", data, "-l", log, "-o", opts, "-w", "start"], capture_output=True, text=True
    )
    if r.returncode:
        text = open(log, encoding="utf-8", errors="replace").read() if os.path.exists(log) else ""  # noqa: SIM115
        _stop(d, bindir)
        raise RuntimeError(f"could not start the private test server:\n{r.stderr}\n{text}")
    owner = os.getpid()
    _watch(owner, bindir, d)
    atexit.register(lambda: os.getpid() == owner and _stop(d, bindir))  # not in forked children
    _url = f"postgresql+psycopg://{quote(user)}@{where}"
    os.environ[TEST_ADMIN_URL] = _url
    return _url
