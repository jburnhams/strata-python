"""Pipelines and state machines on the in-memory store: keys, resume, fail-soft, gates, clears and states."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from strata_python.tasks import ProgressReporter, reporting
from strata_python.workflow import (
    CLEARED,
    DONE,
    FAILED,
    Context,
    InvalidTransition,
    MemoryStore,
    Pipeline,
    Record,
    StateMachine,
    gate,
    request_key,
    step,
)


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        self.t += 1
        return self.t


class Calls:
    """Stage code that counts its runs and can be told to fail or to answer differently."""

    def __init__(self) -> None:
        self.runs: list[str] = []
        self.fail: set[str] = set()
        self.answer: dict[str, Any] = {}

    def __call__(self, name: str) -> Any:
        def run(ctx: Context) -> Any:
            self.runs.append(name)
            if name in self.fail:
                raise OSError(f"{name} could not read the disk")
            ctx.measure(**{f"{name}.runs": self.runs.count(name)})
            got = {n: ctx.output(n) for n in ctx.stage.needs}
            return self.answer.get(name, {"stage": name, "from": got})

        return run


def tape(calls: Calls, *, auto: Any = None) -> Pipeline:
    """reels' shape with a review: hash, then three assets from it, a review of the proxy, then a plan."""
    return Pipeline(
        "tape",
        [
            step("hash", calls("hash"), keys=("fingerprint",), resources=("disk",)),
            step("proxy", calls("proxy"), needs=("hash",), keys=("height",), resources=("disk",)),
            step("thumbnails", calls("thumbnails"), needs=("hash",), version=2),
            step("peaks", calls("peaks"), needs=("hash",)),
            gate("review", needs=("proxy", "thumbnails"), auto=auto),
            step("plan", calls("plan"), needs=("review", "proxy"), resources=("gpu",)),
        ],
        clock=Clock(),
    )


INPUTS = {"fingerprint": "abc", "height": 540}


def test_request_key_is_canonical():
    assert request_key({"a": 1, "b": (1, 2)}) == request_key({"b": [1, 2], "a": 1})
    assert request_key("x", 1) != request_key("x", "1")
    assert len(request_key(object())) == 64  # anything JSON has no form for is written with str


def test_advance_runs_to_the_gate_then_resumes_after_approval():
    calls, store = Calls(), MemoryStore()
    p = tape(calls)
    out = p.advance(store, "tape:1", INPUTS)
    assert calls.runs == ["hash", "proxy", "thumbnails", "peaks"]
    assert (out.state, out.waiting, out.states["plan"]) == ("waiting", ["review"], "blocked")
    assert out.records["proxy"].data == {"stage": "proxy", "from": {"hash": {"stage": "hash", "from": {}}}}
    assert out.records["proxy"].metrics == {"proxy.runs": 1}
    assert out.as_json()["stages"]["review"] == "waiting"

    # nothing runs twice: the done stages are found by their keys
    assert p.advance(store, "tape:1", INPUTS).state == "waiting"
    assert calls.runs == ["hash", "proxy", "thumbnails", "peaks"]

    rec = p.approve(store, "tape:1", "review", INPUTS, by="ada", value={"note": "fine"})
    assert (rec.status, rec.by, rec.data) == (DONE, "ada", {"note": "fine"})
    out = p.advance(store, "tape:1", INPUTS)
    assert (out.state, calls.runs[-1]) == ("done", "plan")
    assert out.records["plan"].data["from"]["review"] == {"note": "fine"}  # a gate's value is its answer
    assert p.status(store, "tape:1", INPUTS).state == "done"
    assert (p.kind, p.resources) == ("workflow:tape", ["disk", "gpu"])


def test_a_changed_input_reruns_only_what_reads_it():
    calls, store = Calls(), MemoryStore()
    p = tape(calls, auto=lambda c: True)
    p.advance(store, "t", INPUTS)
    calls.runs.clear()
    out = p.advance(store, "t", {**INPUTS, "height": 720})
    # proxy reads height; thumbnails and peaks do not. plan needs proxy, whose key moved, so it runs again even with
    # the same answer: a stage that writes files and returns little must not leave its dependents on the old files
    assert calls.runs == ["proxy", "plan"]
    assert out.state == "done"
    assert out.records["review"].by == "auto"  # the gate saw new evidence and its policy approved it again
    calls.runs.clear()
    p.advance(store, "t", {**INPUTS, "height": 720, "fingerprint": "new"})
    assert calls.runs == ["hash", "proxy", "thumbnails", "peaks", "plan"]  # everything reads the file


