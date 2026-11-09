"""`serve` run the way a service runs it: a separate process, a real port, real requests, then stopped."""

import json
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

SCRIPT = """
import sys
from strata_python import web
web.serve(web.base_app("demo", "7"), sys.argv[1:], default_port=1, banner="demo banner")
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_serve_answers_requests_until_stopped():
    port = free_port()
    proc = subprocess.Popen([sys.executable, "-c", SCRIPT, "--port", str(port)], stdout=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health") as r:
                    assert json.load(r) == {"ok": True, "version": "7"}
                break
            except OSError:
                if time.monotonic() > deadline or proc.poll() is not None:
                    pytest.fail("the server did not come up")
                time.sleep(0.1)
    finally:
        proc.terminate()
        out, _ = proc.communicate(timeout=20)
    assert out.startswith("demo banner\nhttp://127.0.0.1:")
