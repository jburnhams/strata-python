"""ServiceClient against real servers on loopback (live_app, http_server), not a patched urlopen."""

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse

from strata_python.client import Rejected, ServiceClient, Unavailable, env_url
from strata_python.testing import Sleeper


@pytest.fixture
def service():
    app = FastAPI()
    state = {"polls": 0, "flaky": 0, "headers": {}}

    @app.get("/api/health")
    def health():
        return {"ok": True, "version": "3"}

    @app.post("/api/echo")
    def echo(body: dict):
        return {"got": body}

    @app.get("/api/who")
    def who(request: __import__("fastapi").Request):
        state["headers"] = dict(request.headers)
        return {}

    @app.get("/api/rows/1")
    def row(wait: float = 0):
        state["polls"] += 1
        state["wait"] = wait
        return {"id": 1, "status": "done" if state["polls"] >= 3 else "running"}

    @app.get("/api/refused")
    def refused():
        return JSONResponse({"detail": "no such book", "payload": {"id": 7}}, status_code=404)

    @app.get("/api/fastapi-refused")
    def fastapi_refused():
        raise HTTPException(409, "already running")

    @app.get("/api/plain")
    def plain():
        return PlainTextResponse("teapot", status_code=418)

    @app.get("/api/flaky")
    def flaky():
        state["flaky"] += 1
        if state["flaky"] < 3:
            raise HTTPException(503, "starting")
        return {"ok": True}

    @app.post("/api/flaky")
    def flaky_post():
        state["flaky"] += 1
        raise HTTPException(503, "starting")

    @app.delete("/api/empty")
    def empty():
        return PlainTextResponse("")

    return app, state


def test_calls_parse_json_and_send_the_app(service, live_app):
    app, state = service
    with live_app(app) as base:
        c = ServiceClient(base + "/", app="strata-scan")
        assert c.post("/api/echo", {"a": 1}) == {"got": {"a": 1}}
        c.get("/api/who")
        assert state["headers"]["x-strata-app"] == "strata-scan"
        assert state["headers"]["user-agent"] == "strata-scan (strata-python)"
        assert c.delete("/api/empty") is None
        assert c.available()
        assert c.health()["version"] == "3"


def test_refusals_carry_the_reason_and_payload(service, live_app):
    app, _ = service
    with live_app(app) as base:
        c = ServiceClient(base)
        with pytest.raises(Rejected) as e:
            c.get("/api/refused")
        assert (e.value.status, e.value.detail, e.value.payload) == (404, "no such book", {"id": 7})
        with pytest.raises(Rejected, match="already running") as e:
            c.get("/api/fastapi-refused")
        assert e.value.payload is None
        with pytest.raises(Rejected, match="teapot"):
            c.get("/api/plain")


def test_reads_retry_an_outage_and_posts_do_not(service, live_app):
    app, state = service
    sleeper = Sleeper()
    with live_app(app) as base:
        c = ServiceClient(base, retries=2, backoff_s=0.5, sleep=sleeper)
        assert c.get("/api/flaky") == {"ok": True}
        assert sleeper.delays == [0.5, 1.0]
        state["flaky"] = 0
        with pytest.raises(Unavailable, match="answered 503: starting"):
            c.post("/api/flaky")
        assert state["flaky"] == 1


def test_unreachable_and_env_url(monkeypatch):
    monkeypatch.setenv("DEMO_URL", "http://127.0.0.1:9")  # the discard port: nothing listens
    c = ServiceClient(env_url("DEMO_URL", "http://127.0.0.1:1"), retries=0)
    assert c.url() == "http://127.0.0.1:9"
    with pytest.raises(Unavailable, match="not reachable"):
        c.get("/api/x")
    assert not c.available(timeout=1)
    monkeypatch.delenv("DEMO_URL")
    assert c.url() == "http://127.0.0.1:1"


def test_wait_long_polls_until_done_or_timeout(service, live_app):
    app, state = service
    with live_app(app) as base:
        c = ServiceClient(base)
        row = c.wait("/api/rows/1", lambda r: r["status"] == "done", poll_s=0.2)
        assert (row["status"], state["polls"], state["wait"]) == ("done", 3, 0.2)
        state["polls"] = -10
        with pytest.raises(TimeoutError, match="last: running"):
            c.wait("/api/rows/1", lambda r: r["status"] == "done", timeout=0)


def test_models_outage_is_a_service_outage():
    from strata_python import client
    from strata_python.clients import models

    assert issubclass(models.Unavailable, client.Unavailable)
