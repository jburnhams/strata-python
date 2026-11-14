"""User settings for an app: one JSON file, `~/.<app>/settings.json`, with environment overrides.

An app declares its settings once (`Setting`: key, default, optional normaliser) and gets the file handling, the override order and the validation:

    SETTINGS = Settings("myapp", [Setting("data_dir", default=lambda: "/srv/data", normalise=absolute_path),
                                  Setting("greeting", default="Hello", normalise=non_empty),
                                  Setting("workers", default="2", normalise=whole_between(1, 16)),
                                  ListSetting("source_roots", item=absolute_path)])
    SETTINGS.get("greeting")          # MYAPP_GREETING, else the file, else the default
    SETTINGS.get_int("workers")       # typed getters: get_int, get_float, get_bool, get_list
    SETTINGS.save({"greeting": "Hi"}) # validated, written atomically; None resets to the default

Order of precedence: environment variable (`<APP>_<KEY>`, upper-cased, or the name given), then the store, then the default. The folder holding the file is `~/.<app>`, moved
with `<APP>_HOME`. A stored or environment value that fails its normaliser is ignored (the default applies) rather than crashing the app at start; only `save` raises.
Values are canonical strings (a normaliser returns the one spelling it accepts), and lists of them: the file is edited by hand as well as by the app.

Where the values live is a `SettingsStore`: the JSON file by default; `Settings.bind(store)` moves an app onto another one at run time (its database's `settings` table,
`strata_python.db.settings.TableStore`), copying the file's values in once. The CLI and tests without a database keep the file."""

import datetime
import json
import math
import os
import re
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

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


def number_between(lo: float, hi: float) -> Callable[[str], str]:
    """Normaliser: a finite number from `lo` to `hi` (GB of memory, a budget in dollars), written the shortest way (`"8"`, `"8.5"`)."""

    def normalise(value: str) -> str:
        try:
            x = float(value.strip())
        except ValueError:
            raise ValueError("must be a number") from None
        if not math.isfinite(x) or not lo <= x <= hi:
            raise ValueError(f"must be a number from {_num(lo)} to {_num(hi)}")
        return _num(x)

    return normalise


def whole_between(lo: int, hi: int) -> Callable[[str], str]:
    """Normaliser: a whole number from `lo` to `hi` (job slots, a port)."""

    def normalise(value: str) -> str:
        try:
            x = int(value.strip())
        except ValueError:
            raise ValueError("must be a whole number") from None
        if not lo <= x <= hi:
            raise ValueError(f"must be a whole number from {lo} to {hi}")
        return str(x)

    return normalise


_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def iso_date(allow_blank: bool = True) -> Callable[[str], str]:
    """Normaliser: a `YYYY-MM-DD` date; with `allow_blank`, `""` is a value too (no date: the setting is cleared but not reset)."""

    def normalise(value: str) -> str:
        v = value.strip()
        if not v and allow_blank:
            return ""
        try:
            if not _DATE.fullmatch(v):
                raise ValueError
            return datetime.date.fromisoformat(v).isoformat()
        except ValueError:
            raise ValueError("must be a date, YYYY-MM-DD") from None

    return normalise


_TRUE, _FALSE = {"1", "true", "yes", "on"}, {"0", "false", "no", "off"}


def boolean(value: str) -> str:
    """Normaliser: `true` or `false` (also accepts 1/0, yes/no, on/off, in any case)."""
    v = value.strip().lower()
    if v in _TRUE:
        return "true"
    if v in _FALSE:
        return "false"
    raise ValueError("must be true or false")


def _num(x: float) -> str:
    return str(int(x)) if float(x).is_integer() and abs(x) < 1e15 else repr(float(x))


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

    def parse(self, raw: object) -> str:
        """A stored, environment or saved value as its canonical string; ValueError when it is not acceptable."""
        if not isinstance(raw, str):
            raise ValueError("must be text")  # str(5) would pass a hand-edited number off as a deliberate string
        return self.normalise(raw)


def _no_list() -> list[str]:
    return []


@dataclass(frozen=True)
class ListSetting:
    """A setting that is a list of strings (folders to scan, names to enable). The file holds a JSON list (a string there is split, so hand edits work); the
    environment value is split on `sep` (`os.pathsep` by default, for folders; give `sep=","` for names). `item` normalises each entry, and blank entries are
    dropped. `get_list` returns the list and `all()` (so GET /api/settings) the list as JSON; `get` and `strata set` show it joined by `sep`, and `save` takes a
    list or a joined string."""

    key: str
    default: Sequence[str] | Callable[[], Sequence[str]] = _no_list
    item: Callable[[str], str] = _identity
    sep: str = os.pathsep
    env: str | None = None

    def default_value(self) -> list[str]:
        d = self.default
        return list(cast("Sequence[str]", d) if isinstance(d, Sequence) else d())

    def parse(self, raw: object) -> list[str]:
        if isinstance(raw, str):
            items: list[object] = list(raw.split(self.sep))
        elif isinstance(raw, list | tuple):
            items = list(cast("Sequence[object]", raw))
        else:
            raise ValueError("must be a list")
        out: list[str] = []
        for i in items:
            if not isinstance(i, str):
                raise ValueError("must be a list of text")
            if i.strip():
                out.append(self.item(i.strip()))
        return out


AnySetting = Setting | ListSetting
Value = str | list[str]

IMPORTED = "_imported_from_file"  # the marker bind() leaves in a store it copied the file into; not a setting


