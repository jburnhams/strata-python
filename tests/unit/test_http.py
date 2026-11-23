"""The polite client against a real scripted server on loopback (http_server), with the clock and sleep recorded."""

import threading
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest

from strata_python.files import DiskCache
from strata_python.http import Http, HttpError, Reply, retry_after
from strata_python.testing import Sleeper

UA = "demo-app/1.0 (tests)"


class Clock:
    """A clock that sleep moves, so throttling is checked without waiting."""

    def __init__(self) -> None:
        self.t = 100.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(round(s, 3))
        self.t += s


def client(clock: Clock | None = None, **kw) -> Http:
    c = clock or Clock()
    return Http(UA, clock=c, sleep=c.sleep, **kw)


def test_get_sends_the_user_agent_and_params(http_server):
    http_server.route("GET", "/search.json", {"docs": [1]})
    r = client(min_interval=0).get(http_server.url + "/search.json", params={"q": "a b", "n": 2})
    assert r.ok
    assert r.json() == {"docs": [1]}
    assert r.headers["content-type"] == "application/json"
    req = http_server.requests[0]
    assert req.headers["user-agent"] == UA
    assert req.query == {"q": ["a b"], "n": ["2"]}


def test_user_agent_is_required():
    with pytest.raises(ValueError, match="user_agent"):
        Http("  ")


def test_post_json_and_text(http_server):
    http_server.route("POST", "/q", "ok ✓", headers={"Content-Type": "text/plain; charset=utf-8"})
    r = client(min_interval=0).post(http_server.url + "/q", json_body={"a": 1}, headers={"X-Extra": "1"})
    assert r.text() == "ok ✓"
    req = http_server.requests[0]
    assert req.json() == {"a": 1}
    assert req.headers["content-type"] == "application/json"
    assert req.headers["x-extra"] == "1"


def test_requests_to_one_host_are_spaced(http_server):
    http_server.route("GET", "/a", {})
    clock = Clock()
    h = client(clock, min_interval=2.0)
    for _ in range(3):
        h.get(http_server.url + "/a")
    assert clock.sleeps == [2.0, 2.0]


