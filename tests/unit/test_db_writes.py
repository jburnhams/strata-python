"""strata_python.db.writes (issue #74) on real SQLite files and a real throwaway Postgres: a write taken once by its
client-made id, a retry answered with the first result, a changed retry refused, and writes queued on a row lock by
two sessions at once."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import ForeignKey, MetaData, String
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, MappedAsDataclass, Session, mapped_column

from strata_python import web
from strata_python.db import NAMING_CONVENTION, ConflictingWrite, guard, insert_once, lock_row, testserver
from strata_python.db.scratch import scratch_database
from strata_python.db.testing import scratch_engine
from strata_python.tasks import Conflict

# As in test_db.py: CI sets STRATA_TEST_PG=1, so a missing Postgres fails there instead of skipping.
PG = os.environ.get("STRATA_TEST_PG") == "1" or (os.name != "nt" and os.geteuid() != 0 and testserver.available())
PG = PG or bool(os.environ.get(guard.TEST_ADMIN_URL))
BACKENDS = ["sqlite", pytest.param("postgres", marks=pytest.mark.skipif(not PG, reason="no throwaway Postgres"))]


class Base(MappedAsDataclass, DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Learner(Base):
    __tablename__ = "learners"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    nickname: Mapped[str]


class Answer(Base):
    __tablename__ = "answers"
    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    learner_id: Mapped[str] = mapped_column(ForeignKey("learners.id"))
    choice: Mapped[str]
    at: Mapped[datetime]
    correct: Mapped[bool]


class Pair(Base):
    __tablename__ = "pairs"
    a: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    b: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    note: Mapped[str]


AT = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)


def utc(t: datetime) -> datetime:
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)


@pytest.fixture(params=BACKENDS)
def engine(request: pytest.FixtureRequest) -> Iterator[Engine]:
    with scratch_database(request.param == "postgres") as url:
        eng = scratch_engine(url)
        Base.metadata.create_all(eng)  # a test of the helpers, not of an app's migrations
        with Session(eng) as s:
            s.add(Learner(id="L1", nickname="Fox"))
            s.commit()
        try:
            yield eng
        finally:
            eng.dispose()


def record(
    db: Session, event_id: str, choice: str, at: datetime = AT, judged: list[str] | None = None
) -> tuple[Answer, bool]:
    def make() -> Answer:
        if judged is not None:
            judged.append(event_id)  # the app's own work: runs only for a write not seen yet
        return Answer(event_id=event_id, learner_id="L1", choice=choice, at=at, correct=choice == "cat")

    return insert_once(db, Answer, event_id, make, same=lambda old: old.choice == choice and utc(old.at) == at)


def test_a_write_is_taken_once_and_a_retry_gets_the_first_answer(engine: Engine) -> None:
    judged: list[str] = []
    with Session(engine) as db:
        row, duplicate = record(db, "e1", "cat", judged=judged)
        assert not duplicate
        assert row.correct
        db.commit()
    with Session(engine) as db:
        row, duplicate = record(db, "e1", "cat", judged=judged)
        assert duplicate
        assert row.correct
        assert row.choice == "cat"
        assert judged == ["e1"]  # the retry was answered before the app judged it again
    with Session(engine) as db, pytest.raises(ConflictingWrite, match="never rewritten") as refused:
        record(db, "e1", "dog")
    assert isinstance(refused.value, Conflict)
    assert refused.value.payload == "e1"
    with Session(engine) as db, pytest.raises(ConflictingWrite):
        record(db, "e1", "cat", at=AT.replace(minute=1))  # a changed time is a changed event
    with Session(engine) as db:
        kept = db.get(Answer, "e1")
        assert kept is not None
        assert (kept.choice, utc(kept.at)) == ("cat", AT)  # the refused retries changed nothing


def test_two_tries_at_once_store_one_row(engine: Engine) -> None:
    """Without a lock to queue them, both tries see no row; the second's insert fails on the key once the first
    commits, and is answered as a duplicate of what the first stored."""
    first, second = Session(engine), Session(engine)
    try:
        assert first.get(Answer, "e2") is None
        assert second.get(Answer, "e2") is None
        _, duplicate = record(first, "e2", "cat")
        assert not duplicate
        result: list[tuple[Answer, bool] | BaseException] = []

        def other() -> None:
            try:
                result.append(record(second, "e2", "cat"))
            except BaseException as e:
                result.append(e)

        t = threading.Thread(target=other)
        t.start()
        time.sleep(0.3)  # the second is waiting on the first's write
        first.commit()
        t.join(30)
        got = result[0]
        assert not isinstance(got, BaseException), got
        assert got[1]
        assert got[0].choice == "cat"
    finally:
        first.close()
        second.close()


def test_a_failure_in_make_that_is_not_a_duplicate_is_raised(engine: Engine) -> None:
    from sqlalchemy.exc import IntegrityError

    with Session(engine) as db, pytest.raises(IntegrityError):
        insert_once(
            db,
            Answer,
            "e3",
            lambda: Answer(event_id="e3", learner_id="nobody", choice="x", at=AT, correct=False),
            same=lambda _: True,
        )


def test_lock_row_serialises_writes_per_row(engine: Engine) -> None:
    order: list[str] = []
    holding = threading.Event()

    def writer(name: str, hold_s: float) -> None:
        with Session(engine) as db:
            learner = lock_row(db, Learner, "L1")
            holding.set()
            order.append(f"{name} locked")
            time.sleep(hold_s)
            learner.nickname = name
            order.append(f"{name} done")
            db.commit()

    a = threading.Thread(target=writer, args=("a", 0.5))
    a.start()
    assert holding.wait(10)
    b = threading.Thread(target=writer, args=("b", 0))
    b.start()
    a.join(30)
    b.join(30)
    assert order == ["a locked", "a done", "b locked", "b done"]  # b waited for a's transaction to end
    with Session(engine) as db:
        assert lock_row(db, Learner, "L1").nickname == "b"  # read after the lock: never older than the last write


def test_lock_row_needs_the_row_and_the_whole_key(engine: Engine) -> None:
    with Session(engine) as db:
        db.add(Pair(a=1, b=2, note="x"))
        db.commit()
        assert lock_row(db, Pair, (1, 2)).note == "x"
        with pytest.raises(KeyError, match="No Learner nobody"):
            lock_row(db, Learner, "nobody")
        with pytest.raises(ValueError, match="2 primary key columns, 1 values"):
            lock_row(db, Pair, 1)


def test_a_conflict_and_a_missing_row_answer_409_and_404(engine: Engine) -> None:
    app = FastAPI()
    web.add_error_handlers(app)

    @app.post("/answers/{event_id}")
    def post(event_id: str, choice: str) -> dict[str, bool]:  # pyright: ignore[reportUnusedFunction]
        with Session(engine) as db:
            lock_row(db, Learner, "L1")
            _, duplicate = record(db, event_id, choice)
            db.commit()
            return {"duplicate": duplicate}

    @app.post("/learners/{learner_id}/lock")
    def lock(learner_id: str) -> None:  # pyright: ignore[reportUnusedFunction]
        with Session(engine) as db:
            lock_row(db, Learner, learner_id)

    client = TestClient(app)
    assert client.post("/answers/e4", params={"choice": "cat"}).json() == {"duplicate": False}
    assert client.post("/answers/e4", params={"choice": "cat"}).json() == {"duplicate": True}
    conflict = client.post("/answers/e4", params={"choice": "dog"})
    assert conflict.status_code == 409
    assert conflict.json()["payload"] == "e4"
    assert client.post("/learners/nobody/lock").status_code == 404
