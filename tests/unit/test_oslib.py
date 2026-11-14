import shutil
import subprocess
import sys
import time

import pytest

from strata_python import oslib


def test_cli_command_runs_a_module_with_this_interpreter():
    cmd = oslib.cli_command("json.tool")
    assert cmd[:2] == [sys.executable, "-m"]
    out = subprocess.run(cmd, input='{"a": 1}', capture_output=True, text=True, check=True).stdout
    assert '"a": 1' in out


def test_cli_command_leaves_the_environment_alone(monkeypatch):
    monkeypatch.delenv("PYTHONPATH", raising=False)
    oslib.cli_command("json.tool")
    assert "PYTHONPATH" not in __import__("os").environ


def test_pid_alive_for_this_process_and_garbage():
    import os

    assert oslib.pid_alive(os.getpid())
    assert not oslib.pid_alive(0)
    assert not oslib.pid_alive(-3)
    assert not oslib.pid_alive("not a pid")
    assert not oslib.pid_alive(None)


def test_kill_tree_stops_a_real_process_and_its_child():
    # a parent that starts a sleeping child, so the whole tree must go
    parent = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); time.sleep(60)",
        ]
    )
    try:
        time.sleep(0.5)
        assert oslib.pid_alive(parent.pid)
        oslib.kill_tree(parent.pid)
        parent.wait(timeout=10)
        assert not oslib.pid_alive(parent.pid)
    finally:
        if parent.poll() is None:
            parent.kill()


def test_file_lock_excludes_another_process(tmp_path):
    lock = tmp_path / "lock"
    probe = (
        "import sys,time; from strata_python import oslib\n"
        "f=open(sys.argv[1],'a+')\n"
        "t=time.time()\n"
        "with oslib.file_lock(f): print(round(time.time()-t,2))\n"
    )
    with open(lock, "a+") as f, oslib.file_lock(f):
        child = subprocess.Popen([sys.executable, "-c", probe, str(lock)], stdout=subprocess.PIPE, text=True)
        time.sleep(0.6)
        assert child.poll() is None  # still waiting for us
    out, _ = child.communicate(timeout=10)
    assert float(out) >= 0.4


def test_venv_python_path():
    assert oslib.venv_python("v").replace("\\", "/") in ("v/bin/python", "v/Scripts/python.exe")


def test_lower_priority_does_not_raise():
    # run in a child so this test process keeps its priority
    code = "from strata_python import oslib; oslib.lower_priority(); print('ok')"
    assert (
        subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip() == "ok"
    )


LINUX_IONICE = sys.platform.startswith("linux") and shutil.which("ionice") is not None


@pytest.mark.skipif(not LINUX_IONICE, reason="Linux, with ionice to read it back")
@pytest.mark.parametrize("syscall", [True, False])
def test_lower_io_priority_puts_a_process_in_the_idle_io_class(syscall, monkeypatch):
    """Issue #12: once through the syscall and once through the ionice fallback (an architecture the table does not
    know). Aimed at a child process, so this test process keeps its priority; read back with ionice."""
    reader = shutil.which("ionice")
    assert reader is not None
    if syscall:
        monkeypatch.setattr(shutil, "which", lambda *a, **k: None)  # so a silent fallback cannot pass for the syscall
    else:
        monkeypatch.setattr(oslib, "_IOPRIO_SET", {})
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        oslib._lower_io_priority(child.pid)
        out = subprocess.run([reader, "-p", str(child.pid)], capture_output=True, text=True, check=True).stdout
        assert out.strip() == "idle"
    finally:
        child.kill()
        child.wait()


@pytest.mark.skipif(not LINUX_IONICE, reason="Linux, with ionice to read it back")
def test_lower_priority_io_in_a_child_reaches_its_children():
    """The public call, in a child so this test process keeps its priority; the child's own child inherits it."""
    code = (
        "import subprocess\n"
        "from strata_python import oslib\n"
        "oslib.lower_priority(io=True)\n"
        "print(subprocess.run(['ionice'], capture_output=True, text=True, check=True).stdout.strip())\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
    assert out == "idle"


def test_lower_priority_in_this_process_with_no_change():
    oslib.lower_priority(0)  # nice(0) is a no-op, so this process keeps its priority


def test_detached_kwargs_start_a_new_session():
    import os

    kw = oslib.detached_kwargs()
    if oslib.WIN:  # no sessions to read back: the flags must still start a process that runs and answers
        p = subprocess.Popen([sys.executable, "-c", "print('ok')"], stdout=subprocess.PIPE, text=True, **kw)
        out, _ = p.communicate(timeout=20)
        assert out.strip() == "ok"
        assert p.returncode == 0
        return
    p = subprocess.Popen(
        [sys.executable, "-c", "import os; print(os.getsid(0))"], stdout=subprocess.PIPE, text=True, **kw
    )
    out, _ = p.communicate(timeout=20)
    assert int(out) == p.pid != os.getsid(0)  # its own session, so the server's signals do not reach it


def test_load_average():
    load = oslib.load_average()
    if oslib.WIN:  # Windows has no load average; the status page shows none rather than failing
        assert load is None
        return
    assert load is not None
    assert len(load) == 3


LIFELINE_CHILD = """
import os, subprocess, sys, time
from strata_python import oslib
assert oslib.watch_supervisor()
assert oslib.SUPERVISED_ENV not in os.environ  # its own children are not supervised by it
tool = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
open(sys.argv[1], "w").write(f"{os.getpid()} {tool.pid}")
time.sleep(120)
"""


def test_a_supervised_child_ends_its_group_when_the_parent_end_of_the_lifeline_closes(tmp_path):
    """A server that crashes closes the pipe the same way: the job and the tool it started must not outlive it."""
    pids = tmp_path / "pids"
    p = oslib.start_supervised([sys.executable, "-c", LIFELINE_CHILD, str(pids)])
    deadline = time.monotonic() + 30
    while not (pids.exists() and len(pids.read_text().split()) == 2):
        assert time.monotonic() < deadline
        assert p.poll() is None
        time.sleep(0.02)
    child, tool = (int(x) for x in pids.read_text().split())
    assert oslib.pid_alive(tool)
    assert p.stdin is not None
    p.stdin.close()
    assert p.wait(30) != 0
    while oslib.pid_alive(tool) or oslib.pid_alive(child):
        assert time.monotonic() < deadline, (child, tool)
        time.sleep(0.02)


def test_watch_supervisor_needs_a_supervisor(monkeypatch):
    monkeypatch.delenv(oslib.SUPERVISED_ENV, raising=False)
    assert not oslib.watch_supervisor()
