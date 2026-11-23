import argparse
import sys

import pytest

from strata_python import cli
from strata_python.settings import ListSetting, Setting, Settings


def build(settings=None, run=None, imports=("json",)):
    p = argparse.ArgumentParser(prog="demo")
    sub = p.add_subparsers(dest="cmd", required=True)
    cli.add_doctor(sub, "demo", "9.9", imports)
    if settings is not None:
        cli.add_set(sub, settings)
    if run is not None:
        cli.add_serve(sub, run, default_port=8123)
    return p


def go(p, *argv):
    a = p.parse_args(argv)
    return a.fn(a)


def test_doctor_reports_modules(capsys):
    assert go(build(imports=("json", "no_such_module_here")), "doctor") == 1
    out = capsys.readouterr().out
    assert out.startswith("demo 9.9, python ")
    assert "ok       json" in out
    assert "MISSING  no_such_module_here" in out


def test_doctor_labels_optional_modules_checks_and_hint(capsys):
    """Issues #15 and #17: labels, optional modules, the app's checks in the same columns, its own hint."""

    def gpu():
        return ("slow", "no CUDA device, running on the CPU")

    def crashes():
        raise RuntimeError("driver mismatch")

    checks = [
        cli.Check("python on the PATH", cli.program(sys.executable)),  # a full path: found on every OS
        cli.Check("gpu", gpu),
        cli.Check("cudnn", lambda: ("missing", "off"), optional=True),
    ]
    imports = [("JSON (settings files)", "json"), cli.Module("ultralytics-ish", "no_such_module_here", optional=True)]
    assert cli.doctor("demo", "1", imports, checks=checks, hint="uv sync --group gpu") == 0
    out = capsys.readouterr().out.splitlines()
    assert out[1].startswith("  ok       JSON (settings files) ")
    assert out[2] == "  missing  ultralytics-ish: uv sync --group gpu"
    assert out[3] == f"  ok       python on the PATH {sys.executable}"
    assert out[4] == "  slow     gpu: no CUDA device, running on the CPU"
    assert out[5] == "  missing  cudnn: off"

    failing = [
        cli.Check("exiftool", cli.program("no-such-program-here", "apt install exiftool")),
        cli.Check("x", crashes),
    ]
    assert cli.doctor("demo", "1", (), checks=failing) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[1:] == ["  MISSING  exiftool: apt install exiftool", "  BROKEN   x: RuntimeError: driver mismatch"]


def test_add_doctor_passes_checks_and_hint(capsys):
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    cli.add_doctor(sub, "demo", "1", ["no_such_module_here"], checks=[cli.Check("c", lambda: ("ok", ""))], hint="h")
    assert go(p, "doctor") == 1
    assert "  MISSING  no_such_module_here: h" in capsys.readouterr().out


def test_doctor_ok(capsys):
    assert go(build(), "doctor") == 0


def test_set_shows_changes_and_resets(capsys):
    s = Settings("demo", [Setting("greeting", default="Hello")])
    p = build(s)
    assert go(p, "set", "greeting") == 0
    assert capsys.readouterr().out.strip() == "greeting = Hello"
    go(p, "set", "greeting", "Hi")
    assert capsys.readouterr().out.strip() == "greeting = Hi"
    go(p, "set", "greeting", "default")
    assert capsys.readouterr().out.strip() == "greeting = Hello"


def test_set_rejects_unknown_keys_and_bad_values():
    from strata_python.settings import non_empty

    s = Settings("demo", [Setting("greeting", default="Hello", normalise=non_empty)])
    p = build(s)
    with pytest.raises(SystemExit, match="unknown setting 'x'"):
        go(p, "set", "x")
    with pytest.raises(SystemExit, match="greeting: must not be blank"):
        go(p, "set", "greeting", " ")


def test_serve_passes_host_and_port():
    seen: list[tuple[str, int]] = []
    p = build(run=lambda h, port: seen.append((h, port)))
    assert go(p, "serve") == 0
    assert go(p, "serve", "--host", "0.0.0.0", "--port", "9000") == 0
    assert seen == [("127.0.0.1", 8123), ("0.0.0.0", 9000)]


def test_set_shows_and_takes_a_list_joined(capsys):
    s = Settings("demo", [ListSetting("names", default=("a",), sep=",")])
    p = build(s)
    go(p, "set", "names")
    assert capsys.readouterr().out.strip() == "names = a"
    go(p, "set", "names", "a,b, c")
    assert capsys.readouterr().out.strip() == "names = a,b,c"
    assert s.get_list("names") == ["a", "b", "c"]


def _app():
    from strata_python import web

    app = web.base_app("démo", "1.0")

    @app.get("/api/zeta")
    def zeta() -> dict[str, int]:
        return {"z": 1}

    return app


def openapi_parser(default):
    p = argparse.ArgumentParser(prog="demo")
    cli.add_openapi(p.add_subparsers(dest="cmd", required=True), _app, default=default)
    return p


def test_openapi_writes_checks_and_prints(tmp_path, capsys):
    """Issue #36: the schema file strata-api-types reads, stable so a diff compares like with like."""
    target = tmp_path / "web" / "openapi.json"
    p = openapi_parser(target)
    assert go(p, "openapi", "--check") == 1
    assert "missing" in capsys.readouterr().err
    assert go(p, "openapi") == 0
    text = target.read_bytes().decode()
    assert text.endswith("}\n")
    assert "\r" not in text
    assert '"title": "démo"' in text  # not escaped
    assert text.index('"/api/health"') < text.index('"/api/zeta"')  # keys sorted
    assert go(p, "openapi", "--check") == 0
    target.write_text(text.replace("démo", "demo"), encoding="utf-8")
    assert go(p, "openapi", "--check") == 1
    assert "out of date" in capsys.readouterr().err
    other = tmp_path / "other.json"
    assert go(p, "openapi", "-o", str(other)) == 0
    assert other.read_text(encoding="utf-8") == text
    capsys.readouterr()
    assert go(p, "openapi", "-o", "-") == 0
    assert capsys.readouterr().out == text
    with pytest.raises(SystemExit, match="give -o PATH"):
        go(p, "openapi", "-o", "-", "--check")


def test_assert_openapi_current(tmp_path):
    from strata_python.testing import assert_openapi_current

    target = tmp_path / "openapi.json"
    with pytest.raises(AssertionError, match="missing"):
        assert_openapi_current(_app, target)
    go(openapi_parser(target), "openapi")
    assert_openapi_current(_app, target)
    target.write_text("{}\n")
    with pytest.raises(AssertionError, match="out of date"):
        assert_openapi_current(_app, target)


def test_set_shows_the_choices(capsys):
    s = Settings("demo", [Setting("mode", default="mixed", choices=["mixed", "review"])])
    go(build(s), "set", "mode", "Review")
    assert capsys.readouterr().out.strip() == "mode = review  (one of: mixed, review)"
    with pytest.raises(SystemExit, match="mode: must be one of mixed, review"):
        go(build(s), "set", "mode", "x")
