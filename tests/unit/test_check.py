import sys

from strata_python import check


def test_steps_follow_the_project_layout(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    names = [n for n, _ in check.steps(str(tmp_path))]
    assert names == ["ruff lint", "ruff format", "pyright (strict)", "ty", "unit tests + coverage"]
    (tmp_path / "uv.lock").write_text("")
    cmds = dict(check.steps(str(tmp_path), tests=False))
    assert next(iter(cmds)) == "lockfile is current"
    assert cmds["ruff lint"] == ["ruff", "check", "src", "tests"]  # scripts/ does not exist, so it is not passed
    assert cmds["ty"] == ["ty", "check", "src"]
    assert "unit tests + coverage" not in cmds


def test_configuration_overrides_paths_and_tests(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "pyproject.toml").write_text('[tool.strata-check]\npaths = ["app", "missing"]\ntests = "t/unit"\n')
    cmds = dict(check.steps(str(tmp_path)))
    assert cmds["ruff format"] == ["ruff", "format", "--check", "app"]
    assert cmds["unit tests + coverage"][:2] == ["pytest", "t/unit"]


def test_no_pyproject_means_defaults(tmp_path):
    assert check.config(str(tmp_path)) == {}


def test_run_reports_success_and_failure(capsys):
    assert check.run("fine", [sys.executable, "-c", "pass"])
    assert not check.run("broken", [sys.executable, "-c", "raise SystemExit(3)"])
    assert "==> fine" in capsys.readouterr().out


def test_main_runs_the_real_tools_on_a_real_project(tiny_project, monkeypatch, capsys):
    monkeypatch.chdir(tiny_project)
    assert check.main(["--fast"]) == 0
    assert "all checks passed" in capsys.readouterr().out


def test_main_reports_what_failed(tiny_project, monkeypatch, capsys):
    (tiny_project / "src" / "tiny.py").write_text("import os\n\n\ndef add(a: int, b: int) -> int:\n    return a + b\n")
    monkeypatch.chdir(tiny_project)
    assert check.main(["--fast"]) == 1
    assert "FAILED: ruff lint" in capsys.readouterr().out
    assert check.main(["--fix", "--fast"]) == 0
