"""The web side every app shares: health and settings routes, the built web app (or a stand-in page), and the uvicorn runner.

    app = base_app("myapp", __version__, SETTINGS)       # /api/health, GET/PUT /api/settings
    add_error_handlers(app)                               # ValueError -> 400, KeyError -> 404, ... as ErrorBody
    add_system(app, __version__, folders={"data": SETTINGS.home}, services={"strata-models": MODELS})  # GET /api/system
    ... app routes under /api ...
    add_web(app, STATIC, LANDING)                         # last: assets + the page at every other path

Reverse proxies: the server only sees the path under its own root, never the proxy's prefix, but the depth of that path says how many `../` lead back to the root, so each page
gets a relative `<base href>` (`with_base`) and one build works at the server's root and behind a proxy at /<prefix>/. The web app reads its base from `document.baseURI`, and its
assets are relative (Vite `base: './'`)."""

import argparse
import concurrent.futures
import datetime
import json
import os
import platform
import re
import shutil
import sys
import typing
from collections.abc import Callable, Mapping, Sequence
from typing import cast

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from strata_python.client import Rejected, ServiceClient, Unavailable
from strata_python.contracts import ErrorBody, Folder, ServiceStatus, SystemInfo
from strata_python.settings import Settings
from strata_python.tasks import Conflict


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


def base_app(
    title: str,
    version: str,
    settings: Settings | None = None,
    *,
    docs: bool = True,
    health: Callable[[], Mapping[str, object]] | None = None,
    commit: str | None = None,
) -> FastAPI:
    """A FastAPI app with `GET /api/health` and, given settings, `GET`/`PUT /api/settings` (a PUT with an unknown key or a value that fails validation is a 400 and changes nothing).
    `health` adds the app's own fields to the health response after `ok` and `version` (strata-models' `gpu`); it is called on every request, so keep it cheap,
    and it may set `ok` to false for a service that is up but cannot do its work. `commit` (`source_commit(__file__)`) is
    answered as `commit`, so a probe can tell which source a server runs without reading files beside it."""
    app = FastAPI(
        title=title,
        docs_url="/api/docs" if docs else None,
        openapi_url="/api/openapi.json" if docs else None,
    )

    @app.get("/api/health")
    def get_health() -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        return {
            "ok": True,
            "version": version,
            **({"commit": commit} if commit else {}),
            **(health() if health else {}),
        }

    if settings is not None:
        shape = settings.model()  # the settings as a typed object in /api/openapi.json, for the web app's types

        @app.get("/api/settings", response_model=shape)
        def get_settings() -> dict[str, str | list[str]]:  # pyright: ignore[reportUnusedFunction]
            return settings.all()

        @app.put("/api/settings", response_model=shape)
        # numbers too: with Settings(numbers=True) the web app sends back the numbers GET gave it; save() takes str(n)
        def put_settings(changes: dict[str, str | int | float | list[str] | None]) -> dict[str, str | list[str]]:  # pyright: ignore[reportUnusedFunction]
            try:
                settings.save(changes)
            except (KeyError, ValueError) as e:
                raise HTTPException(400, str(e.args[0]) if isinstance(e, KeyError) else str(e)) from e
            return settings.all()

    return app


