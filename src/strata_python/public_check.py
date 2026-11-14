"""`strata-public-check`: refuse to publish personal or machine-specific details.

The strata repos are public and their packages go to PyPI, where an upload can be yanked but never removed, so the
release workflow scans the built sdist and wheel (and CI scans the tree) before anything leaves. The built-in patterns
catch what is personal in any repo: email addresses, private IPv4 addresses and home-folder paths. Names that are
personal to one owner (a person, a host name, a port map) cannot be listed here without publishing them, so they come
from the `STRATA_PUBLIC_DENY` environment variable (one case-insensitive regex per line), which CI fills from a secret.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import sys
import tarfile
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

DENY_ENV = "STRATA_PUBLIC_DENY"

# Placeholders the docs use on purpose (AGENTS.md: examples use /home/ada/..., myapp).
PLACEHOLDER_USERS = {"ada", "user", "me", "you", "name", "myapp", "runner"}

BUILT_IN: dict[str, re.Pattern[str]] = {
    # Only the two automated addresses git trailers use are allowed: GitHub's per-user noreply domain and Claude's
    # co-author address. A whole domain is never exempt (review finding: a personal address there would pass).
    "email address": re.compile(
        r"\b(?!noreply@anthropic\.com\b)[A-Za-z0-9._%+-]+@(?!users\.noreply\.github\.com\b)[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"
    ),
    "private IPv4 address": re.compile(r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))(?:\.\d{1,3}){2,3}\b"),
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


def scan_text(where: str, text: str, extra: Iterable[re.Pattern[str]] = ()) -> Iterator[Finding]:
    extra = list(extra)
    for number, line in enumerate(text.splitlines(), 1):
        for kind, pattern in BUILT_IN.items():
            for match in pattern.finditer(line):
                if kind == "home folder path" and match.group("user").lower() in PLACEHOLDER_USERS:
                    continue
                yield Finding(where, number, kind, match.group(0))
        for pattern in extra:
            # The match itself is not printed: the log of a public repo's CI is public too.
            if pattern.search(line):
                yield Finding(where, number, f"matches {DENY_ENV} pattern", "(hidden)")


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


def scan(paths: Iterable[Path], extra: Iterable[re.Pattern[str]] = ()) -> list[Finding]:
    """Every finding in the given folders, files, sdists (.tar.gz) and wheels (.whl)."""
    extra = list(extra)
    findings: list[Finding] = []
    for path in paths:
        for where, data in _members(path):
            text = io.TextIOWrapper(io.BytesIO(data), encoding="utf-8", errors="replace").read()
            findings.extend(scan_text(where, text, extra))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="strata-public-check", description="Refuse to publish personal or machine-specific details."
    )
    parser.add_argument("paths", nargs="*", type=Path, default=[Path()], help="folders, files, sdists or wheels")
    args = parser.parse_args(argv)
    findings = scan(args.paths, deny_patterns())
    for finding in findings:
        print(finding, file=sys.stderr)
    if findings:
        print(f"{len(findings)} personal or machine-specific detail(s); see AGENTS.md (rule 4)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
