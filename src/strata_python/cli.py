"""The CLI commands every app has, added to the app's own argparse subparsers.

    sub = parser.add_subparsers(dest="cmd", required=True)
    add_doctor(sub, "myapp", __version__, ("fastapi", "uvicorn"))
    add_set(sub, SETTINGS)
    add_serve(sub, lambda host, port: web.serve(create_app(), ["--host", host, "--port", str(port)]), default_port=8360)
    args = parser.parse_args(argv); return args.fn(args)

Each command sets `fn` on its parser; the handler returns the exit code."""

import argparse
import importlib
import sys
from collections.abc import Callable, Iterable
from typing import Any

from strata_python.settings import Settings

Sub = Any  # argparse's subparsers object is a private generic type; only add_parser is used


def add_doctor(sub: Sub, name: str, version: str, imports: Iterable[str]) -> None:
    """`doctor`: print the app and Python versions and whether each module in `imports` can be imported (exit 1 if any cannot)."""
    mods = tuple(imports)

    def doctor(_args: argparse.Namespace) -> int:
        ok = True
        print(f"{name} {version}, python {sys.version.split()[0]}")
        for mod in mods:
            try:
                m = importlib.import_module(mod)
                print(f"  ok       {mod} {getattr(m, '__version__', '')}".rstrip())
            except ImportError:
                ok = False
                print(f"  MISSING  {mod}: uv sync")
        return 0 if ok else 1

    sub.add_parser("doctor", help="check the environment").set_defaults(fn=doctor)


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