def openapi_text(app: FastAPI) -> str:
    """The app's OpenAPI schema as the text an app commits for strata-react's `strata-api-types`: sorted keys, indent 1,
    non-ASCII kept, a final newline, so two runs and a git diff compare like with like. Nothing in it comes from the
    machine or the moment; `base_app`'s version is in `info.version`, so a release changes the file as it should."""
    return json.dumps(app.openapi(), indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def source_commit(path: str) -> str | None:
    """The git commit of the checkout that holds `path` (pass `__file__`), read from `.git` without running git, or
    None outside a checkout (an installed wheel). A worktree's `.git` file and packed refs are followed."""
    d = os.path.abspath(path if os.path.isdir(path) else os.path.dirname(path))
    while True:
        dot = os.path.join(d, ".git")
        if os.path.exists(dot):
            return _read_head(dot)
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def _read_head(dot: str) -> str | None:
    try:
        if os.path.isfile(dot):  # a worktree or submodule: "gitdir: <path>"
            with open(dot, encoding="utf8") as f:
                dot = os.path.join(os.path.dirname(dot), f.read().split(":", 1)[1].strip())
        with open(os.path.join(dot, "HEAD"), encoding="utf8") as f:
            head = f.read().strip()
        if not head.startswith("ref:"):
            return head or None  # detached
        ref = head.split(":", 1)[1].strip()
        common = dot
        if os.path.exists(os.path.join(dot, "commondir")):  # a worktree keeps its branches in the main .git
            with open(os.path.join(dot, "commondir"), encoding="utf8") as f:
                common = os.path.normpath(os.path.join(dot, f.read().strip()))
        for base in (dot, common):
            loose = os.path.join(base, *ref.split("/"))
            if os.path.isfile(loose):
                with open(loose, encoding="utf8") as f:
                    return f.read().strip() or None
        with open(os.path.join(common, "packed-refs"), encoding="utf8") as f:
            for line in f:
                sha, _, name = line.strip().partition(" ")
                if name == ref:
                    return sha
    except (OSError, IndexError):
        return None
    return None


Status = int | Callable[[Exception], int]
"""A status, or a function of the exception giving one: one class can mean two answers by its cause (a service's
error is a 503 when the service is not configured here and a 502 when it is configured but failed)."""
ErrorMap = Mapping[type[Exception], Status]
DEFAULT_ERRORS: ErrorMap = {
    ValueError: 400,  # a request the app cannot use (pydantic's ValidationError is one too)
    PermissionError: 403,
    FileNotFoundError: 404,
    KeyError: 404,  # an id the app does not know
    Conflict: 409,  # tasks.SingleFlight: that job is already running
    Rejected: 502,  # a service this app called refused: the request is fine, the dependency said no
    Unavailable: 503,  # a service this app needs is down
}


def add_error_handlers(app: FastAPI, errors: ErrorMap | None = None, *, defaults: bool = True) -> None:
    """Answer the app's own exceptions as HTTP errors with an `ErrorBody` ({detail, payload}), instead of a
    `try/except` and `HTTPException` in every route (the `guard()` five servers wrote). `errors` maps an exception
    class to a status and is added to `DEFAULT_ERRORS` (pass `defaults=False` for only yours); a subclass takes the
    closest mapped class. A status may be a function of the exception
    (`{YoutubeError: lambda e: 503 if not youtube.configured() else 502}`), so the answer keeps the one shape when its
    status depends on the cause. An exception's `.payload`, when it has one, is sent as `payload`."""
    table = {**(DEFAULT_ERRORS if defaults else {}), **(errors or {})}

    def handler(status: Status) -> Callable[[Request, Exception], JSONResponse]:
        def answer(request: Request, e: Exception) -> JSONResponse:
            detail = str(e.args[0]) if isinstance(e, KeyError) and e.args else str(e) or type(e).__name__
            body = ErrorBody(detail=detail, payload=getattr(e, "payload", None))
            code = status if isinstance(status, int) else status(e)
            return JSONResponse(body.model_dump(mode="json"), status_code=code)

        return answer

    for cls, status in table.items():
        app.add_exception_handler(cls, handler(status))  # pyright: ignore[reportArgumentType] - Starlette types the handler's exception as Exception


def _folder(name: str, path: str) -> Folder:
    probe = path
    while not os.path.exists(probe) and os.path.dirname(probe) != probe:
        probe = os.path.dirname(probe)  # the disk a missing folder will be on
    try:
        usage = shutil.disk_usage(probe)
        free, total = usage.free, usage.total
    except OSError:
        free = total = None
    return Folder(name=name, path=path, exists=os.path.isdir(path), free_bytes=free, total_bytes=total)


def _service(name: str, client: ServiceClient) -> ServiceStatus:
    try:
        h = client.health()
    except (Unavailable, Rejected) as e:
        return ServiceStatus(name=name, url=client.url(), ok=False, detail=str(e))
    version = h.get("version")
    return ServiceStatus(
        name=name,
        url=client.url(),
        ok=bool(h.get("ok")),
        version=None if version is None else str(version),
        detail=None if h.get("ok") else "answered, but not ok",
    )


def _app_name(title: str) -> str:
    """`my-app` -> `MyApp`, the prefix of the app's own schema components (as `Settings.model()` names them)."""
    return "".join(p.capitalize() for p in re.split(r"[^0-9A-Za-z]+", title) if p)


def _returned_model(fn: Callable[[], object] | None) -> type[BaseModel] | None:
    """The pydantic model `fn` is annotated to return, if any."""
    if fn is None:
        return None
    try:
        hint = typing.get_type_hints(fn).get("return")
    except Exception:  # an annotation that does not resolve (a lambda has none): no model, as before
        return None
    return hint if isinstance(hint, type) and issubclass(hint, BaseModel) else None


def add_system(
    app: FastAPI,
    version: str,
    *,
    commit: str | None = None,
    folders: Mapping[str, str | Callable[[], str]] | None = None,
    services: Mapping[str, ServiceClient | str] | None = None,
    extra: Callable[[], Mapping[str, object] | BaseModel] | None = None,
    extra_model: type[BaseModel] | None = None,
) -> None:
    """`GET /api/system`, a `SystemInfo`: the version and commit, Python and platform, when the server started, each
    named folder with its disk's free and total space, and each service this app calls with what its health endpoint
    answers now (probed in parallel, 2 s each). A folder is a path or a function giving one (a setting read on each
    request); a service is a `ServiceClient` or a base URL. `extra` adds the app's own fields (a queue length), as a
    mapping or a model. Their model, `extra_model` or the model `extra` is annotated to return, is merged into the
    response model, named `<App>SystemInfo` after the app's title, so the schema and the generated types list them;
    without one they are sent but not described."""
    started = datetime.datetime.now(datetime.UTC)
    clients = {k: v if isinstance(v, ServiceClient) else ServiceClient(v) for k, v in (services or {}).items()}
    fields = extra_model or _returned_model(extra)
    shape: type[SystemInfo] = SystemInfo
    if fields is not None:
        # one component holding both: the web app's type is Schema<'<App>SystemInfo'>, nothing written by hand
        shape = cast("type[SystemInfo]", type(_app_name(app.title) + "SystemInfo", (SystemInfo, fields), {}))

    def own() -> dict[str, object]:
        got: Mapping[str, object] | BaseModel = extra() if extra else {}
        # by alias: the merged model validates by the same keys the app's model does (review finding)
        return got.model_dump(by_alias=True) if isinstance(got, BaseModel) else dict(got)

    @app.get("/api/system", response_model=shape)
    def get_system() -> SystemInfo:  # pyright: ignore[reportUnusedFunction]
        paths = {k: v if isinstance(v, str) else v() for k, v in (folders or {}).items()}
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(len(clients), 1)) as pool:
            probes = [pool.submit(_service, k, c) for k, c in clients.items()]
            statuses = [p.result() for p in probes]
        return shape(
            version=version,
            commit=commit,
            python=sys.version.split()[0],
            platform=platform.platform(terse=True),
            started_at=started,
            folders=[_folder(k, p) for k, p in paths.items()],
            services=statuses,
            **own(),
        )


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
