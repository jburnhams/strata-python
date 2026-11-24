"""Stages, their keys and records, and the runner that advances one subject through them (see the package's docstring
for where each rule came from)."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from strata_python.contracts import StageState, WorkflowState
from strata_python.tasks.progress import report_progress

DONE, FAILED, CLEARED = "done", "failed", "cleared"
"""A record's status: the stage ran (or the gate was approved), it raised, or it was cleared (forgotten, or an
approval taken back). Records are appended, never changed: the latest per stage is its state."""

# A stage's state: done, ready, stale, failed, waiting or blocked. Declared with the API shapes (`contracts`), so the
# web app's generated types name the same words.

Json = dict[str, Any]


def request_key(*parts: Any) -> str:
    """sha256 of the parts as canonical JSON (sorted keys, no spaces; a tuple is a list): the same request, the same
    key, in every process and on every machine. Values JSON has no form for are written with `str`."""
    body = json.dumps(list(parts), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode()).hexdigest()


def _json(value: Any, what: str) -> Any:
    """`value` as it reads back from JSON, so a record in memory is the record a database returns (a tuple is a
    list, keys are strings). TypeError for something JSON cannot hold: a programming error, never retried."""
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as e:
        raise TypeError(f"{what} must be JSON: {e}") from e


@dataclass(frozen=True)
class Record:
    """One event of one stage for one subject. `data` is the stage's answer (any JSON); `metrics` the numbers it
    measured (listory's evidence: shown on the review screen, checked by a policy); `by` who approved a gate."""

    stage: str
    status: str
    key: str | None
    data: Any = None
    metrics: Mapping[str, Any] = field(default_factory=lambda: {})
    error: str | None = None
    elapsed_s: float = 0.0
    at: float = 0.0
    by: str | None = None

    @property
    def digest(self) -> str:
        """What a dependent's key includes: this record's key and its answer. An answer JSON cannot hold (a picture
        kept in memory, `Pipeline(json=False)`) cannot be compared, so then it is the key alone: the dependents run
        again when this stage's key changes, not when its answer does."""
        try:
            answer = json.dumps(self.data, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError):
            return request_key(self.key)
        return request_key(self.key, answer)

    def as_json(self) -> Json:
        return {
            "stage": self.stage,
            "status": self.status,
            "key": self.key,
            "data": self.data,
            "metrics": dict(self.metrics),
            "error": self.error,
            "elapsed_s": self.elapsed_s,
            "at": self.at,
            "by": self.by,
        }


class Store(Protocol):
    """Where a pipeline's records live. `load` returns the latest record of each stage for the subject."""

    def load(self, subject: str) -> Mapping[str, Record]: ...

    def save(self, subject: str, record: Record) -> None: ...


class MemoryStore:
    """Records in a dict: tests, a CLI run, or a run whose records the app keeps itself (stills' sidecar)."""

    def __init__(self) -> None:
        self.events: dict[str, list[Record]] = {}

    def load(self, subject: str) -> dict[str, Record]:
        return {r.stage: r for r in self.events.get(subject, [])}

    def save(self, subject: str, record: Record) -> None:
        self.events.setdefault(subject, []).append(record)

    def history(self, subject: str) -> list[Record]:
        return list(self.events.get(subject, []))

    def subjects(self) -> list[str]:
        return list(self.events)


@dataclass
class Context:
    """What a stage sees. `app` is whatever the app passed to `advance` (its record, a session, a working picture);
    `inputs` are the values that may enter keys; `previous` is this stage's latest record (a failed attempt's, or
    one for an older key), for a stage that resumes its own partial work."""

    pipeline: Pipeline
    stage: Stage
    subject: str
    inputs: Mapping[str, Any]
    key: str
    records: Mapping[str, Record]
    app: Any = None
    metrics: dict[str, Any] = field(default_factory=lambda: {})

    @property
    def previous(self) -> Record | None:
        return self.records.get(self.stage.name)

    def output(self, name: str) -> Any:
        """The answer of a stage this one needs. KeyError for one it does not: what a stage reads must be in its
        key, or a changed answer upstream would not make it run again."""
        if name not in self.stage.needs:
            raise KeyError(f"stage {self.stage.name!r} does not need {name!r}: add it to its needs to read it")
        return self.records[name].data

    def measure(self, **metrics: Any) -> None:
        self.metrics.update(metrics)


Run = Callable[[Context], Any]

KeyFn = Callable[["Pipeline", "Stage", Mapping[str, Any], Mapping[str, Record]], str]
"""`key(pipeline, stage, inputs, needs)`: a stage's key from the inputs it names in `keys` and its needs' done records.
The default is `default_key`; an app with keys of its own already in use (stills' picture keys, which name files on
disk) passes its function and stays on them. Whatever it returns must change when a need's `digest` does, or a stage
(and an approval) is not run again when the answer it read changes."""


def default_key(pipeline: Pipeline, stage: Stage, inputs: Mapping[str, Any], needs: Mapping[str, Record]) -> str:
    """request_key(pipeline, stage, version, the inputs it reads, each need's digest in the order it names them)."""
    return request_key(pipeline.name, stage.name, stage.version, dict(inputs), [needs[n].digest for n in stage.needs])


@dataclass(frozen=True)
class Stage:
    """A step of a pipeline. `version` is bumped when the code would answer differently (the key changes, so it runs
    again); `keys` name the inputs it reads; `needs` the earlier stages whose answers it reads; `resources` what its
    job must hold (`workflow.store.enqueue` asks the queue for all of the pipeline's). A gate has no `run`: its
    record is an approval, from a person (`Pipeline.approve`) or from `auto` when that says so."""

    name: str
    run: Run | None = None
    version: int | str = 1
    needs: tuple[str, ...] = ()
    keys: tuple[str, ...] = ()
    resources: tuple[str, ...] = ()
    gate: bool = False
    auto: Callable[[Context], bool] | None = None
    label: str = ""

    @property
    def title(self) -> str:
        return self.label or self.name.replace("_", " ").capitalize()


def step(
    name: str,
    run: Run,
    *,
    version: int | str = 1,
    needs: Iterable[str] = (),
    keys: Iterable[str] = (),
    resources: Iterable[str] = (),
    label: str = "",
) -> Stage:
    """A stage that runs code."""
    return Stage(name, run, version, tuple(needs), tuple(keys), tuple(resources), label=label)


def gate(
    name: str,
    *,
    needs: Iterable[str],
    auto: Callable[[Context], bool] | None = None,
    version: int | str = 1,
    keys: Iterable[str] = (),
    label: str = "",
) -> Stage:
    """A stage a person approves. Its key covers its needs' answers, so an approval holds until they change.
    `auto(ctx)` True approves it as `by="auto"` (listory's policy: every check passed and auto-approve is on)."""
    return Stage(name, None, version, tuple(needs), tuple(keys), gate=True, auto=auto, label=label)


@dataclass(frozen=True)
class Outcome:
    """Where a subject stands after `advance`: each stage's state and latest record."""

    subject: str
    states: Mapping[str, StageState]
    records: Mapping[str, Record]
    stopped: bool = False

    @property
    def waiting(self) -> list[str]:
        """The gates waiting for a person."""
        return [s for s, st in self.states.items() if st == "waiting"]

    @property
    def failed(self) -> list[str]:
        return [s for s, st in self.states.items() if st == "failed"]

    @property
    def state(self) -> WorkflowState:
        """One word for the whole: failed before waiting (a failure needs looking at, a gate only a decision)."""
        if self.failed:
            return "failed"
        if self.stopped and any(st in ("ready", "stale") for st in self.states.values()):
            return "stopped"
        if self.waiting:
            return "waiting"
        if all(st == "done" for st in self.states.values()):
            return "done"
        return "blocked"

    def as_json(self) -> Json:
        """For a job's result or an API: `{state, waiting, failed, stages: {name: state}}`."""
        return {"state": self.state, "waiting": self.waiting, "failed": self.failed, "stages": dict(self.states)}


class Pipeline:
    """Named stages in the order they run. A stage may need only stages before it, so the order is the dependency
    order and a whole run is one pass."""

    def __init__(
        self,
        name: str,
        stages: Sequence[Stage],
        *,
        key: KeyFn = default_key,
        json: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """`key` replaces how a stage's key is made (`KeyFn`). `json=False` keeps answers as the stages return them,
        for a run whose records live in memory (`MemoryStore`, or the app's own `Store`): a stage can hand the next one
        a picture as it is. `DbStore` still needs JSON and refuses anything else when it saves."""
        self.name = name
        self.stages: tuple[Stage, ...] = tuple(stages)
        self.key_fn = key
        self.json = json
        self.clock = clock
        self._by_name: dict[str, Stage] = {}
        for s in self.stages:
            if s.name in self._by_name:
                raise ValueError(f"pipeline {name!r} has two stages named {s.name!r}")
            for n in s.needs:
                if n not in self._by_name:
                    raise ValueError(f"stage {s.name!r} needs {n!r}, which is not a stage before it")
            if s.gate == (s.run is not None):
                raise ValueError(f"stage {s.name!r} must have code to run or be a gate, not both or neither")
            self._by_name[s.name] = s

    def __getitem__(self, name: str) -> Stage:
        try:
            return self._by_name[name]
        except KeyError:
            raise KeyError(f"pipeline {self.name!r} has no stage {name!r}: {', '.join(self.names)}") from None

    @property
    def names(self) -> list[str]:
        return [s.name for s in self.stages]

    @property
    def kind(self) -> str:
        """The queue job kind that advances it: `workflow:<name>`."""
        return f"workflow:{self.name}"

    @property
    def resources(self) -> list[str]:
        return sorted({r for s in self.stages for r in s.resources})

    def dependents(self, name: str) -> list[str]:
        """`name` and every stage that needs it, directly or not, in order: what clearing it makes run again."""
        out = {self[name].name}
        for s in self.stages:
            if any(n in out for n in s.needs):
                out.add(s.name)
        return [n for n in self.names if n in out]

    def upto(self, names: Iterable[str]) -> list[str]:
        """These stages and everything they need, in order: what `advance(only=...)` runs."""
        want = {self[n].name for n in names}
        for s in reversed(self.stages):
            if s.name in want:
                want.update(s.needs)
        return [n for n in self.names if n in want]

    # --- keys and states ---------------------------------------------------------------------------------------------

    def key(self, stage: Stage, inputs: Mapping[str, Any], records: Mapping[str, Record]) -> str | None:
        """The stage's key now, or None while a need has no done record."""
        needs: dict[str, Record] = {}
        for n in stage.needs:
            r = records.get(n)
            if r is None or r.status != DONE:
                return None
            needs[n] = r
        missing = [k for k in stage.keys if k not in inputs]
        if missing:
            raise KeyError(f"stage {stage.name!r} reads {', '.join(missing)}, which the inputs do not have")
        return self.key_fn(self, stage, {k: inputs[k] for k in stage.keys}, needs)

    def states(self, records: Mapping[str, Record], inputs: Mapping[str, Any]) -> dict[str, StageState]:
        """Every stage's state (strata-360's matrix row, listory's review phase). A need that is not done blocks its
        dependents, even one whose own record would still match: the need's next answer may differ."""
        out: dict[str, StageState] = {}
        for s in self.stages:
            out[s.name] = self._state(s, records, inputs, out)
        return out

    def _state(
        self, s: Stage, records: Mapping[str, Record], inputs: Mapping[str, Any], so_far: Mapping[str, StageState]
    ) -> StageState:
        r = records.get(s.name)
        if any(so_far.get(n) != "done" for n in s.needs):
            return "blocked"
        key = self.key(s, inputs, records)
        if r is not None and r.key == key and r.status in (DONE, FAILED):
            return "done" if r.status == DONE else "failed"
        if s.gate:
            return "waiting"
        return "stale" if r is not None and r.status in (DONE, FAILED) else "ready"

    # --- running -----------------------------------------------------------------------------------------------------

    def advance(
        self,
        store: Store,
        subject: str,
        inputs: Mapping[str, Any],
        app: Any = None,
        *,
        only: Iterable[str] | None = None,
        stopping: Callable[[], bool] = lambda: False,
        raise_errors: bool = True,
    ) -> Outcome:
        """Run every stage that is not done, in order, until the end or a gate; return where the subject stands.

        A stage that raises is recorded as failed and the stages that do not need it still run (strata-360's
        fail-soft: a failed peaks does not stop thumbnails); then the first error is raised (so a queue job retries
        with its backoff, and the next attempt starts at the failed stage), or with `raise_errors=False` the outcome
        says `failed`. A gate approves itself when its `auto` says so, else it is `waiting` and its dependents are
        blocked. `stopping()` is asked before each stage (a job's `stopping`): those not started wait for the next
        call. `only` runs those stages and what they need."""
        records = dict(store.load(subject))
        names = set(self.upto(only)) if only is not None else set(self.names)
        first: Exception | None = None
        stopped = False
        states: dict[str, StageState] = {}
        for s in self.stages:
            if s.name not in names:
                states[s.name] = self._state(s, records, inputs, states)
                continue
            st = self._state(s, records, inputs, states)
            if st in ("ready", "stale", "failed", "waiting") and stopping():
                stopped = True
            elif st == "waiting" and s.auto is not None:
                key = self.key(s, inputs, records)
                assert key is not None  # noqa: S101 - a waiting gate's needs are done
                if s.auto(self._context(s, subject, inputs, key, records, app)):
                    rec = Record(s.name, DONE, key, at=self.clock(), by="auto")
                    store.save(subject, rec)
                    records[s.name] = rec
                    st = "done"
            elif st in ("ready", "stale", "failed"):
                st, error = self._run(s, store, subject, inputs, records, app)
                first = first or error
            states[s.name] = st
        outcome = Outcome(subject, states, records, stopped=stopped)
        if first is not None and raise_errors:
            raise first
        return outcome

    def _context(
        self, s: Stage, subject: str, inputs: Mapping[str, Any], key: str, records: Mapping[str, Record], app: Any
    ) -> Context:
        return Context(self, s, subject, inputs, key, records, app)

    def _run(
        self,
        s: Stage,
        store: Store,
        subject: str,
        inputs: Mapping[str, Any],
        records: dict[str, Record],
        app: Any,
    ) -> tuple[StageState, Exception | None]:
        key = self.key(s, inputs, records)
        assert key is not None  # noqa: S101 - its needs are done
        assert s.run is not None  # noqa: S101 - a gate is not run
        ctx = self._context(s, subject, inputs, key, records, app)
        index = self.names.index(s.name)
        report_progress(index, len(self.stages), s.title, stage=s.name)
        started = time.monotonic()
        error: Exception | None = None
        try:
            data = s.run(ctx)
            metrics: Any = dict(ctx.metrics)
            if self.json:
                data = _json(data, f"what stage {s.name!r} returns")
                metrics = _json(metrics, f"the metrics of stage {s.name!r}")
        except Exception as e:  # recorded, then raised by advance once the stages that do not need it have run
            error = e
            rec = Record(
                s.name,
                FAILED,
                key,
                error=f"{type(e).__name__}: {e}",
                elapsed_s=time.monotonic() - started,
                at=self.clock(),
            )
        else:
            rec = Record(s.name, DONE, key, data, metrics, elapsed_s=time.monotonic() - started, at=self.clock())
        store.save(subject, rec)
        records[s.name] = rec
        return ("failed" if error is not None else "done"), error

    # --- people ------------------------------------------------------------------------------------------------------

    def approve(
        self, store: Store, subject: str, name: str, inputs: Mapping[str, Any], *, by: str, value: Any = None
    ) -> Record:
        """Approve a gate for its needs' current answers; `value` is kept with it (the policy it was judged by, the
        choice made). ValueError while a need is not done: there is nothing yet to approve."""
        s = self[name]
        if not s.gate:
            raise ValueError(f"{s.title} is a stage, not a gate: it is run, not approved")
        records = store.load(subject)
        states = self.states(records, inputs)
        blocked = [n for n in s.needs if states[n] != "done"]
        if blocked:
            raise ValueError(f"{s.title} cannot be approved before {', '.join(self[n].title for n in blocked)} is done")
        key = self.key(s, inputs, records)
        rec = Record(name, DONE, key, _json(value, "an approval's value"), at=self.clock(), by=by)
        store.save(subject, rec)
        return rec

    def clear(self, store: Store, subject: str, name: str, *, by: str | None = None, cascade: bool = True) -> list[str]:
        """Forget `name` (and, by default, its dependents) for the subject, so `advance` runs it again; for a gate this
        takes the approval back. The names that had a record to clear."""
        records = store.load(subject)
        done: list[str] = []
        for n in self.dependents(name) if cascade else [self[name].name]:
            r = records.get(n)
            if r is not None and r.status != CLEARED:
                store.save(subject, Record(n, CLEARED, None, at=self.clock(), by=by))
                done.append(n)
        return done

    def status(self, store: Store, subject: str, inputs: Mapping[str, Any]) -> Outcome:
        """Where the subject stands, without running anything."""
        records = store.load(subject)
        return Outcome(subject, self.states(records, inputs), records)
