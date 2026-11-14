import json
import urllib.error
import urllib.request

import pytest

from strata_python import web
from strata_python.settings import ListSetting, Setting, Settings, absolute_path


def test_with_base_points_back_at_the_root():
    html = "<html><head><title>x</title></head></html>"
    assert '<head><base href="./">' in web.with_base(html, "")
    assert '<base href="./">' in web.with_base(html, "settings")
    assert '<base href="../../">' in web.with_base(html, "a/b/c")


def test_with_base_without_a_head_prepends_the_tag():
    assert web.with_base("<p>hi</p>", "a/b") == '<base href="../">' + "<p>hi</p>"


@pytest.fixture
def static(tmp_path):
    root = tmp_path / "static"
    (root / "assets").mkdir(parents=True)
    (root / "assets" / "app.js").write_text("console.log(1)")
    (root / "favicon.svg").write_text("<svg/>")
    (tmp_path / "secret.txt").write_text("secret")
    return str(root)


@pytest.mark.parametrize(
    "path", ["", "a/b", "..", ".", "../secret.txt", "..\\secret.txt", "C:\\x", "assets/app.js", "missing.svg"]
)
def test_static_file_refuses_everything_but_a_plain_name_that_exists(static, path):
    assert web.static_file(static, path) is None


def test_static_file_finds_a_file_in_the_folder(static):
    found = web.static_file(static, "favicon.svg")
    assert found is not None
    assert found.endswith("favicon.svg")


def get(url: str) -> tuple[int, str, str]:
    try:
        with urllib.request.urlopen(url) as r:
            return r.status, r.read().decode(), r.headers.get("content-type", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), e.headers.get("content-type", "")


def put(url: str, body: object) -> tuple[int, str]:
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"}, method="PUT")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


@pytest.fixture
def landing(tmp_path):
    p = tmp_path / "landing.html"
    p.write_text("<html><head><title>stand-in</title></head><body>stand-in</body></html>")
    return str(p)


@pytest.fixture
def app(static, landing, tmp_path):
    s = Settings(
        "demo",
        [
            Setting("data_dir", default=str(tmp_path / "data"), normalise=absolute_path),
            Setting("greeting", default="Hello"),
        ],
    )
    a = web.base_app("demo", "1.2.3", s)
    web.add_web(a, static, landing)
    return a


def test_health_settings_and_pages_over_real_http(app, live_app):
    with live_app(app) as base:
        assert json.loads(get(base + "/api/health")[1]) == {"ok": True, "version": "1.2.3"}
        assert json.loads(get(base + "/api/settings")[1])["greeting"] == "Hello"
        status, text = put(base + "/api/settings", {"greeting": "Hi"})
        assert status == 200
        assert json.loads(text)["greeting"] == "Hi"
        status, text = put(base + "/api/settings", {"data_dir": "relative"})
        assert status == 400
        assert "absolute" in text
        status, text = put(base + "/api/settings", {"nope": "1"})
        assert status == 400
        assert "unknown setting" in text
        assert json.loads(get(base + "/api/settings")[1])["greeting"] == "Hi"


def test_pages_get_a_base_and_unknown_api_paths_are_404(app, live_app):
    with live_app(app) as base:
        status, html, ctype = get(base + "/settings/deep")
        assert status == 200
        assert '<base href="../">' in html
        assert "stand-in" in html
        assert ctype.startswith("text/html")
        assert get(base + "/api/nothing")[0] == 404
        assert get(base + "/favicon.svg")[1] == "<svg/>"
        assert get(base + "/assets/app.js")[1] == "console.log(1)"


def test_the_built_index_wins_over_the_landing_page(static, landing, live_app):
    with open(static + "/index.html", "w", encoding="utf8") as f:
        f.write("<html><head></head><body>built</body></html>")
    a = web.base_app("demo", "1")
    web.add_web(a, static, landing)
    with live_app(a) as base:
        assert "built" in get(base + "/")[1]
        assert get(base + "/api/settings")[0] == 404  # no settings given, no settings routes


def test_no_built_app_and_no_landing_is_a_404(static, live_app):
    a = web.base_app("demo", "1", docs=False)
    web.add_web(a, static)
    with live_app(a) as base:
        assert get(base + "/")[0] == 404
        assert get(base + "/api/docs")[0] == 404


def test_health_carries_the_apps_own_fields(live_app):
    """Issue #17: strata-models' `{ok, version, gpu}`, called per request, and able to say the service cannot work."""
    state = {"gpu": "RTX 4090"}
    app = web.base_app(
        "models", "2.0", health=lambda: {"gpu": state["gpu"]} if state["gpu"] else {"ok": False, "gpu": None}
    )
    with live_app(app) as base:
        assert json.loads(get(base + "/api/health")[1]) == {"ok": True, "version": "2.0", "gpu": "RTX 4090"}
        state["gpu"] = ""
        assert json.loads(get(base + "/api/health")[1]) == {"ok": False, "version": "2.0", "gpu": None}


def test_list_settings_over_real_http(live_app, tmp_path):
    """Issue #10: GET returns a list setting as a JSON list, PUT takes a list (or a joined string), a bad item is a 400."""
    s = Settings("demo", [ListSetting("source_roots", item=absolute_path), ListSetting("names", sep=",")])
    a, b = str(tmp_path / "a"), str(tmp_path / "b")
    with live_app(web.base_app("demo", "1", s)) as base:
        assert json.loads(get(base + "/api/settings")[1]) == {"source_roots": [], "names": []}
        status, text = put(base + "/api/settings", {"source_roots": [a, b], "names": "x,y"})
        assert status == 200
        assert json.loads(text) == {"source_roots": [a, b], "names": ["x", "y"]}
        status, text = put(base + "/api/settings", {"source_roots": ["relative"]})
        assert status == 400
        assert "absolute" in text


