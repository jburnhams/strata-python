"""User settings for an app: one JSON file, `~/.<app>/settings.json`, with environment overrides.

An app declares its settings once (`Setting`: key, default, optional normaliser) and gets the file handling, the override order and the validation:

    SETTINGS = Settings("myapp", [Setting("data_dir", default=lambda: "/srv/data", normalise=absolute_path),
                                  Setting("greeting", default="Hello", normalise=non_empty),
                                  Setting("workers", default="2", normalise=whole_between(1, 16)),
                                  Setting("mode", default="mixed", choices=["mixed", "review", "new"]),
                                  ListSetting("source_roots", item=absolute_path)])
    SETTINGS.get("greeting")          # MYAPP_GREETING, else the file, else the default
    SETTINGS.get_int("workers")       # typed getters: get_int, get_float, get_bool, get_list
    SETTINGS.save({"greeting": "Hi"}) # validated, written atomically; None resets to the default

Order of precedence: environment variable (`<APP>_<KEY>`, upper-cased, or the name given), then the store, then the default. The folder holding the file is `~/.<app>`, moved
with `<APP>_HOME`. A stored or environment value that fails its normaliser is ignored (the default applies, and the log says so once) rather than crashing the app at start; only `save` raises.
Values are canonical strings (a normaliser returns the one spelling it accepts), and lists of them: the file is edited by hand as well as by the app.

`Settings.model()` describes them for `/api/openapi.json`: a setting with `choices` (or `normalise=one_of(...)`) is a
`Literal` of them, so the schema has an `enum` and the web app's generated type is the union. `Settings(...,
numbers=True)` also sends `whole_between` and `number_between` settings as JSON numbers with their range in the schema.
It is off by default because it changes what `GET /api/settings` sends, so an app turns it on with its web app.

Where the values live is a `SettingsStore`: the JSON file by default; `Settings.bind(store)` moves an app onto another one at run time (its database's `settings` table,
`strata_python.db.settings.TableStore`), copying the file's values in once. The CLI and tests without a database keep the file."""

import datetime
import json
import logging
import math
import os
import re
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast

from strata_python.oslib import file_lock

log = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class number_between:  # lower case: used as a normaliser function, and was one before it carried its range
    """Normaliser: a finite number from `lo` to `hi` (GB of memory, a budget in dollars), written the shortest way
    (`"8"`, `"8.5"`). `hi=None` has no ceiling; `lo_open=True` refuses `lo` itself, for a value that must be more than
    0 but has no natural floor (a cache size, a half-life: 0 would empty the cache or divide by zero, while tests use
    `1e-9`). `positive()` is that case. Its range is visible, so `Settings(numbers=True)` puts it in the schema
    (`exclusiveMinimum` when `lo_open`)."""

    lo: float
    hi: float | None = None
    lo_open: bool = False

    def __call__(self, value: str) -> str:
        try:
            x = float(value.strip())
        except ValueError:
            raise ValueError("must be a number") from None
        low_ok = x > self.lo if self.lo_open else x >= self.lo
        if not math.isfinite(x) or not low_ok or (self.hi is not None and x > self.hi):
            raise ValueError(self._range())
        return _num(x)

    def _range(self) -> str:
        lo, hi = _num(self.lo), None if self.hi is None else _num(self.hi)
        if self.lo_open:
            return f"must be more than {lo}" + ("" if hi is None else f" and at most {hi}")
        return f"must be a number from {lo} to {hi}" if hi is not None else f"must be a number of at least {lo}"


def positive(hi: float | None = None) -> number_between:
    """Normaliser: a number more than 0 (up to `hi`, when given): `number_between(0, hi, lo_open=True)`."""
    return number_between(0, hi, lo_open=True)


@dataclass(frozen=True)
class whole_between:  # lower case: as number_between
    """Normaliser: a whole number from `lo` to `hi` (job slots, a port); `hi=None` has no ceiling (a per-day limit).
    Its range is visible, so `Settings(numbers=True)` puts it in the schema."""

    lo: int
    hi: int | None = None

    def __call__(self, value: str) -> str:
        try:
            x = int(value.strip())
        except ValueError:
            raise ValueError("must be a whole number") from None
        if x < self.lo or (self.hi is not None and x > self.hi):
            raise ValueError(
                f"must be a whole number from {self.lo} to {self.hi}"
                if self.hi is not None
                else f"must be a whole number of at least {self.lo}"
            )
        return str(x)