def test_threads_take_slots_instead_of_firing_together(http_server):
    """Real time: four threads with a 0.1 s gap start at least 0.3 s apart in total."""
    http_server.route("GET", "/a", {})
    h = Http(UA, min_interval=0.1)
    starts: list[float] = []
    lock = threading.Lock()

    def one():
        h.get(http_server.url + "/a")
        with lock:
            starts.append(time.monotonic())

    threads = [threading.Thread(target=one) for _ in range(4)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert max(starts) - t0 >= 0.29


def test_429_waits_for_retry_after_then_succeeds(http_server):
    http_server.route("GET", "/x", status=429, headers={"Retry-After": "7"}, once=True)
    http_server.route("GET", "/x", {"ok": 1})
    clock = Clock()
    r = client(clock, min_interval=0).get(http_server.url + "/x")
    assert r.ok
    assert 7.0 in clock.sleeps
    assert len(http_server.requests) == 2


def test_a_host_still_limited_after_the_retries_cools_down(http_server):
    http_server.route("GET", "/x", status=429)
    clock = Clock()
    h = client(clock, min_interval=0, attempts=3, cool_down=600)
    r = h.get(http_server.url + "/x")
    assert r.status == 429
    assert r.limited
    assert len(http_server.requests) == 3
    assert clock.sleeps == [1.0, 2.0]  # no Retry-After: 1, 2, 4 s
    assert h.cooling(http_server.url)
    again = h.get(http_server.url + "/x")
    assert again.limited
    assert "not asking again" in again.error
    assert len(http_server.requests) == 3  # not asked during the cool-down
    clock.t += 601
    assert not h.cooling(http_server.url)
    h.get(http_server.url + "/x")
    assert len(http_server.requests) == 6


def test_429_is_retried_for_a_post_but_a_500_is_not(http_server):
    http_server.route("POST", "/w", status=429, once=True)
    http_server.route("POST", "/w", {"done": True}, once=True)
    http_server.route("POST", "/w", status=500)
    h = client(min_interval=0)
    assert h.post(http_server.url + "/w", json_body={}).ok
    r = h.post(http_server.url + "/w", json_body={})
    assert r.status == 500
    assert len(http_server.requests) == 3  # the 500 was not replayed: the write may have happened


def test_a_post_marked_idempotent_is_retried(http_server):
    http_server.route("POST", "/interpreter", status=504, once=True)
    http_server.route("POST", "/interpreter", {"elements": []})
    r = client(min_interval=0).post(http_server.url + "/interpreter", data=b"[out:json];", idempotent=True)
    assert r.ok
    assert http_server.requests[1].body == b"[out:json];"


def test_5xx_on_a_get_is_retried_then_returned(http_server):
    http_server.route("GET", "/down", "busy", status=502)
    clock = Clock()
    r = client(clock, min_interval=0, attempts=3).get(http_server.url + "/down")
    assert r.status == 502
    assert not r.ok
    assert r.body == b"busy"
    assert clock.sleeps == [1.0, 2.0]


def test_503_with_retry_after_waits_as_told(http_server):
    http_server.route("GET", "/m", status=503, headers={"Retry-After": "3"}, once=True)
    http_server.route("GET", "/m", {})
    clock = Clock()
    assert client(clock, min_interval=0).get(http_server.url + "/m").ok
    assert clock.sleeps == [3.0]


def test_404_and_4xx_are_returned_at_once(http_server):
    http_server.route("GET", "/gone", {"detail": "no"}, status=404)
    r = client(min_interval=0).get(http_server.url + "/gone")
    assert r.status == 404
    assert r.json() == {"detail": "no"}
    assert len(http_server.requests) == 1


def test_unreachable_host_is_a_reply_with_status_0(http_server):
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens there now
    clock = Clock()
    r = client(clock, min_interval=0, attempts=2).get(f"http://127.0.0.1:{port}/x")
    assert r.status == 0
    assert r.error
    assert not r.ok
    assert clock.sleeps == [1.0]
    with pytest.raises(HttpError, match="could not be reached"):
        r.raise_for_status()


def test_only_http_and_https(http_server):
    h = client(min_interval=0)
    assert "only http(s)" in h.get("file:///etc/passwd").error
    assert "only https" in client(min_interval=0, https_only=True).get(http_server.url + "/x").error
    assert http_server.requests == []


def test_a_body_over_max_bytes_is_cut_and_says_so(http_server):
    http_server.route("GET", "/big", b"x" * 100)
    r = client(min_interval=0, max_bytes=10).get(http_server.url + "/big")
    assert r.body == b"x" * 10
    assert "larger than 10" in r.error
    assert not r.ok


def test_raise_for_status_and_get_json(http_server):
    http_server.route("GET", "/ok", {"a": 1})
    http_server.route("GET", "/missing", status=404)
    http_server.route("GET", "/bad", "nope", status=400)
    http_server.route("GET", "/html", "<html>", headers={"Content-Type": "text/html"})
    h = client(min_interval=0)
    assert h.get_json(http_server.url + "/ok") == {"a": 1}
    assert h.get_json(http_server.url + "/missing", missing=[]) == []
    with pytest.raises(HttpError, match="answered 400: nope") as e:
        h.get_json(http_server.url + "/bad")
    assert e.value.status == 400
    with pytest.raises(HttpError, match="not JSON"):
        h.get_json(http_server.url + "/html")


def test_cache_answers_young_copies_and_keeps_404s(http_server, tmp_path):
    http_server.route("GET", "/book", {"t": "A"}, once=True)
    http_server.route("GET", "/book", {"t": "B"})
    http_server.route("GET", "/none", status=404)
    http_server.route("GET", "/err", status=500)
    clock = Clock()
    cache = DiskCache(tmp_path, clock=clock)
    h = client(clock, min_interval=0, attempts=1, cache=cache)
    first = h.get(http_server.url + "/book", cache_s=60)
    second = h.get(http_server.url + "/book", cache_s=60)
    assert (first.json(), first.cached) == ({"t": "A"}, False)
    assert (second.json(), second.cached) == ({"t": "A"}, True)
    assert second.headers["content-type"] == "application/json"
    clock.t += 61
    assert h.get(http_server.url + "/book", cache_s=60).json() == {"t": "B"}
    assert h.get(http_server.url + "/book").json() == {"t": "B"}  # no cache_s: always asks
    h.get(http_server.url + "/none", cache_s=60)
    assert h.get(http_server.url + "/none", cache_s=60).cached
    h.get(http_server.url + "/err", cache_s=60)
    assert not h.get(http_server.url + "/err", cache_s=60).cached
    paths = [r.path for r in http_server.requests]
    assert paths.count("/book") == 3
    assert paths.count("/none") == 1
    assert paths.count("/err") == 2


def test_cache_key_and_damaged_entry(http_server, tmp_path):
    http_server.route("POST", "/q", {"n": 1})
    cache = DiskCache(tmp_path)
    h = client(min_interval=0, cache=cache)
    h.post(http_server.url + "/q", data=b"query", idempotent=True, cache_s=60, cache_key="q:1")
    assert h.post(http_server.url + "/q", data=b"other", cache_s=60, cache_key="q:1").cached
    cache.put("q:1", b"garbage")
    assert not h.post(http_server.url + "/q", data=b"query", cache_s=60, cache_key="q:1").cached


def test_observe_sees_every_attempt(http_server):
    http_server.route("GET", "/x", status=502, once=True)
    http_server.route("GET", "/x", {})
    seen: list[tuple[int, int]] = []
    client(min_interval=0, observe=lambda r, n: seen.append((r.status, n))).get(http_server.url + "/x")
    assert seen == [(502, 1), (200, 2)]


def test_throttle_file_spaces_requests_across_clients(http_server, tmp_path):
    """Two clients (as two processes would) sharing a lock file: the second waits for the first's gap."""
    http_server.route("GET", "/a", {})
    lock = tmp_path / "throttle" / ".last"
    waits: list[float] = []
    a = Http(UA, min_interval=0.5, throttle_file=lock, sleep=waits.append)
    b = Http(UA, min_interval=0.5, throttle_file=lock, sleep=waits.append)
    a.get(http_server.url + "/a")
    b.get(http_server.url + "/a")
    assert len(waits) == 1
    assert 0.3 < waits[0] <= 0.5
    lock.write_text("not a number")
    a.get(http_server.url + "/a")  # a damaged file is read as "never"


def test_retry_after_forms():
    assert retry_after({"Retry-After": "5"}, 1, max_wait=60) == 5
    assert retry_after({"retry-after": "500"}, 1, max_wait=60) == 60
    assert retry_after({"Retry-After": "0"}, 1, max_wait=60) == 0.5
    assert retry_after({}, 3, max_wait=60) == 4
    later = datetime.now(UTC) + timedelta(seconds=30)
    assert 25 < retry_after({"Retry-After": format_datetime(later, usegmt=True)}, 1, max_wait=60) <= 30
    assert retry_after({"Retry-After": "soon"}, 2, max_wait=60) == 2


def test_reply_basics():
    r = Reply("https://example.org/a", 200, b"\xe9", {"content-type": "text/plain; charset=latin-1"})
    assert r.text() == "é"
    assert r.raise_for_status() is r
    assert not Reply("u", 204, error="x").ok


def test_sleeper_fixture_still_works_with_the_default_sleep(http_server, no_sleep):
    """The default sleep is time.sleep looked up at call time, so the plugin's no_sleep makes retries instant."""
    http_server.route("GET", "/x", status=503, once=True)
    http_server.route("GET", "/x", {})
    h = Http(UA, min_interval=0)
    assert h.get(http_server.url + "/x").ok
    assert isinstance(no_sleep, Sleeper)


def test_an_error_whose_body_cannot_be_read_still_reports_the_status():
    """The one fake opener here: a reset while reading a 500's body is hard to make on a real server."""
    import io
    import urllib.error
    from email.message import Message

    class Broken(io.BytesIO):
        def read(self, *a):
            raise ConnectionResetError("reset while reading")

    def opener(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 500, "Server Error", Message(), Broken())

    r = Http(UA, min_interval=0, attempts=1, opener=opener).get("https://example.org/x")
    assert r.status == 500
    assert "Server Error" in r.error