def test_error_handlers_answer_the_apps_exceptions_as_error_bodies(live_app):
    from strata_python.client import Unavailable
    from strata_python.tasks import Conflict

    class Busy(RuntimeError):
        def __init__(self, msg):
            super().__init__(msg)
            self.payload = {"queue": 3}

    app = web.base_app("demo", "1")
    web.add_error_handlers(app, {Busy: 429})
    raises = {
        "value": ValueError("page must be a number"),
        "key": KeyError("book 7"),
        "missing": FileNotFoundError("cover.jpg"),
        "conflict": Conflict("scan is already running", {"key": "a"}),
        "down": Unavailable("strata-models is not reachable"),
        "busy": Busy("try later"),
    }

    @app.get("/api/raise/{what}")
    def boom(what: str):
        raise raises[what]

    with live_app(app) as base:

        def get(what):
            try:
                with urllib.request.urlopen(base + "/api/raise/" + what) as r:
                    return r.status, json.load(r)
            except urllib.error.HTTPError as e:
                return e.code, json.load(e) if e.code != 500 else None

        assert get("value") == (400, {"detail": "page must be a number", "payload": None})
        assert get("key") == (404, {"detail": "book 7", "payload": None})  # no quotes, as str(KeyError) has
        assert get("missing")[0] == 404
        assert get("conflict") == (409, {"detail": "scan is already running", "payload": {"key": "a"}})
        assert get("down")[0] == 503
        assert get("busy") == (429, {"detail": "try later", "payload": {"queue": 3}})
    only = web.base_app("demo", "1")
    web.add_error_handlers(only, {Busy: 429}, defaults=False)
    assert ValueError not in only.exception_handlers


def test_system_reports_folders_disks_and_services(live_app, tmp_path):
    from strata_python.client import ServiceClient

    up = web.base_app("models", "4.2")
    data = tmp_path / "data"
    data.mkdir()
    with live_app(up) as up_url:
        app = web.base_app("demo", "1", commit="abc123")
        web.add_system(
            app,
            "1",
            commit="abc123",
            folders={"data": str(data), "later": lambda: str(tmp_path / "not-yet")},
            services={"strata-models": ServiceClient(up_url), "gone": "http://127.0.0.1:9"},
            extra=lambda: {"queued": 2},
        )
        with live_app(app) as base:
            with urllib.request.urlopen(base + "/api/system") as r:
                info = json.load(r)
            with urllib.request.urlopen(base + "/api/health") as r:
                health = json.load(r)
    assert (info["version"], info["commit"], info["queued"]) == ("1", "abc123", 2)
    assert info["python"].count(".") == 2
    folders = {f["name"]: f for f in info["folders"]}
    assert folders["data"]["exists"]
    assert folders["data"]["free_bytes"] > 0
    assert not folders["later"]["exists"]
    assert folders["later"]["total_bytes"] > 0  # the disk it will be on
    services = {s["name"]: s for s in info["services"]}
    assert (services["strata-models"]["ok"], services["strata-models"]["version"]) == (True, "4.2")
    assert not services["gone"]["ok"]
    assert "not reachable" in services["gone"]["detail"]
    assert health == {"ok": True, "version": "1", "commit": "abc123"}


def test_system_says_when_a_service_answers_but_is_not_ok(live_app):
    sick = web.base_app("models", "4", health=lambda: {"ok": False})
    with live_app(sick) as sick_url:
        app = web.base_app("demo", "1")
        web.add_system(app, "1", services={"models": sick_url})
        with live_app(app) as base, urllib.request.urlopen(base + "/api/system") as r:
            (s,) = json.load(r)["services"]
    assert (s["ok"], s["detail"]) == (False, "answered, but not ok")


def test_source_commit_reads_git_without_git(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git" / "refs" / "heads").mkdir(parents=True)
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("")
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (repo / ".git" / "refs" / "heads" / "main").write_text("a" * 40 + "\n")
    assert web.source_commit(str(repo / "src" / "app.py")) == "a" * 40
    (repo / ".git" / "refs" / "heads" / "main").unlink()
    (repo / ".git" / "packed-refs").write_text(f"# pack-refs\n{'b' * 40} refs/heads/main\n")
    assert web.source_commit(str(repo / "src")) == "b" * 40
    (repo / ".git" / "HEAD").write_text("c" * 40)
    assert web.source_commit(str(repo)) == "c" * 40  # detached
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/gone\n")
    assert web.source_commit(str(repo)) is None
    # a worktree: .git is a file naming its folder in the main repo, branches in the common dir
    wt_git = repo / ".git" / "worktrees" / "wt"
    wt_git.mkdir(parents=True)
    (wt_git / "HEAD").write_text("ref: refs/heads/feature\n")
    (wt_git / "commondir").write_text("../..\n")
    (repo / ".git" / "refs" / "heads" / "feature").write_text("d" * 40)
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {wt_git}\n")
    assert web.source_commit(str(wt)) == "d" * 40
    assert web.source_commit("/") is None


def test_settings_routes_describe_each_setting_in_openapi(live_app, tmp_path):
    s = Settings("demo-app", [Setting("greeting", default="hi"), ListSetting("roots", item=absolute_path)])
    with live_app(web.base_app("demo", "1", s)) as base, urllib.request.urlopen(base + "/api/openapi.json") as r:
        schema = json.load(r)["components"]["schemas"]["DemoAppSettings"]
    assert schema["properties"]["greeting"]["type"] == "string"
    assert schema["properties"]["roots"]["type"] == "array"
    assert set(schema["required"]) == {"greeting", "roots"}
