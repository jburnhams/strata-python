import json
import urllib.error
import urllib.request

import pytest


def get(url, **headers):
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=5)


def test_real_requests_reach_scripted_routes_and_are_recorded(http_server):
    http_server.route("GET", "/items/1", {"id": 1})
    http_server.route("POST", "/items", {"made": True}, status=201)
    with get(http_server.url + "/items/1?full=1", Accept="application/json") as r:
        assert (r.status, json.load(r)) == (200, {"id": 1})
    post = urllib.request.Request(http_server.url + "/items", data=b'{"a": 1}', method="POST")
    with urllib.request.urlopen(post, timeout=5) as r:
        assert r.status == 201
    first, second = http_server.requests
    assert (first.method, first.path, first.query, first.headers["accept"]) == (
        "GET",
        "/items/1",
        {"full": ["1"]},
        "application/json",
    )
    assert second.json() == {"a": 1}


def test_once_routes_script_a_sequence(http_server):
    http_server.route("GET", "/flaky", "busy", status=503, once=True)
    http_server.route("GET", "/flaky", "fine")
    with pytest.raises(urllib.error.HTTPError) as err:
        get(http_server.url + "/flaky")
    assert err.value.code == 503
    with get(http_server.url + "/flaky") as r:
        assert r.read() == b"fine"


def test_a_request_no_route_answers_gets_a_404_and_fails_the_test(http_server):
    with pytest.raises(urllib.error.HTTPError) as err:
        get(http_server.url + "/nope")
    assert err.value.code == 404
    assert [(r.method, r.path) for r in http_server.unexpected] == [("GET", "/nope")]
    http_server.unexpected.clear()  # this test checks the recording; a real one would fail at teardown


def test_text_and_binary_bodies(http_server):
    http_server.route("GET", "/text", "plain words").route("GET", "/bin", b"\x00\x01")
    with get(http_server.url + "/text") as r:
        assert r.headers["Content-Type"].startswith("text/plain")
        assert r.read() == b"plain words"
    with get(http_server.url + "/bin") as r:
        assert r.headers["Content-Type"] == "application/octet-stream"
        assert r.read() == b"\x00\x01"
