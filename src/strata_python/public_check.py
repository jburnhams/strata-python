"""`strata-public-check`: refuse to publish personal or machine-specific details.

The strata repos are public and their packages go to PyPI, where an upload can be yanked but never removed, so the
release workflow scans the built sdist and wheel (and CI scans the tree) before anything leaves. The built-in patterns
catch what is personal in any repo: email addresses, private IPv4 addresses and home-folder paths. Names that are
personal to one owner (a person, a host name, a port map) cannot be listed here without publishing them, so they come
from the `STRATA_PUBLIC_DENY` environment variable (one case-insensitive regex per line), which CI fills from a secret.

False positives left after that are silenced in the repo, where a reviewer sees them:

- `[tool.strata-public-check] exclude = ["web/package-lock.json"]` in pyproject.toml (or `--exclude`): glob patterns
  matched against each file's path in the folder or archive, or any trailing part of it (so an sdist's
  `pkg-1.0/web/package-lock.json` matches too). An excluded file skips the built-in patterns only.
- `strata-public-check: ignore` anywhere on a line skips the built-in patterns on that line. Never the owner's
  `STRATA_PUBLIC_DENY` patterns: a name the owner listed is never one to publish.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import sys
import tarfile
import tomllib
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, cast

DENY_ENV = "STRATA_PUBLIC_DENY"
TABLE = "strata-public-check"
IGNORE = "strata-public-check: ignore"

# Placeholders the docs use on purpose (AGENTS.md: examples use /home/ada/..., myapp).
PLACEHOLDER_USERS = {"ada", "user", "me", "you", "name", "myapp", "runner"}

BUILT_IN: dict[str, re.Pattern[str]] = {
    # Only the two automated addresses git trailers use are allowed: GitHub's per-user noreply domain and Claude's
    # co-author address. A whole domain is never exempt (review finding: a personal address there would pass).
    "email address": re.compile(
        r"\b(?!noreply@anthropic\.com\b)[A-Za-z0-9._%+-]+@(?!users\.noreply\.github\.com\b)[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"
    ),
    # A whole dotted quad and nothing more: a three-part version (`10.4.2` in package.json, issue #13) or a longer
    # dotted number is not an address. The lookarounds stop a match inside a longer run of digits and dots.
    "private IPv4 address": re.compile(
        r"(?<![\w.])(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})(?!\.?\d)"
    ),
    "home folder path": re.compile(r"(?:/home/|/Users/|[A-Za-z]:\\+Users\\+)(?P<user>[A-Za-z0-9._-]+)"),
}

# Text files only: a binary (an image, a compiled extension) is skipped rather than decoded into noise.
TEXT_SUFFIXES = {".py", ".pyi", ".md", ".txt", ".toml", ".cfg", ".ini", ".json", ".yml", ".yaml", ".typed", ".sh", ""}
SKIP_PARTS = {
    "__pycache__",
    "node_modules",
    "dist",
    "build",
}  # and hidden folders (.git, .venv, caches), but not .github


@dataclass(frozen=True)
class Finding:
    where: str  # path (inside the archive, when scanning one)
    line: int
    kind: str
    text: str

    def __str__(self) -> str:
        return f"{self.where}:{self.line}: {self.kind}: {self.text}"


def deny_patterns(env: str | None = None) -> list[re.Pattern[str]]:
    """The owner's own patterns from `STRATA_PUBLIC_DENY`, one regex per line; blank lines are ignored."""
    raw = os.environ.get(DENY_ENV, "") if env is None else env
    return [re.compile(line.strip(), re.IGNORECASE) for line in raw.splitlines() if line.strip()]


def scan_text(
    where: str, text: str, extra: Iterable[re.Pattern[str]] = (), *, built_in: bool = True
) -> Iterator[Finding]:
    """Findings in one file's text. `built_in=False` (an excluded file) checks only the owner's patterns."""
    extra = list(extra)
    for number, line in enumerate(text.splitlines(), 1):
        for kind, pattern in BUILT_IN.items() if built_in and IGNORE not in line else ():
            for match in pattern.finditer(line):
                if kind == "home folder path" and match.group("user").lower() in PLACEHOLDER_USERS:
                    continue
                yield Finding(where, number, kind, match.group(0))
        for pattern in extra:
            # The match itself is not printed: the log of a public repo's CI is public too.
            if pattern.search(line):
                yield Finding(where, number, f"matches {DENY_ENV} pattern", "(hidden)")


def excluded(name: str, patterns: Iterable[str]) -> bool:
    """Whether a path (posix, relative to its folder or archive) matches an exclude pattern, whole or at its end."""
    return any(fnmatchcase(name, p) or fnmatchcase(name, "*/" + p) for p in patterns)


def config_excludes(root: Path | None = None) -> list[str]:
    """`exclude` from `[tool.strata-public-check]` in root's (default: the current folder's) pyproject.toml."""
    f = (root or Path()) / "pyproject.toml"
    if not f.is_file():
        return []
    tool = cast("dict[str, Any]", tomllib.loads(f.read_text(encoding="utf-8")).get("tool", {}))
    return [str(p) for p in cast("dict[str, Any]", tool.get(TABLE, {})).get("exclude", [])]


def _is_text(name: str) -> bool:
    path = Path(name)
    hidden = any(part.startswith(".") and part != ".github" for part in path.parts[:-1])
    return path.suffix in TEXT_SUFFIXES and not hidden and not (set(path.parts) & SKIP_PARTS)


def _members(path: Path) -> Iterator[tuple[str, bytes]]:
    if path.is_dir():
        for file in sorted(path.rglob("*")):
            relative = file.relative_to(path).as_posix()
            if file.is_file() and _is_text(relative):
                yield relative, file.read_bytes()
    elif path.name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(path) as archive:
            for member in archive.getmembers():
                handle = archive.extractfile(member) if member.isfile() and _is_text(member.name) else None
                if handle is not None:
                    yield f"{path.name}/{member.name}", handle.read()
    elif path.suffix in {".whl", ".zip"}:
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if not name.endswith("/") and _is_text(name):
                    yield f"{path.name}/{name}", archive.read(name)
    else:
        yield path.as_posix(), path.read_bytes()


def scan(paths: Iterable[Path], extra: Iterable[re.Pattern[str]] = (), exclude: Iterable[str] = ()) -> list[Finding]:
    """Every finding in the given folders, files, sdists (.tar.gz) and wheels (.whl), less the excluded files."""
    extra, exclude = list(extra), list(exclude)
    findings: list[Finding] = []
    for path in paths:
        for where, data in _members(path):
            skip = excluded(where, exclude)
            if skip and not extra:
                continue
            text = io.TextIOWrapper(io.BytesIO(data), encoding="utf-8", errors="replace").read()
            # an excluded file is still checked against the owner's patterns: an exclude is for false positives of the
            # built-in ones, and a name the owner listed is never one to publish (review finding)
            findings.extend(scan_text(where, text, extra, built_in=not skip))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="strata-public-check", description="Refuse to publish personal or machine-specific details."
    )
    parser.add_argument("paths", nargs="*", type=Path, default=[Path()], help="folders, files, sdists or wheels")
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help=f"skip matching files, as well as [tool.{TABLE}] exclude in ./pyproject.toml",
    )
    args = parser.parse_args(argv)
    findings = scan(args.paths, deny_patterns(), [*config_excludes(), *args.exclude])
    for finding in findings:
        print(finding, file=sys.stderr)
    if findings:
        print(f"{len(findings)} personal or machine-specific detail(s); see AGENTS.md (rule 4)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
