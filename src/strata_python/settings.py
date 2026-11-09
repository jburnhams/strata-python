"""User settings for an app: one JSON file, `~/.<app>/settings.json`, with environment overrides.

An app declares its settings once (`Setting`: key, default, optional normaliser) and gets the file handling, the override order and the validation:

    SETTINGS = Settings("myapp", [Setting("data_dir", default=lambda: "/srv/data", normalise=absolute_path),
                                  Setting("greeting", default="Hello", normalise=non_empty)])
    SETTINGS.get("greeting")          # MYAPP_GREETING, else the file, else the default
    SETTINGS.save({"greeting": "Hi"}) # validated, written atomically; None resets to the default

Order of precedence: environment variable (`<APP>_<KEY>`, upper-cased, or the name given), then the file, then the default. The folder holding the file is `~/.<app>`, moved
with `<APP>_HOME`. A stored or environment value that fails its normaliser is ignored (the default applies) rather than crashing the app at start; only `save` raises.
Values are strings: the file is edited by hand as well as by the app."""

import json
import os
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from strata_python.oslib import file_lock


def non_empty(value: str) -> str:
    """Normaliser: trim, and refuse a blank value."""
    v = value.strip()
    if not v:
        raise ValueError("must not be blank")
    return v


def absolute_path(value: str) -> str:
    """Normaliser: `~` expanded, and the path must then be absolute (a relative one would mean a different folder for every working directory)."""
    p = os.path.expanduser(value.strip())
    if not os.path.isabs(p):
        raise ValueError("must be an absolute path")
    return os.path.abspath(p)


def _identity(value: str) -> str:
    return value


@dataclass(frozen=True)
class Setting:
    key: str
    default: str | Callable[[], str]
    normalise: Callable[[str], str] = _identity
    env: str | None = None  # environment variable; default `<APP>_<KEY>`

    def default_value(self) -> str:
        d = self.default
        return d if isinstance(d, str) else d()


class Settings:
    def __init__(self, app: str, settings: Sequence[Setting], *, prefix: str | None = None) -> None:
        """`prefix` names the environment variables (`<PREFIX>_HOME`, `<PREFIX>_<KEY>`); default the upper-cased app name with `-` as `_`.
        Give it when an app's variables already have a short form that deployed services use."""
        self.app = app
        self.prefix = prefix or app.upper().replace("-", "_")
        self._by_key = {s.key: s for s in settings}
        self._thread_lock = threading.Lock()  # file_lock alone does not exclude threads on every platform

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(self._by_key)

    def home(self) -> str:
        """The folder that holds the settings file (`<APP>_HOME`, else `~/.<app>`)."""
        return os.environ.get(f"{self.prefix}_HOME") or os.path.join(os.path.expanduser("~"), f".{self.app}")

    def path(self) -> str:
        return os.path.join(self.home(), "settings.json")

    def env_name(self, key: str) -> str:
        return self._by_key[key].env or f"{self.prefix}_{key.upper()}"

    def load(self) -> dict[str, Any]:
        """The raw file contents ({} when it is missing, unreadable or not a JSON object)."""
        try:
            with open(self.path(), encoding="utf8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return {}
        return cast("dict[str, Any]", d) if isinstance(d, dict) else {}

    def save(self, changes: Mapping[str, object]) -> dict[str, Any]:
        """Merge `changes` into the file and return the saved contents. Unknown keys raise KeyError, a value its normaliser refuses raises ValueError
        (naming the key), and None removes the setting. All changes are checked before any is written."""
        for k in changes:
            if k not in self._by_key:
                raise KeyError(f"unknown setting {k!r}")
        os.makedirs(self.home(), exist_ok=True)
        # The whole read-merge-write is one critical section, across threads (the server's PUT handlers run in a pool) and processes (the CLI `set` beside a running server);
        # otherwise two saves of different keys lose one update.
        with self._thread_lock, open(self.path() + ".lock", "a+") as lock, file_lock(lock):
            d = self.load()
            for k, v in changes.items():
                if v is None:
                    d.pop(k, None)
                    continue
                try:
                    d[k] = self._by_key[k].normalise(str(v))
                except ValueError as e:
                    raise ValueError(f"{k}: {e}") from e
            fd, tmp = tempfile.mkstemp(dir=self.home(), prefix="settings.", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf8") as f:
                json.dump(d, f, indent=1)
            os.replace(tmp, self.path())  # atomic: a reader never sees half a file
        return d

    def get(self, key: str) -> str:
        s = self._by_key[key]
        stored = self.load().get(key)
        for raw in (os.environ.get(self.env_name(key)), stored if isinstance(stored, str) else None):
            if raw is not None:  # "" is a value; the normaliser decides whether it is acceptable
                try:
                    return s.normalise(raw)
                except ValueError:
                    continue
        return s.normalise(s.default_value())

    def all(self) -> dict[str, str]:
        return {k: self.get(k) for k in self._by_key}

    def folder(self, key: str) -> str:
        """The setting `key` as a folder that exists (created when asked for)."""
        p = self.get(key)
        os.makedirs(p, exist_ok=True)
        return p
