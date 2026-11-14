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