def test_a_new_answer_upstream_makes_an_approval_out_of_date():
    calls, store = Calls(), MemoryStore()
    p = tape(calls)
    p.advance(store, "t", INPUTS)
    p.approve(store, "t", "review", INPUTS, by="ada")
    p.advance(store, "t", INPUTS)
    calls.answer["thumbnails"] = {"other": 1}
    p.clear(store, "t", "thumbnails", cascade=False)
    out = p.advance(store, "t", INPUTS)
    assert (out.state, out.waiting, out.states["plan"]) == ("waiting", ["review"], "blocked")
    assert calls.runs.count("plan") == 1  # not run again behind the gate


def test_a_failed_stage_is_recorded_the_others_still_run_and_the_error_is_raised():
    calls, store = Calls(), MemoryStore()
    calls.fail.add("proxy")
    p = tape(calls)
    with pytest.raises(OSError, match="proxy could not read"):
        p.advance(store, "t", INPUTS)
    assert calls.runs == ["hash", "proxy", "thumbnails", "peaks"]
    rec = store.load("t")["proxy"]
    assert (rec.status, rec.error) == (FAILED, "OSError: proxy could not read the disk")
    out = p.status(store, "t", INPUTS)
    assert (out.state, out.failed, out.states["review"]) == ("failed", ["proxy"], "blocked")
    out = p.advance(store, "t", INPUTS, raise_errors=False)  # tried again; still failing, said not raised
    assert (out.state, calls.runs.count("proxy")) == ("failed", 2)
    calls.fail.clear()
    out = p.advance(store, "t", INPUTS)  # the retry starts at the failed stage
    assert (out.state, calls.runs.count("hash")) == ("waiting", 1)
    assert [r.status for r in store.history("t") if r.stage == "proxy"] == [FAILED, FAILED, DONE]


def test_an_answer_that_is_not_json_is_a_programming_error():
    def bad(ctx: Context) -> Any:
        return {"x": object()}

    p = Pipeline("p", [step("a", bad)])
    with pytest.raises(TypeError, match="what stage 'a' returns must be JSON"):
        p.advance(MemoryStore(), "s", {})


def test_answers_read_back_as_json_and_stopping_leaves_the_rest():
    def a(ctx: Context) -> Any:
        return {"t": (1, 2), 3: "x"}

    seen: list[str] = []
    p = Pipeline("p", [step("a", a), step("b", lambda c: seen.append("b"), needs=("a",))])
    store = MemoryStore()
    asked = iter([False, True])
    out = p.advance(store, "s", {}, stopping=lambda: next(asked))
    assert (out.state, seen, store.load("s")["a"].data) == ("stopped", [], {"t": [1, 2], "3": "x"})
    assert p.advance(store, "s", {}).state == "done"


def test_only_runs_those_stages_and_their_needs_and_reports_progress():
    calls, store = Calls(), MemoryStore()
    p = tape(calls)
    seen: list[tuple[str | None, str]] = []
    with reporting(ProgressReporter(lambda pr: seen.append((pr.stage, pr.text)), min_interval_s=0)):
        out = p.advance(store, "t", INPUTS, only=["peaks"])
    assert calls.runs == ["hash", "peaks"]
    assert seen == [("hash", "Hash"), ("peaks", "Peaks")]
    assert (out.states["proxy"], out.state) == ("ready", "blocked")
    assert p.upto(["plan"]) == ["hash", "proxy", "thumbnails", "review", "plan"]


def test_clear_cascades_and_takes_an_approval_back():
    calls, store = Calls(), MemoryStore()
    p = tape(calls)
    p.advance(store, "t", INPUTS)
    p.approve(store, "t", "review", INPUTS, by="ada")
    p.advance(store, "t", INPUTS)
    assert p.dependents("proxy") == ["proxy", "review", "plan"]
    assert p.clear(store, "t", "review", by="ada") == ["review", "plan"]
    assert store.load("t")["review"].status == CLEARED
    assert p.clear(store, "t", "review") == []  # nothing left to clear
    out = p.status(store, "t", INPUTS)
    assert (out.states["review"], out.states["plan"]) == ("waiting", "blocked")
    assert p.clear(store, "t", "hash") == ["hash", "proxy", "thumbnails", "peaks"]
    states = p.status(store, "t", INPUTS).states
    assert states == {
        "hash": "ready",
        "proxy": "blocked",
        "thumbnails": "blocked",
        "peaks": "blocked",
        "review": "blocked",
        "plan": "blocked",
    }


