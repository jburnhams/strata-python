"""Load the library's own pytest plugins (see addopts in pyproject.toml for why they are not loaded through their entry points here)."""

import textwrap

import pytest

pytest_plugins = ["strata_python.pytest_plugin", "strata_python.db.pytest_plugin"]

PYPROJECT = textwrap.dedent(
    """
    [project]
    name = "tiny"
    version = "0"
    requires-python = ">=3.11"
    [tool.ruff]
    line-length = 120
    [tool.pyright]
    include = ["src"]
    typeCheckingMode = "strict"
    [tool.pytest.ini_options]
    addopts = "-p no:randomly"
    [tool.coverage.run]
    source = ["src"]
    """
)


@pytest.fixture
def tiny_project(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)
    (tmp_path / "src" / "tiny.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n")
    (tmp_path / "tests" / "unit" / "test_tiny.py").write_text(
        'import sys\n\nsys.path.insert(0, "src")\nfrom tiny import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n'
    )
    return tmp_path
