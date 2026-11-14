"""A typed base for calling another strata service's HTTP API: standard library only, so it runs in any venv.

The models client, the 360 and models self-CLIs and the homepage probes each wrote the same urllib wrapper: build a
JSON request, map a refusal to one error and an outage to another, poll until a row finishes. This is that once.

    MODELS = ServiceClient(env_url("STRATA_MODELS_URL", "http://127.0.0.1:8366"), app="strata-scan")
    MODELS.get("/api/registry")                               # parsed JSON
    MODELS.post("/api/jobs", {"role": "ocr", ...})
    MODELS.wait(f"/api/jobs/{jid}", lambda row: row["status"] in FINISHED, timeout=600)   # long-polls with ?wait=
    MODELS.available()                                        # GET /api/health answered ok

Errors: `Rejected` when the service answered 4xx (it understood and said no: `.status`, `.detail`, `.payload`, the
`ErrorBody` shape), `Unavailable` when it could not be asked or answered 5xx. Reads (GET, PUT, DELETE, HEAD: the
idempotent methods) are retried `retries` times on `Unavailable`, with a doubling pause; a POST is not, since a
retried POST can do the work twice.

The URL is read on every call, so `env_url` picks up a changed environment variable (a test's stand-in service) and
no URL is fixed at import. `app` is sent as `X-Strata-App` and in the User-Agent, so the service can say who asked."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any, cast

Json = dict[str, Any]
IDEMPOTENT = frozenset({"GET", "HEAD", "PUT", "DELETE"})


class Unavailable(RuntimeError):
    """The service could not be asked (not running, not reachable) or failed while answering (5xx)."""


class Rejected(RuntimeError):
    """The service answered 4xx: the request was understood and refused. `.detail` is its reason, `.payload` what it
    sent with it (an `ErrorBody` payload, or the whole body when it was not one)."""

    def __init__(self, status: int, detail: str, payload: Any = None) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.payload = payload


def env_url(var: str, default: str) -> Callable[[], str]:
    """The service URL from environment variable `var`, read on each call, else `default`."""
    return lambda: os.environ.get(var) or default


class ServiceClient:
    def __init__(
        self,
        base_url: str | Callable[[], str],
        *,
        app: str | None = None,
        timeout: float = 30,
        retries: int = 2,
        backoff_s: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._base = base_url
        self.app = app
        self.timeout = timeout
        self.retries = retries
        self.backoff_s = backoff_s
        self._sleep = sleep

    def url(self) -> str:
        b = self._base
        return (b if isinstance(b, str) else b()).rstrip("/")

    def call(
        self,
        method: str,
        path: str,
        body: object = None,
        *,
        params: Mapping[str, object] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Send one request and return the parsed JSON answer (None for an empty body)."""
        method = method.upper()
        attempts = 1 + (self.retries if method in IDEMPOTENT else 0)
        for attempt in range(attempts):
            try:
                return self._once(method, path, body, params, self.timeout if timeout is None else timeout)
            except Unavailable:
                if attempt == attempts - 1:
                    raise
                self._sleep(self.backoff_s * 2**attempt)
        raise AssertionError("unreachable")  # pragma: no cover - the loop returns or raises

    def _once(self, method: str, path: str, body: object, params: Mapping[str, object] | None, timeout: float) -> Any:
        url = self.url() + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode({k: str(v) for k, v in params.items()})
        data = None if body is None else json.dumps(body).encode()
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self.app:
            headers["X-Strata-App"] = self.app
            headers["User-Agent"] = f"{self.app} (strata-python)"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)  # noqa: S310 - the service's own URL
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
                raw = r.read()
        except urllib.error.HTTPError as e:
            answer = _parse(e.read())
            detail, payload = _reason(answer, e)
            if 400 <= e.code < 500:
                raise Rejected(e.code, detail, payload) from None
            raise Unavailable(f"{self.url()} answered {e.code}: {detail}") from None
        except (urllib.error.URLError, OSError) as e:
            raise Unavailable(f"{self.url()} is not reachable: {getattr(e, 'reason', e)}") from None
        return _parse(raw) if raw else None

    def get(self, path: str, **kw: Any) -> Any:
        return self.call("GET", path, **kw)

    def post(self, path: str, body: object = None, **kw: Any) -> Any:
        return self.call("POST", path, body, **kw)

    def put(self, path: str, body: object = None, **kw: Any) -> Any:
        return self.call("PUT", path, body, **kw)

    def delete(self, path: str, **kw: Any) -> Any:
        return self.call("DELETE", path, **kw)

    def health(self, timeout: float = 2.0) -> Json:
        """`GET /api/health`, once (no retries: a probe wants the answer now)."""
        return cast("Json", self._once("GET", "/api/health", None, None, timeout) or {})

    def available(self, timeout: float = 2.0) -> bool:
        """Is the service up and saying it is ok?"""
        try:
            return bool(self.health(timeout).get("ok"))
        except (Unavailable, Rejected):
            return False

    def wait(
        self,
        path: str,
        done: Callable[[Json], bool],
        *,
        timeout: float | None = None,
        poll_s: float = 30,
        clock: Callable[[], float] = time.monotonic,
    ) -> Json:
        """Long-poll `path?wait=<s>` until `done(row)`; TimeoutError (with the last row's state) after `timeout`.
        The service holds each request up to `wait` seconds and answers early when the row changes, so this costs one
        request per change, not one per second."""
        end = None if timeout is None else clock() + timeout
        while True:
            step = poll_s if end is None else max(min(poll_s, end - clock()), 0)
            row = cast("Json", self.get(path, params={"wait": f"{step:.1f}"}, timeout=step + 15))
            if done(row):
                return row
            if end is not None and clock() >= end:
                raise TimeoutError(
                    f"{path} did not finish in {timeout} s (last: {row.get('status') or row.get('state')})"
                )


def _parse(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode(errors="replace").strip() or None


def _reason(answer: Any, e: urllib.error.HTTPError) -> tuple[str, Any]:
    """The reason and payload in an error answer: `ErrorBody` ({detail, payload}), FastAPI's {detail}, or plain text."""
    if isinstance(answer, dict):
        d = cast("Json", answer)
        detail = d.get("detail")
        text = detail if isinstance(detail, str) else json.dumps(detail) if detail is not None else str(e.reason)
        return text, d.get("payload", d if set(d) - {"detail"} else None)
    return (answer if isinstance(answer, str) and answer else str(e.reason)), None
