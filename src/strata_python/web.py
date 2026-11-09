"""The web side every app shares: health and settings routes, the built web app (or a stand-in page), and the uvicorn runner.

    app = base_app("myapp", __version__, SETTINGS)       # /api/health, GET/PUT /api/settings
    ... app routes under /api ...
    add_web(app, STATIC, LANDING)                         # last: assets + the page at every other path

Reverse proxies: the server only sees the path under its own root, never the proxy's prefix, but the depth of that path says how many `../` lead back to the root, so each page
gets a relative `<base href>` (`with_base`) and one build works at the server's root and behind a proxy at /<prefix>/. The web app reads its base from `document.baseURI`, and its
assets are relative (Vite `base: './'`)."""

import argparse
import os
from collections.abc import Sequence

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from strata_python.settings import Settings


def with_base(html: str, path: str) -> str:
    """Insert `<base href>` pointing back at the server root for a page served at `path` (no leading slash)."""
    tag = f'<base href="{"../" * path.count("/") or "./"}">'
    return html.replace("<head>", "<head>" + tag, 1) if "<head>" in html else tag + html


def static_file(static: str, path: str) -> str | None:
    """The file directly inside `static` that `path` names, or None. Only a plain file name is served: a path with either separator, a drive or `..` could reach outside
    the folder (on Windows `..\\app.py` has no `/`, and `os.path.join` drops the root for `C:\\...`), so the resolved file must also sit directly in `static`."""
    if not path or "/" in path or "\\" in path or ":" in path or path in (".", ".."):
        return None
    root = os.path.realpath(static)
    full = os.path.realpath(os.path.join(root, path))
    return full if os.path.dirname(full) == root and os.path.isfile(full) else None


def base_app(title: str, version: str, settings: Settings | None = None, *, docs: bool = True) -> FastAPI:
    """A FastAPI app with `GET /api/health` and, given settings, `GET`/`PUT /api/settings` (a PUT with an unknown key or a value that fails validation is a 400 and changes nothing)."""
    app = FastAPI(
        title=title,
        docs_url="/api/docs" if docs else None,
        openapi_url="/api/openapi.json" if docs else None,
    )

    @app.get("/api/health")
    def get_health() -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        return {"ok": True, "version": version}

    if settings is not None:

        @app.get("/api/settings")
        def get_settings() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
            return settings.all()

        @app.put("/api/settings")
        def put_settings(changes: dict[str, str | None]) -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
            try:
                settings.save(changes)
            except (KeyError, ValueError) as e:
                raise HTTPException(400, str(e.args[0]) if isinstance(e, KeyError) else str(e)) from e
            return settings.all()

    return app


def add_web(app: FastAPI, static: str, landing: str | None = None) -> None:
    """Serve the built web app: `static/assets` at /assets, files directly in `static` (favicon and the like) at their name, and `static/index.html` (else the plain `landing` page)
    at every other path outside /api, so a reload or a shared link opens that screen (the app routes in the browser). Call it after the app's own routes: the catch-all is registered here."""
    if os.path.isdir(os.path.join(static, "assets")):
        app.mount("/assets", StaticFiles(directory=os.path.join(static, "assets")), name="assets")

    @app.get("/{path:path}", response_class=HTMLResponse, response_model=None, include_in_schema=False)
    def page(path: str) -> HTMLResponse | FileResponse:  # pyright: ignore[reportUnusedFunction]
        if path.startswith("api/"):
            raise HTTPException(404, "no such endpoint")
        top = static_file(static, path)
        if top:
            return FileResponse(top)
        built = os.path.join(static, "index.html")
        source = built if os.path.exists(built) else landing
        if source is None:
            raise HTTPException(404, "the web app has not been built")
        with open(source, encoding="utf8") as f:
            return HTMLResponse(with_base(f.read(), path))


def serve(app: FastAPI, argv: Sequence[str] | None = None, *, default_port: int = 8000, banner: str = "") -> None:
    """Run `app` with uvicorn: `--host` (default 127.0.0.1) and `--port`. The banner (what the app wants shown, such as its data folder) is printed before the URL."""
    a = argparse.ArgumentParser()
    a.add_argument("--host", default="127.0.0.1")
    a.add_argument("--port", type=int, default=default_port)
    ns = a.parse_args(argv)
    print(
        f"{banner}\nhttp://{ns.host}:{ns.port}/" if banner else f"http://{ns.host}:{ns.port}/", flush=True
    )  # flushed: a service logs to a pipe
    uvicorn.run(app, host=ns.host, port=ns.port, log_level="warning")
