import subprocess
import sys
import time

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


def test_lower_priority_in_this_process_with_no_change():
    oslib.lower_priority(0)  # nice(0) is a no-op, so this process keeps its priority
