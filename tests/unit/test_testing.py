"""The fakes and runners, used through the plugin's fixtures the way an app's tests use them."""

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from strata_python import testing
from strata_python.testing import FakeResponse, http_error, run_cli, run_module


def test_fake_urlopen_replies_in_order_and_records_each_request(fake_urlopen):
    fake_urlopen.reply(http_error(429, {"error": "slow down"}), {"ok": 1}, FakeResponse("plain", 201, {"X-A": "b"}))
    req = urllib.request.Request(
        "https://api.example/v1", data=json.dumps({"q": "dune"}).encode(), headers={"User-Agent": "myapp"}
    )
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert (e.value.code, json.loads(e.value.read())) == (429, {"error": "slow down"})
    with urllib.request.urlopen(req) as r:
        assert (r.getcode(), json.loads(r.read())) == (200, {"ok": 1})
    r = urllib.request.urlopen("https://api.example/plain", b"not json")
    assert (r.status, r.headers, r.read(3)) == (201, {"X-A": "b"}, b"pla")

    first, _, third = fake_urlopen.calls
    assert (first.url, first.method, first.timeout, first.body) == ("https://api.example/v1", "POST", 5, {"q": "dune"})
    assert first["headers"]["user-agent"] == "myapp"  # dict-style access, as the apps' old copies had
    assert (third.method, third.body, third.headers) == (None, b"not json", {})


def test_fake_response_reads_to_eof_in_chunks():
    r = FakeResponse(b"abcdef")
    assert [r.read(4), r.read(4), r.read(4)] == [b"abcd", b"ef", b""]
    assert FakeResponse({"a": 1}).read() == b'{"a": 1}'


def test_fake_urlopen_fails_rather_than_reaching_the_network(fake_urlopen):
    fake_urlopen.reply(OSError("down"))
    with pytest.raises(OSError, match="down"):
        urllib.request.urlopen("https://api.example/")
    with pytest.raises(AssertionError, match="no reply queued"):
        urllib.request.urlopen("https://api.example/again")


def test_http_error_bodies():
    assert http_error(404).read() == b"{}"
    assert http_error(500, b"raw").read() == b"raw"
    assert http_error(400, "text").read() == b"text"


def test_fake_popen_starts_nothing_and_is_fresh_per_test(fake_popen, tmp_path):
    assert fake_popen.instances == []
    with (tmp_path / "log").open("w") as log:
        p = subprocess.Popen(["worker", 3], stdout=log, cwd="/srv")
    assert (p.cmd, p.kw, p.pid, p.poll(), p.wait()) == (
        ["worker", "3"],
        {"stdout": str(tmp_path / "log"), "cwd": "/srv"},
        4242,
        None,
        0,
    )
    with subprocess.Popen(["second"]) as q:
        q.returncode = 1
    assert (q.pid, q.wait(), fake_popen.instances) == (4243, 1, [p, q])
    p.terminate()
    assert p.returncode == -15
    p.kill()
    assert p.returncode == -9
    assert testing.FakePopen.instances == []  # the shared base class is never touched


def test_fake_run_scripts_output_and_failure(fake_run):
    fake_run.returns["ffprobe"] = b'{"duration": 3}'
    fake_run.returns["--version"] = "1.2\n"
    assert subprocess.run(["ffprobe", "-v", "error", "a.mp4"], check=True).stdout == b'{"duration": 3}'
    assert subprocess.check_output(["tool", "--version"]) == "1.2\n"
    assert subprocess.run(["other"]).stdout == b""
    fake_run.code = 1
    assert subprocess.run(["other"]).returncode == 1  # no check: a result, as subprocess.run gives
    with pytest.raises(subprocess.CalledProcessError):
        subprocess.run(["other"], check=True)
    with pytest.raises(subprocess.CalledProcessError):
        subprocess.check_output(["other"])
    assert fake_run.calls[0] == ["ffprobe", "-v", "error", "a.mp4"]
    assert len(fake_run.calls) == 6


def test_no_sleep_records_delays(no_sleep):
    time.sleep(15)
    time.sleep(30.5)
    assert no_sleep.delays == [15, 30.5]


def _main(argv):
    if argv[0] == "fail":
        print("bad input", file=sys.stderr)
        return 2
    if argv[0] == "exit":
        raise SystemExit(argv[1])
    print("hello", *argv[1:])
    return None


def test_run_cli_captures_output_and_exit_codes():
    r = run_cli(_main, "greet", "World")
    assert (r.stdout, r.code) == ("hello World\n", 0)
    assert run_cli(_main, "fail", check=False).out == "bad input\n"
    with pytest.raises(AssertionError, match="fail exited 2:\nbad input"):
        run_cli(_main, "fail")
    assert run_cli(_main, "exit", "no settings file", check=False).code == 1
    assert run_cli(_main, "exit", "no settings file", check=False).stderr == "no settings file\n"


def test_run_module_runs_a_real_process(tmp_path):
    (tmp_path / "tinyapp.py").write_text("import sys\nprint('args', sys.argv[1:])\nsys.exit(len(sys.argv) - 2)\n")
    assert run_module("tinyapp", "one", src=tmp_path).stdout == "args ['one']\n"
    with pytest.raises(AssertionError, match="python -m tinyapp exited 1"):
        run_module("tinyapp", "one", "two", src=tmp_path)
    assert run_module("json.tool", "--help").code == 0
