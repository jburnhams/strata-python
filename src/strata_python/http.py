"""A polite client for other people's web APIs: a gap between requests to each host, Retry-After honoured, a host that
keeps saying 429 left alone for a while, retries that never replay a write, and an optional disk cache.

Six apps call public services (Open Library, Audible, Overpass, Nominatim, Mapillary, tile servers, YouTube, Plex,
planning portals) and about ten copies of this loop grew there: listory's `covers/net.py` (the most complete: per-host
throttle, Retry-After as seconds or a date, a cool-down), its providers' `Http` (404 as None, resets retried for reads
only), strata-360's `places._get` (a throttle shared across processes through a lock file, a disk cache) and
`streetview._open` (errors returned, not raised), and smaller ones in takeout, tubes and planning. This is that loop
once, with each copy's best rule:

    OL = Http("strata-listory/0.3 (audiobook catalogue)", min_interval=0.4, cache=DiskCache(cache_dir))
    reply = OL.get("https://openlibrary.org/search.json", params={"q": title}, cache_s=7 * 86400)
    if reply.ok: data = reply.json()
    elif reply.limited: ...                       # still rate limited after the retries, or cooling down
    OL.get(url).raise_for_status().json()         # or raise HttpError(status, url, detail)

Rules:
- `user_agent` is required: public services ask to be told who is calling, and an app name says it without naming a
  person. Never put a person's name, address or e-mail in it here; the app passes its own.
- Requests to one host are spaced `min_interval` apart, across threads (each takes the next free slot, so ten threads
  do not all fire after one gap). `throttle_file=` spaces them across processes as well (several workers, a CLI beside
  the server), through a lock file.
- 429 and 503 wait for the server's Retry-After (seconds or an HTTP date), capped at `max_wait`, else 1, 2, 4... s. A host
  still saying 429 after `attempts` is left alone for `cool_down` seconds: calls return a `limited` reply at once instead
  of making every lookup wait out the limit again.
- 500, 502, 504 and network errors (refused, reset, timed out) are retried only for an idempotent request (GET, HEAD,
  PUT, DELETE, OPTIONS, or `idempotent=True` for a POST that only reads, such as an Overpass query). A 429 is retried
  for any method: the server said it did nothing.
- Errors come back as a `Reply` (status 0 when the server could not be reached, `.error` saying why), not exceptions,
  so a caller can record why a source found nothing. `raise_for_status()` turns one into `HttpError` for code that
  wants an exception. Only `https://` and `http://` URLs are fetched; `https_only=True` refuses plain http.
- Bodies over `max_bytes` are cut and the reply carries `error="body larger than ..."`, so one huge answer cannot fill
  memory.
- With `cache=DiskCache(...)`, a request with `cache_s=` is answered from disk while its copy is younger than that.
  Statuses in `cache_status` (200 and 404 by default: "not there" is an answer worth keeping) are stored.
- `observe(reply, attempt)` sees every attempt (listory's provider log, a test's record).

Standard library only. The clock, sleep and opener are arguments, but tests should prefer the `http_server` fixture,
which answers real requests on loopback."""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from typing import Any

from strata_python import oslib
from strata_python.files import DiskCache

IDEMPOTENT = frozenset({"GET", "HEAD", "PUT", "DELETE", "OPTIONS"})
RETRY_STATUS = frozenset({500, 502, 503, 504})
WAIT_STATUS = frozenset({429, 503})  # statuses whose Retry-After is honoured


@dataclass
class Reply:
    """What the server said. `status` is 0 when it could not be reached; `error` says why anything went wrong."""

    url: str
    status: int
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=lambda: {})  # names lower-cased
    error: str = ""
    limited: bool = False  # still rate limited after the retries, or the host is cooling down
    cached: bool = False  # answered from the disk cache

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and not self.error

    def json(self) -> Any:
        return json.loads(self.body)

    def text(self) -> str:
        ctype = self.headers.get("content-type", "")
        charset = ctype.split("charset=")[-1].split(";")[0].strip() if "charset=" in ctype else "utf-8"
        return self.body.decode(charset or "utf-8", errors="replace")

    def raise_for_status(self) -> Reply:
        """This reply, or `HttpError` when it is not `ok`."""
        if not self.ok:
            raise HttpError(self)
        return self


