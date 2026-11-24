"""The life of a row as declared transitions: strata-listory's processing plan (planning -> proposed -> approved ->
running -> done | failed, or rejected), strata-stills' arena flags (open -> investigating -> fixed -> confirmed, or
wontfix). Each app checked these with a tuple of state names and an `if`, and claimed a row with a hand-written
`UPDATE ... WHERE state='approved'`; here the transitions are data, a wrong move is a `ValueError` with a sentence (a
400 through `web.add_error_handlers`), and `workflow.store.move` turns "may go to running" into the WHERE clause.

    PLAN = StateMachine("planning", {
        "planning": {"proposed", "failed"},
        "proposed": {"approved", "rejected", "planning"},
        "approved": {"running", "proposed"},
        "running": {"done", "failed"},
        "failed": {"planning"},
    })
    PLAN.check(plan.state, "approved")       # InvalidTransition: "a rejected plan cannot be approved"
    PLAN.sources("running")                  # {"approved"}: what an atomic claim's WHERE names
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping


class InvalidTransition(ValueError):
    """A move the machine does not allow. A ValueError, so an API answers it as a 400 with this sentence."""

    def __init__(self, noun: str, frm: str, to: str) -> None:
        a = "an" if frm[:1] in "aeiou" else "a"
        super().__init__(f"{a} {frm} {noun} cannot be {to}" if frm != to else f"the {noun} is already {to}")
        self.frm = frm
        self.to = to


class StateMachine:
    """States and the moves between them. A state named only as a target is terminal (`done`, `rejected`)."""

    def __init__(self, initial: str, transitions: Mapping[str, Iterable[str]], *, noun: str = "item") -> None:
        self.initial = initial
        self.noun = noun  # for the error sentence: "a rejected plan cannot be approved"
        self.transitions: dict[str, frozenset[str]] = {k: frozenset(v) for k, v in transitions.items()}
        targets = {t for ts in self.transitions.values() for t in ts}
        self.states: frozenset[str] = frozenset(self.transitions) | targets | {initial}
        self.terminal: frozenset[str] = frozenset(s for s in self.states if not self.transitions.get(s))

    def can(self, frm: str, to: str) -> bool:
        return to in self.transitions.get(frm, frozenset())

    def check(self, frm: str, to: str) -> None:
        """Raise InvalidTransition unless `frm` may move to `to`; ValueError for a state the machine does not have."""
        for s in (frm, to):
            if s not in self.states:
                raise ValueError(f"{s!r} is not a state of a {self.noun}: one of {', '.join(sorted(self.states))}")
        if not self.can(frm, to):
            raise InvalidTransition(self.noun, frm, to)

    def sources(self, to: str) -> frozenset[str]:
        """Every state that may move to `to`: the states a conditional UPDATE may find the row in."""
        return frozenset(s for s, ts in self.transitions.items() if to in ts)