class SettingsStore(Protocol):
    """Where the values live. `load` returns every stored value; `update` applies changes (already validated; None removes a key) as one atomic step, so two
    saves of different keys never lose one, and returns what is stored afterwards."""

    def load(self) -> dict[str, object]: ...

    def update(self, changes: Mapping[str, object]) -> dict[str, object]: ...


class JsonFileStore:
    """The default store: one JSON object in a file, written atomically under a lock (across threads and processes). `path` is called each time, so a
    variable that moves the folder (`<APP>_HOME`) takes effect without a restart."""

    def __init__(self, path: str | Callable[[], str]) -> None:
        self._path = path
        self._thread_lock = threading.Lock()  # file_lock alone does not exclude threads on every platform

    def path(self) -> str:
        p = self._path
        return p if isinstance(p, str) else p()

    def load(self) -> dict[str, object]:
        """The file's contents ({} when it is missing, unreadable or not a JSON object)."""
        try:
            with open(self.path(), encoding="utf8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return {}
        return cast("dict[str, object]", d) if isinstance(d, dict) else {}

    def update(self, changes: Mapping[str, object]) -> dict[str, object]:
        path = self.path()
        folder = os.path.dirname(path)
        os.makedirs(folder, exist_ok=True)
        # The whole read-merge-write is one critical section, across threads (the server's PUT handlers run in a pool) and processes (the CLI `set` beside a running server);
        # otherwise two saves of different keys lose one update.
        with self._thread_lock, open(path + ".lock", "a+") as lock, file_lock(lock):
            d = self.load()
            for k, v in changes.items():
                if v is None:
                    d.pop(k, None)
                else:
                    d[k] = v
            fd, tmp = tempfile.mkstemp(dir=folder, prefix="settings.", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf8") as f:
                json.dump(d, f, indent=1)
            os.replace(tmp, path)  # atomic: a reader never sees half a file
        return d


class Settings:
    def __init__(
        self, app: str, settings: Sequence[AnySetting], *, prefix: str | None = None, store: SettingsStore | None = None
    ) -> None:
        """`prefix` names the environment variables (`<PREFIX>_HOME`, `<PREFIX>_<KEY>`); default the upper-cased app name with `-` as `_`.
        Give it when an app's variables already have a short form that deployed services use. `store` is where values live (default: the JSON file)."""
        self.app = app
        self.prefix = prefix or app.upper().replace("-", "_")
        self._by_key: dict[str, AnySetting] = {s.key: s for s in settings}
        self.file = JsonFileStore(self.path)
        self.store: SettingsStore = store or self.file

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

    def bind(self, store: SettingsStore, *, import_file: bool = True) -> None:
        """Keep the values in `store` from now on. The first time a store is bound (it has no import marker), the file's valid values are copied into it,
        with the marker, so moving an app onto its database keeps what its people set. Two processes binding a new store at once both copy the same values."""
        if import_file and IMPORTED not in store.load():
            values: dict[str, object] = {}
            for k, raw in self.file.load().items():
                s = self._by_key.get(k)
                if s is not None:
                    try:
                        values[k] = s.parse(raw)
                    except ValueError:
                        continue  # a bad value would be ignored by get() too; it is not carried over
            store.update({**values, IMPORTED: self.file.path()})
        self.store = store

    def load(self) -> dict[str, Any]:
        """The raw stored values (for the file: its contents; {} when it is missing, unreadable or not a JSON object)."""
        return self.store.load()

    def save(self, changes: Mapping[str, object]) -> dict[str, Any]:
        """Merge `changes` into the store and return what is stored. Unknown keys raise KeyError, a value its normaliser refuses raises ValueError
        (naming the key), and None removes the setting. All changes are checked before any is written. A list setting takes a list or a joined string."""
        for k in changes:
            if k not in self._by_key:
                raise KeyError(f"unknown setting {k!r}")
        clean: dict[str, object] = {}
        for k, v in changes.items():
            s = self._by_key[k]
            try:
                clean[k] = (
                    None if v is None else s.parse(v if isinstance(s, ListSetting) or isinstance(v, str) else str(v))
                )
            except ValueError as e:
                raise ValueError(f"{k}: {e}") from e
        return self.store.update(clean)

    def value(self, key: str) -> Value:
        """The setting's value: a string, or a list of strings for a `ListSetting`."""
        s = self._by_key[key]
        for raw in (os.environ.get(self.env_name(key)), self.load().get(key)):
            if raw is not None:  # "" is a value; the normaliser decides whether it is acceptable
                try:
                    return s.parse(raw)
                except ValueError:
                    continue
        return s.parse(s.default_value())

    def get(self, key: str) -> str:
        """The value as a string (a list setting joined by its separator)."""
        v = self.value(key)
        s = self._by_key[key]
        return s.sep.join(v) if isinstance(s, ListSetting) and isinstance(v, list) else cast("str", v)

    def get_list(self, key: str) -> list[str]:
        s = self._by_key[key]
        if not isinstance(s, ListSetting):
            raise TypeError(f"{key!r} is not a ListSetting")
        return cast("list[str]", self.value(key))

    def get_int(self, key: str) -> int:
        return int(self.get(key))

    def get_float(self, key: str) -> float:
        return float(self.get(key))

    def get_bool(self, key: str) -> bool:
        return boolean(self.get(key)) == "true"

    def all(self) -> dict[str, Value]:
        return {k: self.value(k) for k in self._by_key}

    def folder(self, key: str) -> str:
        """The setting `key` as a folder that exists (created when asked for)."""
        p = self.get(key)
        os.makedirs(p, exist_ok=True)
        return p