class HttpError(RuntimeError):
    """A reply that was not ok, for code that wants an exception: `.status`, `.url`, `.reply`, `.limited`."""

    def __init__(self, reply: Reply) -> None:
        host = urllib.parse.urlsplit(reply.url).netloc
        what = reply.error or reply.body[:200].decode(errors="replace").strip()
        said = f"answered {reply.status}" if reply.status else "could not be reached"
        super().__init__(f"{host} {said}" + (f": {what}" if what else ""))
        self.reply, self.status, self.url, self.limited = reply, reply.status, reply.url, reply.limited


def retry_after(headers: Mapping[str, str], attempt: int, *, max_wait: float, now: float | None = None) -> float:
    """Seconds to wait before attempt `attempt + 1`: the server's Retry-After (seconds or an HTTP date) when it sent
    one, else 1, 2, 4... s; at least 0.5 s and at most `max_wait`."""
    value = {k.lower(): v for k, v in headers.items()}.get("retry-after", "").strip()
    try:
        wait = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
            wait = when.timestamp() - (time.time() if now is None else now)
        except (TypeError, ValueError, IndexError):
            wait = 2.0 ** (attempt - 1)
    return max(0.5, min(wait, max_wait))


Opener = Callable[[urllib.request.Request, float], Any]


def _urlopen(req: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 - the scheme is checked before this is called


def _time_sleep(seconds: float) -> None:
    time.sleep(seconds)  # looked up per call, so a test's patched time.sleep applies


class Http:
    def __init__(
        self,
        user_agent: str,
        *,
        min_interval: float = 1.0,
        attempts: int = 4,
        max_wait: float = 60.0,
        cool_down: float = 600.0,
        timeout: float = 30.0,
        max_bytes: int = 50_000_000,
        headers: Mapping[str, str] | None = None,
        https_only: bool = False,
        throttle_file: str | os.PathLike[str] | None = None,
        cache: DiskCache | None = None,
        cache_status: frozenset[int] = frozenset({200, 404}),
        observe: Callable[[Reply, int], None] | None = None,
        opener: Opener = _urlopen,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if not user_agent.strip():
            raise ValueError("user_agent is required: name the app (and its purpose) so the service knows who asks")
        self.user_agent = user_agent
        self.min_interval, self.attempts, self.max_wait, self.cool_down = (
            min_interval,
            max(1, attempts),
            max_wait,
            cool_down,
        )
        self.timeout, self.max_bytes, self.https_only = timeout, max_bytes, https_only
        self.headers = dict(headers or {})
        self.throttle_file = None if throttle_file is None else os.fspath(throttle_file)
        self.cache, self.cache_status, self.observe = cache, cache_status, observe
        self._open, self._clock = opener, clock
        self._sleep = sleep or _time_sleep
        self._lock = threading.Lock()
        self._next: dict[str, float] = {}  # host -> the earliest time the next request may start
        self._cool_until: dict[str, float] = {}

    # ---- pacing ----------------------------------------------------------------------------------------------------

    def _wait_turn(self, host: str) -> None:
        """Take the next free slot for `host` and sleep until it. Slots are handed out under a lock, so concurrent
        threads queue up `min_interval` apart instead of all firing after one gap."""
        with self._lock:
            now = self._clock()
            start = max(now, self._next.get(host, now))
            self._next[host] = start + self.min_interval
        if start > now:
            self._sleep(start - now)
        if self.throttle_file:
            self._wait_file(self.throttle_file)

    def _wait_file(self, path: str) -> None:
        """The same gap across processes: the lock file holds the wall-clock time the last request started."""
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "a+", encoding="utf8") as f, oslib.file_lock(f):
            f.seek(0)
            try:
                last = float(f.read().strip() or 0)
            except ValueError:
                last = 0.0
            wait = last + self.min_interval - time.time()
            if wait > 0:
                self._sleep(min(wait, self.min_interval))
            f.seek(0)
            f.truncate()
            f.write(repr(time.time()))
            f.flush()

    def cooling(self, url_or_host: str) -> bool:
        """Is this host being left alone after a rate limit?"""
        host = urllib.parse.urlsplit(url_or_host).netloc or url_or_host
        return self._cool_until.get(host, 0.0) > self._clock()

    # ---- requests --------------------------------------------------------------------------------------------------

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, object] | None = None,
        json_body: object = None,
        data: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        idempotent: bool | None = None,
        cache_s: float | None = None,
        cache_key: str | None = None,
    ) -> Reply:
        """Send one request with the rules above and return what came back (never raises for an HTTP or network
        failure). `json_body` is sent as JSON; `data` as it is. `cache_s` reads and fills the cache."""
        method = method.upper()
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode({k: str(v) for k, v in params.items()})
        scheme, host = urllib.parse.urlsplit(url)[:2]
        if scheme != "https" and (self.https_only or scheme != "http"):
            return Reply(url, 0, error=f"only {'https' if self.https_only else 'http(s)'} addresses are fetched")
        body = json.dumps(json_body).encode() if json_body is not None else data
        hdrs = {"User-Agent": self.user_agent, **self.headers, **(headers or {})}
        if json_body is not None:
            hdrs.setdefault("Content-Type", "application/json")
        key = None
        if cache_s is not None and self.cache is not None:
            key = cache_key or json.dumps([method, url, body.decode(errors="replace") if body else None])
            hit = self._from_cache(self.cache, url, key, cache_s)
            if hit is not None:
                return hit
        if self.cooling(host):
            return Reply(url, 429, error="rate limited a moment ago; not asking again yet", limited=True)
        safe = method in IDEMPOTENT if idempotent is None else idempotent
        reply = Reply(url, 0, error="not attempted")
        for attempt in range(1, self.attempts + 1):
            self._wait_turn(host)
            reply = self._once(method, url, body, hdrs, self.timeout if timeout is None else timeout)
            if self.observe:
                self.observe(reply, attempt)
            if reply.status == 429 or (reply.status == 503 and "retry-after" in reply.headers):
                reply.limited = reply.status == 429
                wait = retry_after(reply.headers, attempt, max_wait=self.max_wait)
            elif safe and (reply.status in RETRY_STATUS or reply.status == 0):
                wait = 2.0 ** (attempt - 1)
            else:
                break
            if attempt < self.attempts:
                self._sleep(wait)
        if reply.limited:
            with self._lock:
                self._cool_until[host] = self._clock() + self.cool_down
        if key is not None and self.cache is not None and reply.status in self.cache_status and not reply.error:
            self._to_cache(self.cache, key, reply)
        return reply

    def _once(self, method: str, url: str, body: bytes | None, headers: dict[str, str], timeout: float) -> Reply:
        req = urllib.request.Request(url, data=body, headers=headers, method=method)  # noqa: S310 - scheme checked
        try:
            with self._open(req, timeout) as r:
                raw = r.read(self.max_bytes + 1)
                status = int(getattr(r, "status", None) or r.getcode())
                hdrs = {k.lower(): v for k, v in dict(r.headers).items()}
        except urllib.error.HTTPError as e:
            with contextlib.suppress(OSError, HTTPException):
                raw = e.read(self.max_bytes + 1)
                return Reply(
                    url, e.code, raw[: self.max_bytes], {k.lower(): v for k, v in dict(e.headers or {}).items()}
                )
            return Reply(url, e.code, error=str(e))
        except (urllib.error.URLError, OSError, ValueError, HTTPException) as e:
            return Reply(url, 0, error=str(getattr(e, "reason", None) or e))
        if len(raw) > self.max_bytes:
            return Reply(url, status, raw[: self.max_bytes], hdrs, error=f"body larger than {self.max_bytes} bytes")
        return Reply(url, status, raw, hdrs)

    @staticmethod
    def _from_cache(cache: DiskCache, url: str, key: str, max_age_s: float) -> Reply | None:
        raw = cache.get(key, max_age_s)
        if raw is None:
            return None
        head, _, body = raw.partition(b"\n")
        try:
            meta = json.loads(head)
            return Reply(url, int(meta["status"]), body, dict(meta.get("headers") or {}), cached=True)
        except (ValueError, KeyError, TypeError):
            return None

    @staticmethod
    def _to_cache(cache: DiskCache, key: str, reply: Reply) -> None:
        keep = {k: v for k, v in reply.headers.items() if k in ("content-type", "etag", "last-modified")}
        cache.put(key, json.dumps({"status": reply.status, "headers": keep}).encode() + b"\n" + reply.body)

    def get(self, url: str, **kw: Any) -> Reply:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw: Any) -> Reply:
        return self.request("POST", url, **kw)

    def get_json(self, url: str, *, missing: Any = None, **kw: Any) -> Any:
        """The parsed JSON of a GET; `missing` for a 404; `HttpError` for anything else that is not ok, or a body
        that is not JSON."""
        reply = self.get(url, **kw)
        if reply.status == 404:
            return missing
        reply.raise_for_status()
        try:
            return reply.json()
        except ValueError:
            raise HttpError(
                Reply(reply.url, reply.status, reply.body, reply.headers, error="the body is not JSON")
            ) from None
