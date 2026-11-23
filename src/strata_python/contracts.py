"""The data shapes the strata apps share across their HTTP APIs, written once as pydantic models.

Every app's server returned these as plain dicts and every web app retyped them by hand (`web/src/api.ts`), with the
field names drifting (a job's `state` or `status`, progress text as `current`, `text` or `stage`). Declared as a route's
`response_model`, a shape is checked on the way out and lands in `/api/openapi.json`, so a web app's TypeScript can be
generated from it instead of copied.

  Health        GET /api/health: ok, version, commit, plus the app's own fields (web.base_app)
  SystemInfo    GET /api/system: version, commit, Python, platform, folders with their disks, services it calls (web.add_system)
  ErrorBody     every error response: detail, and a payload when the error carries one (web.add_error_handlers)
  Task          a job or task row: id, kind, state, times, error, result, progress (tasks.Queue answers these)
  Progress      how far a task has got: done of total, a line of text, the stage (tasks.ProgressReporter writes these)
  Page[T]       one page of a list: items, the cursor for the next page (None at the end), the total when it is known
  FileRef       a file by content and place: sha256, size, mtime, root, rel (the path under that root)
  Judgment      one pairwise choice by a person or a model: a, b, the winner (a, b or tie), who, when, undone when
  ApiModel      a base for an app's own response shapes: a field with a default is still required in the schema

Times are timezone-aware datetimes, sent as ISO 8601 with their offset: the apps had both ISO text and float epochs,
and only one of those reads correctly in a browser. Use `utc(epoch)` to turn a stored float into one."""

from __future__ import annotations

import datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field


def utc(epoch: float | None) -> datetime.datetime | None:
    """A float epoch (`time.time()`) as an aware UTC datetime; None stays None."""
    return None if epoch is None else datetime.datetime.fromtimestamp(epoch, datetime.UTC)


class ApiModel(BaseModel):
    """The base of the response shapes here, and for an app's own. pydantic marks a field with a default as optional in
    the schema, so the generated TypeScript gets a `?` though the server always sends it; this config makes the
    response schema say what is sent (`Page.next` is `string | null`, not `next?`). Use it for responses: a request
    body built on it has an `-Input` and an `-Output` schema. `FileRef` and `Judgment` stay on `BaseModel`, since an
    app may take them as a request body."""

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


class Health(ApiModel):
    """What `GET /api/health` answers. Extra fields are the app's own (strata-models adds `gpu`)."""

    model_config = ConfigDict(extra="allow")
    ok: bool
    version: str
    commit: str | None = None  # the source commit the server runs, so a probe can tell a stale deploy


class Folder(ApiModel):
    """A folder the app uses, and the disk it is on (None when the folder does not exist yet)."""

    name: str
    path: str
    exists: bool
    free_bytes: int | None = None
    total_bytes: int | None = None


class ServiceStatus(ApiModel):
    """Another strata service this app calls, as its health endpoint answered just now."""

    name: str
    url: str
    ok: bool
    version: str | None = None
    detail: str | None = None  # why it is not ok: not reachable, or what it answered


class SystemInfo(ApiModel):
    """What `GET /api/system` answers: what is running, where its data is, and whether its services answer."""

    model_config = ConfigDict(extra="allow")
    version: str
    commit: str | None = None
    python: str
    platform: str
    started_at: datetime.datetime
    folders: list[Folder] = Field(default_factory=lambda: [])
    services: list[ServiceStatus] = Field(default_factory=lambda: [])


class ErrorBody(ApiModel):
    """The body of every error the app answers. `detail` is what FastAPI already uses, so clients that read it keep
    working; `payload` carries what the error wants the client to have (the job row of a refused job)."""

    detail: str
    payload: Any = None


TaskState = Literal["queued", "running", "done", "failed", "cancelled"]
FINISHED: frozenset[str] = frozenset({"done", "failed", "cancelled"})


class Progress(ApiModel):
    """How far a task has got. `total` None means not known yet; `text` is the line a person reads ("Page 3 of 40")."""

    done: float = 0
    total: float | None = None
    text: str = ""
    stage: str | None = None
    at: datetime.datetime | None = None

    @property
    def fraction(self) -> float | None:
        return None if not self.total else min(max(self.done / self.total, 0.0), 1.0)


class Task(ApiModel):
    """A job or task row as an API answers it."""

    id: int | str
    kind: str
    state: TaskState
    created_at: datetime.datetime
    started_at: datetime.datetime | None = None
    finished_at: datetime.datetime | None = None
    error: str | None = None
    result: Any = None
    progress: Progress | None = None
    attempts: int = 0
    domain: str | None = None  # what the task is about in the app's terms (a book id, a folder)


T = TypeVar("T")


class Page(ApiModel, Generic[T]):  # Generic, not `Page[T]`: Python 3.11 is supported
    """One page of a list. `next` is an opaque cursor (pass it back as `after`), None on the last page; a cursor, not an
    offset, so rows added while a person pages through do not repeat or vanish."""

    items: list[T]
    next: str | None = None
    total: int | None = None


class FileRef(BaseModel):
    """A file by content and place. `sha256` is lower-case hex (the apps had hex and raw bytes); `rel` is the path under
    `root` with `/` separators, so the same file found from two machines or two apps joins on (sha256) or (root, rel)."""

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)
    mtime: float
    root: str
    rel: str


class Judgment(BaseModel):
    """One pairwise choice: `a` and `b` are the two things compared, `winner` names a side (or a tie), `rater` who chose
    (a person's name in the app, or a model's). Undoing keeps the row and sets `undone_at`, so a ranking can be rebuilt."""

    a: str
    b: str
    winner: Literal["a", "b", "tie"]
    rater: str
    at: datetime.datetime
    undone_at: datetime.datetime | None = None
