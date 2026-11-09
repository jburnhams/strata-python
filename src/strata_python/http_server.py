"""A real HTTP server on 127.0.0.1 for tests of code that makes outbound requests. The code under test sends actual requests over a real socket (urllib, httpx, requests: whatever it uses), so URL building, headers, encodings and timeouts are all exercised; only the far end is scripted.

    def test_fetch(http_server):
        http_server.route("GET", "/items/1", {"id": 1})                 # JSON body, 200
        http_server.route("GET", "/slow", status=503, body="busy")      # a failing reply
        assert fetch(http_server.url + "/items/1") == {"id": 1}
        assert http_server.requests[0].headers["accept"] == "application/json"

Every request is recorded in `.requests` (method, path, query, headers, body); a request no route answers gets a 404 and fails the test at teardown, so a forgotten route is never silent. Use it through the `http_server` fixture the strata_python pytest plugin provides."""

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit


@dataclass
class Recorded:
    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    body: bytes

    def json(self):
        return json.loads(self.body)


@dataclass
class Route:
    status: int
    body: bytes
    headers: dict[str, str]
    once: bool


@dataclass
class ScriptedServer:
    httpd: ThreadingHTTPServer
    routes: dict[tuple[str, str], list[Route]] = field(default_factory=lambda: {})
    requests: list[Recorded] = field(default_factory=lambda: [])
    unexpected: list[Recorded] = field(default_factory=lambda: [])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def route(
        self,
        method: str,
        path: str,
        body: object = b"",
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        once: bool = False,
    ):
        """Answer `method path` (query ignored) with `body` (bytes, str, or anything JSON-serialisable). A route answers every matching request; with `once=True` it answers one and is then used up, so several `once` routes for the same request script a sequence (503, then 200)."""
        if isinstance(body, bytes):
            raw, ctype = body, "application/octet-stream"
        elif isinstance(body, str):
            raw, ctype = body.encode(), "text/plain; charset=utf-8"
        else:
            raw, ctype = json.dumps(body).encode(), "application/json"
        self.routes.setdefault((method.upper(), path), []).append(
            Route(status, raw, {"Content-Type": ctype, **(headers or {})}, once)
        )
        return self


def make_handler(state: "ScriptedServer"):
    class Handler(BaseHTTPRequestHandler):
        def _serve(self):
            parts = urlsplit(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            rec = Recorded(
                self.command,
                parts.path,
                parse_qs(parts.query),
                {k.lower(): v for k, v in self.headers.items()},
                self.rfile.read(length) if length else b"",
            )
            state.requests.append(rec)
            queue = state.routes.get((self.command, parts.path))
            if not queue:
                state.unexpected.append(rec)
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            route = queue.pop(0) if queue[0].once else queue[0]
            self.send_response(route.status)
            for k, v in route.headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(route.body)))
            self.end_headers()
            self.wfile.write(route.body)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _serve

        def log_message(self, format: str, *args: Any) -> None:
            pass

    return Handler


def start() -> ScriptedServer:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    state = ScriptedServer(httpd)
    httpd.RequestHandlerClass = make_handler(state)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return state


def stop(state: ScriptedServer) -> None:
    state.httpd.shutdown()
    state.httpd.server_close()