def test_stale_when_the_version_changes():
    calls, store = Calls(), MemoryStore()
    p1 = Pipeline("p", [step("a", calls("a")), step("b", calls("b"), needs=("a",))])
    p1.advance(store, "s", {})
    p2 = Pipeline("p", [step("a", calls("a"), version=2), step("b", calls("b"), needs=("a",))])
    assert p2.status(store, "s", {}).states == {"a": "stale", "b": "blocked"}
    calls.answer["a"] = "new"
    p2.advance(store, "s", {})
    assert calls.runs == ["a", "b", "a", "b"]


def test_approve_refuses_what_cannot_be_approved():
    calls, store = Calls(), MemoryStore()
    p = tape(calls)
    with pytest.raises(ValueError, match="Review cannot be approved before Proxy, Thumbnails is done"):
        p.approve(store, "t", "review", INPUTS, by="ada")
    with pytest.raises(ValueError, match="Hash is a stage, not a gate"):
        p.approve(store, "t", "hash", INPUTS, by="ada")


def test_a_stage_reads_only_what_it_needs_and_inputs_it_names():
    def peek(ctx: Context) -> Any:
        assert ctx.previous is None
        return ctx.output("a")

    p = Pipeline("p", [step("a", lambda c: 1), step("b", lambda c: 2), step("c", peek, needs=("b",))])
    with pytest.raises(KeyError, match="does not need 'a'"):
        p.advance(MemoryStore(), "s", {})
    q = Pipeline("q", [step("a", lambda c: 1, keys=("missing",))])
    with pytest.raises(KeyError, match="reads missing"):
        q.advance(MemoryStore(), "s", {})


def test_the_pipeline_is_checked_when_it_is_made():
    run = lambda c: None  # noqa: E731
    with pytest.raises(ValueError, match="two stages named 'a'"):
        Pipeline("p", [step("a", run), step("a", run)])
    with pytest.raises(ValueError, match="needs 'b', which is not a stage before it"):
        Pipeline("p", [step("a", run, needs=("b",)), step("b", run)])
    with pytest.raises(ValueError, match="code to run or be a gate"):
        Pipeline("p", [replace(gate("g", needs=()), run=run)])  # a gate given code too
    with pytest.raises(KeyError, match="has no stage 'x': a"):
        Pipeline("p", [step("a", run)])["x"]


def test_record_as_json():
    r = Record("a", DONE, "k", {"x": 1}, {"m": 2}, at=5.0)
    assert r.as_json() == {
        "stage": "a",
        "status": "done",
        "key": "k",
        "data": {"x": 1},
        "metrics": {"m": 2},
        "error": None,
        "elapsed_s": 0.0,
        "at": 5.0,
        "by": None,
    }
    assert r.digest != Record("a", DONE, "k", {"x": 2}).digest


PLAN = StateMachine(
    "planning",
    {
        "planning": {"proposed", "failed"},
        "proposed": {"approved", "rejected", "planning"},
        "approved": {"running", "proposed"},
        "running": {"done", "failed"},
        "failed": {"planning"},
    },
    noun="plan",
)


def test_state_machine():
    assert PLAN.states == {"planning", "proposed", "approved", "rejected", "running", "done", "failed"}
    assert PLAN.terminal == {"rejected", "done"}
    assert PLAN.can("proposed", "approved")
    assert not PLAN.can("done", "running")
    PLAN.check("approved", "running")
    with pytest.raises(InvalidTransition, match="a rejected plan cannot be approved") as e:
        PLAN.check("rejected", "approved")
    assert isinstance(e.value, ValueError)
    assert (e.value.frm, e.value.to) == ("rejected", "approved")
    with pytest.raises(InvalidTransition, match="an approved plan cannot be done"):
        PLAN.check("approved", "done")
    with pytest.raises(InvalidTransition, match="the plan is already running"):
        PLAN.check("running", "running")
    with pytest.raises(ValueError, match="'lost' is not a state of a plan"):
        PLAN.check("lost", "done")
    assert PLAN.sources("running") == {"approved"}
    assert PLAN.sources("planning") == {"proposed", "failed"}


def test_an_auto_gate_that_says_no_waits_for_a_person():
    calls, store = Calls(), MemoryStore()
    p = tape(calls, auto=lambda c: c.output("proxy")["stage"] == "nothing")
    assert p.advance(store, "t", INPUTS).waiting == ["review"]
    assert p.key(p["review"], INPUTS, {}) is None  # no key while a need has no answer
