"""Work in named, versioned stages over one subject (a book, a tape, a picture), cached by what went into each stage,
resumable, with people's approvals as stages of their own; and the small state machines apps keep for a row's life.

Four apps had built the same thing four times:

- strata-360's pipeline runner: stages with a version, the config keys they read and the stages they need; a stage
  is skipped when its cache key (version, the clip's fingerprint, those keys, the upstream keys) is unchanged, so a
  re-run is cheap and a changed setting re-runs only what it affects; `clear` cascades to the dependents; a state
  matrix (ok, failed, stale, active) for the screen. This module's model is that one.
- strata-listory's ingest: eleven stages over a book, each an append-only event (running, done, failed) with its
  metrics, its data and how long it took; a resume skips the stages that are done; three review gates where a person
  approves, the approval bound to a signature of the evidence it saw (new evidence needs a new approval) or given by
  the policy automatically when every check passes.
- strata-stills' develop pipeline: ordered stages, each with settings and a version, the key of all of them naming
  the picture made, so asking for the same thing again finds it; timing per stage.
- strata-reels: `hash` then `proxy`, `thumbnails` and `peaks`, each a job that queues the next, each result keyed on
  its inputs' hashes, its parameters and the recipe's version.

What they share is here; what each stage does stays in the app.

    from strata_python.workflow import Pipeline, gate, step

    INGEST = Pipeline("ingest", [
        step("inventory", inventory, version=3, keys=("files",)),     # reads inputs["files"]
        step("metadata", metadata, needs=("inventory",), keys=("providers",)),
        gate("book", needs=("metadata",), auto=lambda c: c.output("metadata")["score"] > 0.9),
        step("plan", plan, needs=("book", "inventory"), version=2, resources=("disk",)),
    ])

    def inventory(ctx: Context) -> dict:                  # any JSON value; ctx.measure(...) adds metrics
        return {"files": scan(ctx.app.path)}

    out = INGEST.advance(store, "book:12", {"files": fingerprint, "providers": ["audible"]}, app=book)
    out.state          # "done", "waiting" (on a gate: out.waiting), "failed", "blocked" or "stopped"
    INGEST.approve(store, "book:12", "book", inputs, by="jonathan")   # then advance again (or enqueue it)
    INGEST.clear(store, "book:12", "metadata")                        # it and everything after it run again

The key of a stage is `request_key(pipeline, stage, version, its keys from the inputs, the digest of each need's
record)`. A need's digest covers its key and its answer, so a need that ran again for a new key, or gave another
answer, makes its dependents (and an approval of it) out of date: a stage that writes files and returns little must
not leave the stages after it on the old files. A clear re-run that answers the same leaves them alone. A gate's record
is an approval: its key is the key it was given for, so it holds only while the evidence it saw is current.

Records go to a `Store`, a two-method protocol (`load`, `save`): `MemoryStore` here (tests, a CLI run, stills'
in-process runs), `workflow.store.DbStore` (an append-only table in the app's database, with the db extra), or the
app's own (stills' sidecar JSON). `Pipeline(key=...)` swaps how keys are made, for an app whose keys already name
things on disk; `Pipeline(json=False)` lets stages hand each other values JSON cannot hold, when the store is memory. Stages that must survive a restart run as one
queue job per subject (`workflow.store.enqueue`): the queue gives the retry, the resources and the cancel, and a
retried job resumes at the stage that failed.

`StateMachine` is the other half: the life of one row (a plan proposed, approved, running, done) as declared
transitions, checked in code and moved in the database with one conditional UPDATE (`workflow.store.move`).
"""

from strata_python.workflow.machine import InvalidTransition, StateMachine
from strata_python.workflow.pipeline import (
    CLEARED,
    DONE,
    FAILED,
    Context,
    KeyFn,
    MemoryStore,
    Outcome,
    Pipeline,
    Record,
    Stage,
    StageState,
    Store,
    default_key,
    gate,
    request_key,
    step,
)

__all__ = [
    "CLEARED",
    "DONE",
    "FAILED",
    "Context",
    "InvalidTransition",
    "KeyFn",
    "MemoryStore",
    "Outcome",
    "Pipeline",
    "Record",
    "Stage",
    "StageState",
    "StateMachine",
    "Store",
    "default_key",
    "gate",
    "request_key",
    "step",
]
