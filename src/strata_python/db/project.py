"""An app's database settings, from its pyproject.toml:

[tool.strata-db]
app = "notes"                       # owner role name; also picks the URL (config.url_for)
schema = "notes"                    # its schema in the shared database (default: the app name)
models = "notes.models:Base"        # module:attribute with .metadata (a Base or a MetaData)
migrations = "migrations"           # folder of Alembic revisions, relative to pyproject.toml
engine = "portable"                 # or "postgres": uses Postgres-only features; tests and autogenerate then
                                    # need Postgres (strata-db and the pytest plugin follow this)
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
        """Find the nearest pyproject.toml with [tool.strata-db], from start (default: cwd) upwards."""
        p = Path(start or os.getcwd()).resolve()
        for d in [p, *p.parents] if p.is_dir() else [p.parent, *p.parent.parents]:
            f = d / "pyproject.toml"
            if not f.is_file():
                continue
            cfg = cast("dict[str, Any]", tomllib.loads(f.read_text(encoding="utf-8")).get("tool", {})).get(TABLE)
            if cfg:
                return cls(
                    root=d,
                    app=cfg["app"],
                    models=cfg["models"],
                    migrations_dir=(d / cfg.get("migrations", "migrations")).resolve(),
                    engine=cfg.get("engine", "portable"),
                    schema=cfg.get("schema", cfg["app"]),
                )
        raise FileNotFoundError(f"no pyproject.toml with [tool.{TABLE}] in {p} or above (strata-db init writes one)")

    @property
    def metadata(self) -> MetaData:
        from sqlalchemy import MetaData

        for extra in (self.root / "src", self.root):  # the app need not be installed (a test run from its checkout)
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

    def upgrade(self, engine: Engine, revision: str = "head") -> None:
        """Migrate to head (safe at every start). On Postgres: in the app's schema, as the app's role."""
        from .migrations import upgrade

        upgrade(engine, self.migrations_dir, self.metadata, revision, owner_role=self.app, schema=self.schema)
