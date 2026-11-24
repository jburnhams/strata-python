"""The server half of strata-react's `createOutbox`: take each client-made write once, and order writes per parent row.

    from strata_python.db.writes import insert_once, lock_row

    @app.post("/api/answers")
    def post_answer(a: AnswerIn, db: DbSession) -> AnswerOut:
        lock_row(db, Learner, a.learner_id)                  # one learner's writes in the order the database saw them
        row, duplicate = insert_once(
            db, Answer, a.event_id,
            make=lambda: judge(db, a),                       # the app's own work runs only for a write not seen yet
            same=lambda old: old.choice == a.choice and utc(old.at) == a.at,
        )
        if not duplicate:
            db.commit()
        return AnswerOut.of(row, duplicate=duplicate)

The outbox retries a write until the server answers, so a write can arrive twice: the first answer was lost, or a retry
overtook a slow first try. Flashtap wrote this by hand (`learners/store.py`: `record`, `_same`, `hold`, issue #74);
every app that adopts the outbox would write it again. Needs the `[db]` extra.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar, cast

from sqlalchemy import inspect, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from strata_python.tasks import Conflict

M = TypeVar("M")


class ConflictingWrite(Conflict):
    """A write whose id is already taken by a write with other contents: an event is never rewritten. A `Conflict`, so
    `web.add_error_handlers` answers 409, with the id as the payload."""


def insert_once(
    db: Session, model: type[M], key: Any, make: Callable[[], M], *, same: Callable[[M], bool]
) -> tuple[M, bool]:
    """Insert the row `make()` builds under the client-made id `key`, once. Returns the row and whether it was a
    duplicate:

    - no row with that id: `make()` is called, its row added and flushed (commit yourself), `duplicate=False`;
    - a row with that id that `same(old)` accepts: the stored row, `duplicate=True`, and the session rolled back, so a
      retry gets the first answer's result, nothing is written and a lock taken with `lock_row` is let go;
    - a row with that id that `same` refuses: `ConflictingWrite` (409).

    The check comes before `make`, so the app's own work (judging an answer against content that may have moved on
    since the first try) never runs on a retry. `same` compares what the client sent with what was stored, not a row
    rebuilt now; normalise times to UTC on both sides (SQLite gives back naive datetimes).

    Two tries of one id at once (without `lock_row` on a parent to queue them): the second's insert fails on the
    primary key once the first commits, and is answered as a duplicate of the row the first stored. That rolls back
    the whole session, so build everything this write adds inside `make`."""
    old = db.get(model, key)
    if old is None:
        row = make()
        db.add(row)
        try:
            db.flush()
            return row, False
        except IntegrityError:
            db.rollback()  # another try of this id got there first, or `make` broke a constraint: read again
            old = db.get(model, key)
            if old is None:
                raise
    if not same(old):
        raise ConflictingWrite(f"{key} is already recorded with different contents; a write is never rewritten.", key)
    db.rollback()  # nothing to write: let any lock go
    return old, True


def lock_row(db: Session, model: type[M], key: Any) -> M:
    """Take the row lock on the `model` row `key` until this transaction ends, and return the row (KeyError, a 404
    through `add_error_handlers`, when there is none). Writes that each take it are serialised, so a time stamped under
    it orders them as the database saw them (Flashtap: an answer, a strike and an engine decision for one learner).

    Postgres: `SELECT ... FOR UPDATE`. SQLite has no row locks and ignores FOR UPDATE, so there a no-op `UPDATE` of the
    row takes the database's write lock, which serialises the same way (more coarsely). The returned row is read
    after the lock, so it is never older than the write that held the lock before."""
    mapper = inspect(model)
    cols = list(mapper.primary_key)
    keys: tuple[Any, ...] = cast("tuple[Any, ...]", key) if isinstance(key, tuple) else (key,)
    if len(keys) != len(cols):
        raise ValueError(f"{mapper.class_.__name__} has {len(cols)} primary key columns, {len(keys)} values given")
    where = [c == k for c, k in zip(cols, keys, strict=True)]
    if db.get_bind().dialect.name == "sqlite":
        db.execute(update(mapper.local_table).where(*where).values({cols[0].name: cols[0]}))
        query = select(model).where(*where)
    else:
        query = select(model).where(*where).with_for_update()
    row = db.scalars(query.execution_options(populate_existing=True)).one_or_none()
    if row is None:
        raise KeyError(f"No {mapper.class_.__name__} {key}.")
    return row
