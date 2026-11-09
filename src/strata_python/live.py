"""Run a FastAPI app on a real loopback port, so a test makes actual HTTP requests (urllib, httpx) against the real stack instead of a TestClient."""

import contextlib
import threading
import time
from collections.abc import Generator

import uvicorn
from fastapi import FastAPI


@contextlib.contextmanager
def live_server(app: FastAPI, *, timeout: float = 10.0) -> Generator[str]:
    """Yield the base URL (`http://127.0.0.1:<port>`) of `app` served by uvicorn on a free port; the server stops when the block ends."""
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + timeout
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            server.should_exit = True  # do not leave a half-started server running
            thread.join(10)
            raise RuntimeError("the server did not start")
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout)
