"""Run the real gate on a tiny real project, as a person would: it passes on clean code and fails on a lint error and on a failing test."""

import os
import subprocess
import sys


def gate(tiny_project, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("VIRTUAL_ENV")}
    return subprocess.run(
        [sys.executable, "-m", "strata_python.check", *args], cwd=tiny_project, capture_output=True, text=True, env=env
    )


def test_passes_on_clean_code(tiny_project):
    r = gate(tiny_project)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "all checks passed" in r.stdout


def test_fails_on_a_lint_error(tiny_project):
    (tiny_project / "src" / "tiny.py").write_text("import os\n\n\ndef add(a: int, b: int) -> int:\n    return a + b\n")
    r = gate(tiny_project, "--fast")
    assert r.returncode == 1
    assert "FAILED: ruff lint" in r.stdout


def test_fix_applies_safe_fixes(tiny_project):
    (tiny_project / "src" / "tiny.py").write_text("import os\n\n\ndef add(a: int, b: int) -> int:\n    return a + b\n")
    r = gate(tiny_project, "--fix", "--fast")
    assert r.returncode == 0, r.stdout
    assert "import os" not in (tiny_project / "src" / "tiny.py").read_text()


def test_fails_on_a_failing_test(tiny_project):
    (tiny_project / "tests" / "unit" / "test_tiny.py").write_text("def test_bad():\n    assert False\n")
    r = gate(tiny_project)
    assert r.returncode == 1
    assert "unit tests + coverage" in r.stdout.split("FAILED:")[1]
