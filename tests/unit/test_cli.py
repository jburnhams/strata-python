import argparse

import pytest

from strata_python import cli
from strata_python.settings import Setting, Settings


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
