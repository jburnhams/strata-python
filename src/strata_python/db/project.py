"""An app's database settings, from its pyproject.toml:

[tool.strata-db]
app = "notes"                       # owner role name; also picks the URL (config.url_for)
schema = "notes"                    # its schema in the shared database (default: the app name)
models = "notes.models:Base"        # module:attribute with .metadata (a Base or a MetaData)
migrations = "migrations"           # folder of Alembic revisions, relative to pyproject.toml
engine = "portable"                 # or "postgres": uses Postgres-only features; tests and autogenerate then
                                    # need Postgres (strata-db and the pytest plugin follow this)

An app installed as a wheel (a service run from `uv tool install`) has no pyproject.toml beside its code, so the same
keys can live in the package instead, in a `strata-db.toml` (top level, no table header) next to the migrations:

[tool.strata-db]                    # pyproject.toml: only where the declaration is
file = "src/notes/strata-db.toml"

`migrations` is then relative to that file, so the folder ships inside the package, and `Project.load(__file__)` at
service start finds the file by walking up from the module, in a checkout and in site-packages alike. strata-db and
the pytest plugin follow the pointer, so all three read one declaration.
"""

from __future__ import annotations

import importlib
import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from sqlalchemy import MetaData
    from sqlalchemy.engine import Engine

TABLE = "strata-db"
FILE = "strata-db.toml"


def _read(f: Path) -> dict[str, Any]:
    return tomllib.loads(f.read_text(encoding="utf-8"))


def find_table(start: str | os.PathLike[str] | None = None) -> tuple[Path, dict[str, Any]] | None:
    """The nearest declaration, from start (default: cwd) upwards: a `strata-db.toml`, or a pyproject.toml with
    [tool.strata-db] (followed to the file its `file` key names). Returns the folder paths are relative to and the
    keys. Standard library only, so the pytest plugin can ask without loading SQLAlchemy."""
    p = Path(start or os.getcwd()).resolve()
    for d in [p, *p.parents] if p.is_dir() else [p.parent, *p.parent.parents]:
        if (d / FILE).is_file():
            return d, _read(d / FILE)
        f = d / "pyproject.toml"
        if not f.is_file():
            continue
        cfg = cast("dict[str, Any] | None", cast("dict[str, Any]", _read(f).get("tool", {})).get(TABLE))
        if cfg and "file" in cfg:
            target = (d / str(cfg["file"])).resolve()
            if not target.is_file():
                raise FileNotFoundError(f"{f}: [tool.{TABLE}] file = {cfg['file']!r} does not exist")
            return target.parent, _read(target)
        if cfg:
            return d, cfg
    return None


def _import_roots(root: Path) -> list[Path]:
    """Folders to put on sys.path so the models import when the app is not installed (a test run from its checkout):
    `src` and the project folder, or for a declaration inside a package, the folder above its top package."""
    if (root / "__init__.py").is_file():
        top = root
        while (top.parent / "__init__.py").is_file():
            top = top.parent
        return [top.parent]
    return [root / "src", root]


@dataclass
class Project:
    root: Path
    app: str
    models: str
    migrations_dir: Path
    engine: str = "portable"
    schema: str = ""

    def __post_init__(self) -> None:
        self.schema = self.schema or self.app

    @classmethod
    def load(cls, start: str | os.PathLike[str] | None = None) -> Project:
        """The nearest declaration from start (default: cwd) upwards: a `strata-db.toml`, or a pyproject.toml with
        [tool.strata-db] (see the module notes). Pass `__file__` in the service, so an installed wheel finds it too."""
        found = find_table(start)
        if found is None:
            p = Path(start or os.getcwd()).resolve()
            raise FileNotFoundError(
                f"no {FILE} or pyproject.toml with [tool.{TABLE}] in {p} or above (strata-db init writes one)"
            )
        d, cfg = found
        return cls(
            root=d,
            app=cfg["app"],
            models=cfg["models"],
            migrations_dir=(d / cfg.get("migrations", "migrations")).resolve(),
            engine=cfg.get("engine", "portable"),
            schema=cfg.get("schema", cfg["app"]),
        )

    @property
    def metadata(self) -> MetaData:
        from sqlalchemy import MetaData

        for extra in _import_roots(self.root):  # the app need not be installed (a test run from its checkout)
            if extra.is_dir() and str(extra) not in sys.path:
                sys.path.insert(0, str(extra))
        mod, _, attr = self.models.partition(":")
        obj: Any = getattr(importlib.import_module(mod), attr or "Base")
        return obj if isinstance(obj, MetaData) else cast("MetaData", obj.metadata)

    @property
    def postgres_only(self) -> bool:
        return self.engine == "postgres"

    def url(self) -> str:
        from .config import url_for

        return url_for(self.app, self.schema)

    def open(self, url: str | None = None, *, default: str | None = None) -> Engine:
        """An engine on the app's database, migrated to head: `url`, else `url()` with `default` (a URL or path,
        such as `config.local_url(SETTINGS)`) when nothing is configured. Call once and keep the engine."""
        from .config import url_for
        from .engine import connect

        engine = connect(url or url_for(self.app, self.schema, default=default))
        try:
            self.upgrade(engine)
        except BaseException:
            engine.dispose()
            raise
        return engine

    def upgrade(self, engine: Engine, revision: str = "head") -> None:
        """Migrate to head (safe at every start). On Postgres: in the app's schema, as the app's role."""
        from .migrations import upgrade

        upgrade(engine, self.migrations_dir, self.metadata, revision, owner_role=self.app, schema=self.schema)
