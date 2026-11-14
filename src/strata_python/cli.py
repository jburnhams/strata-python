"""The CLI commands every app has, added to the app's own argparse subparsers.

    sub = parser.add_subparsers(dest="cmd", required=True)
    add_doctor(sub, "myapp", __version__, ("fastapi", ("opencv", "cv2"), Module("ultralytics", optional=True)),
               checks=[Check("ffmpeg", program("ffmpeg"))])
    add_set(sub, SETTINGS)
    add_serve(sub, lambda host, port: web.serve(create_app(), ["--host", host, "--port", str(port)]), default_port=8360)
    args = parser.parse_args(argv); return args.fn(args)

Each command sets `fn` on its parser; the handler returns the exit code."""

import argparse
import importlib
import shutil
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal

from strata_python.settings import Settings

Sub = Any  # argparse's subparsers object is a private generic type; only add_parser is used

Status = Literal["ok", "missing", "slow", "broken"]
"""A doctor row: `ok`; `missing` (not installed or not found); `slow` (works, but not as it should: a CPU fallback,
cuDNN off), which never fails the run; `broken` (there, but does not work)."""


@dataclass(frozen=True)
class Module:
    """A module `doctor` imports: `label` is what a person recognises ("rawpy (DNG decoding)"), `module` the import
    name (default: the label). An `optional` one that is missing is shown with the hint but does not fail the run."""

    label: str
    module: str = ""
    optional: bool = False

    @property
    def name(self) -> str:
        return self.module or self.label


@dataclass(frozen=True)
class Check:
    """What an import cannot tell (a program on the PATH, whether the GPU runs a kernel): `fn` returns a status and a
    line of text. `missing` or `broken` fails the run unless the check is `optional`; an exception counts as
    `broken`, with the exception as its text."""

    label: str
    fn: Callable[[], tuple[Status, str]]
    optional: bool = False


def program(name: str, hint: str = "") -> Callable[[], tuple[Status, str]]:
    """A check function for a program on the PATH (`ffmpeg`, `exiftool`): `ok` with its path, else `missing`."""

    def check() -> tuple[Status, str]:
        found = shutil.which(name)
        return ("ok", found) if found else ("missing", hint or "not on the PATH")

    return check


def _row(status: Status, label: str, text: str, fails: bool) -> str:
    word = status.upper() if fails else status  # failing rows shout, so they are found in a long list
    return f"  {word:<9}{label}{': ' if text and status != 'ok' else ' '}{text}".rstrip()


def doctor(
    name: str,
    version: str,
    imports: Iterable[str | tuple[str, str] | Module],
    *,
    checks: Iterable[Check] = (),
    hint: str = "uv sync",
) -> int:
    """Print the app and Python versions, then one row per module and per check, in the same columns for every app.
    Returns 1 when a required module is missing or a required check is missing or broken, else 0."""
    ok = True
    print(f"{name} {version}, python {sys.version.split()[0]}")
    for item in imports:
        mod = Module(item) if isinstance(item, str) else Module(*item) if isinstance(item, tuple) else item
        try:
            m = importlib.import_module(mod.name)
            print(_row("ok", mod.label, str(getattr(m, "__version__", "")), False))
        except ImportError:
            ok = ok and mod.optional
            print(_row("missing", mod.label, hint, not mod.optional))
    for check in checks:
        try:
            status, text = check.fn()
        except Exception as e:  # a check that crashes is a broken row, not a crashed doctor
            status, text = "broken", f"{type(e).__name__}: {e}"
        fails = status in ("missing", "broken") and not check.optional
        ok = ok and not fails
        print(_row(status, check.label, text, fails))
    return 0 if ok else 1


def add_doctor(
    sub: Sub,
    name: str,
    version: str,
    imports: Iterable[str | tuple[str, str] | Module],
    *,
    checks: Iterable[Check] = (),
    hint: str = "uv sync",
) -> None:
    """`doctor`: the app and Python versions, whether each module in `imports` can be imported (a name, a
    `(label, module)` pair or a `Module`), then each of the app's `checks`; `hint` is what a missing module says to
    run. Exit 1 if a required module or check fails (see `doctor`)."""
    mods, chks = tuple(imports), tuple(checks)

    def cmd_doctor(_args: argparse.Namespace) -> int:
        return doctor(name, version, mods, checks=chks, hint=hint)

    sub.add_parser("doctor", help="check the environment").set_defaults(fn=cmd_doctor)


def add_set(sub: Sub, settings: Settings) -> None:
    """`set KEY [VALUE]`: show a setting, change it, or reset it with the value `default`. A bad key or value exits with the message."""

    def cmd_set(args: argparse.Namespace) -> int:
        if args.key not in settings.keys:
            sys.exit(f"unknown setting {args.key!r} (known: {', '.join(settings.keys)})")
        if args.value is not None:
            try:
                settings.save({args.key: None if args.value == "default" else args.value})
            except ValueError as e:
                sys.exit(str(e))
        print(f"{args.key} = {settings.get(args.key)}")
        return 0

    p = sub.add_parser("set", help=f'show or change a setting ({", ".join(settings.keys)}); value "default" resets it')
    p.add_argument("key")
    p.add_argument("value", nargs="?")
    p.set_defaults(fn=cmd_set)


def add_serve(sub: Sub, run: Callable[[str, int], None], *, default_port: int) -> None:
    """`serve [--host] [--port]`: call `run(host, port)`."""

    def cmd_serve(args: argparse.Namespace) -> int:
        run(args.host, args.port)
        return 0

    p = sub.add_parser("serve", help="start the web server")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=default_port)
    p.set_defaults(fn=cmd_serve)
