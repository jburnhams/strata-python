import json
import urllib.error
import urllib.request

import pytest

from strata_python import web
from strata_python.settings import Setting, Settings, absolute_path


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
def app(static, landing):
    s = Settings(
        "demo", [Setting("data_dir", default="/tmp/x", normalise=absolute_path), Setting("greeting", default="Hello")]
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