@dataclass(frozen=True)
class one_of:  # lower case: as number_between
    """Normaliser: one of `choices`, matched without regard to case and surrounding space and returned as the choice
    is spelled; anything else is refused with the list. `Setting(choices=...)` is the short form. `Settings.model()`
    sees the choices and types the setting as a `Literal` of them."""

    choices: tuple[str, ...]

    def __init__(self, choices: Sequence[str]) -> None:
        if not choices:
            raise ValueError("one_of needs at least one choice")
        object.__setattr__(self, "choices", tuple(choices))

    def __call__(self, value: str) -> str:
        v = value.strip().casefold()
        for choice in self.choices:
            if choice.casefold() == v:
                return choice
        raise ValueError(f"must be one of {', '.join(self.choices)}")


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
    """One setting. `normalise` turns a value into its one spelling or raises ValueError; `choices` limits it to a
    fixed set (`one_of`, applied after `normalise`), which `set` shows and the API schema types as an enum."""

    key: str
    default: str | Callable[[], str]
    normalise: Callable[[str], str] = _identity
    env: str | None = None  # environment variable; default `<APP>_<KEY>`
    choices: Sequence[str] | None = None

    def __post_init__(self) -> None:
        if self.choices is not None:
            object.__setattr__(self, "choices", one_of(self.choices).choices)  # a tuple, and refuses an empty list

    def default_value(self) -> str:
        d = self.default
        return d if isinstance(d, str) else d()

    def parse(self, raw: object) -> str:
        """A stored, environment or saved value as its canonical string; ValueError when it is not acceptable."""
        v = self.normalise(self._text(raw))
        return v if self.choices is None else one_of(self.choices)(v)

    def _text(self, raw: object) -> str:
        # A JSON number or boolean is accepted where the normaliser reads one: an app's own settings file from before
        # it moved onto Settings usually stored them that way (issue #52), and so does a hand edit that writes 720 for
        # "720". Anywhere else str(5) would pass a hand-edited number off as a deliberate string, so it is refused.
        n = self.normalise
        if isinstance(raw, str):
            return raw
        if isinstance(raw, bool):
            if n is boolean:
                return "true" if raw else "false"
        elif isinstance(raw, int | float) and isinstance(n, number_between | whole_between):
            return (
                str(raw) if isinstance(raw, int) else _num(raw)
            )  # 720.0 reads as "720", so a whole number stored as a float still passes whole_between
        raise ValueError("must be text")

    def allowed(self) -> tuple[str, ...] | None:
        """The fixed set of values, from `choices` or a `one_of` normaliser, else None."""
        if self.choices is not None:
            return tuple(self.choices)
        n = self.normalise
        return n.choices if isinstance(n, one_of) else None


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
        self,
        app: str,
        settings: Sequence[AnySetting],
        *,
        prefix: str | None = None,
        store: SettingsStore | None = None,
        numbers: bool = False,
    ) -> None:
        """`prefix` names the environment variables (`<PREFIX>_HOME`, `<PREFIX>_<KEY>`); default the upper-cased app name with `-` as `_`.
        Give it when an app's variables already have a short form that deployed services use. `store` is where values live (default: the JSON file).
        `numbers=True` makes `model()` type `whole_between`/`number_between` settings as numbers (see the module notes)."""
        self.app = app
        self.numbers = numbers
        self.prefix = prefix or app.upper().replace("-", "_")
        self._by_key: dict[str, AnySetting] = {s.key: s for s in settings}
        self.file = JsonFileStore(self.path)
        self.store: SettingsStore = store or self.file
        self._ignored: set[tuple[str, str, str]] = set()

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
        env = self.env_name(key)
        for where, raw in ((env, os.environ.get(env)), ("the settings", self.load().get(key))):
            if raw is not None:  # "" is a value; the normaliser decides whether it is acceptable
                try:
                    return s.parse(raw)
                except ValueError as e:
                    self._warn_ignored(key, where, raw, e)
                    continue
        return s.parse(s.default_value())

    def _warn_ignored(self, key: str, where: str, raw: object, e: ValueError) -> None:
        # A value that is ignored is said once (per key, place and value), not on every read, so a migration that
        # drops values is seen in the log instead of the app quietly running on its defaults (issue #52).
        seen = (key, where, repr(raw))
        if seen not in self._ignored:
            self._ignored.add(seen)
            log.warning("setting %s: ignoring %r from %s (%s); using the next source", key, raw, where, e)

    def get(self, key: str) -> str:
        """The value as a string (a list setting joined by its separator)."""
        v = self.value(key)
        s = self._by_key[key]
        return s.sep.join(v) if isinstance(s, ListSetting) and isinstance(v, list) else cast("str", v)

    def allowed(self, key: str) -> tuple[str, ...] | None:
        """The values setting `key` may take, when it has a fixed set (`choices` or `one_of`), else None."""
        s = self._by_key[key]
        return s.allowed() if isinstance(s, Setting) else None

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

    def model(self) -> type[Any]:
        """The settings as a pydantic model, named `<App>Settings`: a field per setting, `str` (a `Literal` of its
        choices when it has them; with `numbers=True`, `int` or `float` with its range for `whole_between` and
        `number_between`), or `list[str]` for a `ListSetting`. `web.base_app` declares it as the settings routes'
        response, so `/api/openapi.json` describes each setting and a web app's types can be generated from it. Built
        on each call; pydantic is imported here, so a CLI that only reads settings does not load it."""
        from pydantic import Field, create_model

        fields: dict[str, Any] = {k: self._field(s, Field) for k, s in self._by_key.items()}
        name = "".join(p.capitalize() for p in re.split(r"[^0-9A-Za-z]+", self.app) if p) + "Settings"
        return create_model(name, **fields)

    def _field(self, s: AnySetting, field: Callable[..., Any]) -> tuple[Any, Any]:
        if isinstance(s, ListSetting):
            return list[str], ...
        allowed = s.allowed()
        if allowed is not None:
            return Literal[allowed], ...  # pyright: ignore[reportInvalidTypeForm] - a Literal built at run time  # ty: ignore[invalid-type-form]
        n = s.normalise
        if self.numbers and isinstance(n, whole_between):
            return int, field(..., ge=n.lo, le=n.hi)  # le=None leaves the maximum out of the schema
        if self.numbers and isinstance(n, number_between):
            low = {"gt": n.lo} if n.lo_open else {"ge": n.lo}
            return float, field(..., le=n.hi, **low)
        return str, ...

    def folder(self, key: str) -> str:
        """The setting `key` as a folder that exists (created when asked for)."""
        p = self.get(key)
        os.makedirs(p, exist_ok=True)
        return p
