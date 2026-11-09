"""`strata-check`: the one gate for an app, run before every commit by hand, by the git hooks and by CI (so they cannot disagree).

    uv run strata-check            # lockfile, ruff lint, ruff format, pyright, ty, then the unit tests with coverage
    uv run strata-check --fast     # skip the tests (for a commit hook)
    uv run strata-check --fix      # apply safe ruff fixes and formatting first

Two type checkers gate: pyright (strict) and ty; when ty is stable and as strict as pyright's strict mode, drop the pyright step. `--fix` never uses ruff's unsafe fixes: they
rewrite code in ways tests have to catch (see the README). Run it from the project root. Optional configuration in pyproject.toml:

    [tool.strata-check]
    paths = ["src", "tests", "scripts"]   # what ruff checks (only folders that exist are passed); ty checks the first one
    tests = "tests/unit"                  # what the coverage run covers
"""

import os
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from typing import Any

DEFAULT_PATHS = ("src", "tests", "scripts")
DEFAULT_TESTS = "tests/unit"


def config(root: str = ".") -> dict[str, Any]:
    try:
        with open(os.path.join(root, "pyproject.toml"), "rb") as f:
            return tomllib.load(f).get("tool", {}).get("strata-check", {})
    except FileNotFoundError:
        return {}


def steps(root: str = ".", *, tests: bool = True) -> list[tuple[str, list[str]]]:
    cfg = config(root)
    paths = [p for p in cfg.get("paths", DEFAULT_PATHS) if os.path.isdir(os.path.join(root, p))]
    out: list[tuple[str, list[str]]] = []
    if os.path.exists(os.path.join(root, "uv.lock")):
        out.append(("lockfile is current", ["uv", "lock", "--check"]))
    out += [
        ("ruff lint", ["ruff", "check", *paths]),
        ("ruff format", ["ruff", "format", "--check", *paths]),
        ("pyright (strict)", ["pyright"]),
        ("ty", ["ty", "check", paths[0] if paths else "."]),
    ]
    if tests:
        out.append(("unit tests + coverage", ["pytest", cfg.get("tests", DEFAULT_TESTS), "--cov", "-q"]))
    return out


def run(label: str, cmd: Sequence[str]) -> bool:
    print(f"\n==> {label}: {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd).returncode == 0  # noqa: S603  (fixed commands from this file)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--fix" in args:
        paths = [p for p in config().get("paths", DEFAULT_PATHS) if os.path.isdir(p)]
        run("ruff fix (safe fixes only)", ["ruff", "check", "--fix", *paths])
        run("ruff format", ["ruff", "format", *paths])
    failed = [label for label, cmd in steps(tests="--fast" not in args) if not run(label, cmd)]
    print("\n" + ("FAILED: " + ", ".join(failed) if failed else "all checks passed"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
