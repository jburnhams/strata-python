import datetime

import pytest
from pydantic import ValidationError

from strata_python import contracts as c


def test_times_are_aware_and_iso_on_the_wire():
    assert c.utc(None) is None
    t = c.utc(0)
    assert t == datetime.datetime(1970, 1, 1, tzinfo=datetime.UTC)
    task = c.Task(id=1, kind="ocr", state="done", created_at=t)
    assert task.model_dump(mode="json")["created_at"] == "1970-01-01T00:00:00Z"


def test_task_state_is_one_of_the_known_words():
    with pytest.raises(ValidationError):
        c.Task(id=1, kind="x", state="finished", created_at=c.utc(0))  # the apps' spelling drift stops here
    assert {"done", "failed", "cancelled"} == c.FINISHED


def test_progress_fraction():
    assert c.Progress(done=3, total=4).fraction == 0.75
    assert c.Progress(done=3).fraction is None
    assert c.Progress(done=9, total=4).fraction == 1.0


def test_page_is_generic_and_health_and_system_keep_extra_fields():
    page = c.Page[c.FileRef](items=[c.FileRef(sha256="a" * 64, size=1, mtime=0, root="photos", rel="a/b.jpg")])
    assert page.next is None
    assert page.items[0].rel == "a/b.jpg"
    assert c.Health(ok=True, version="1", gpu="cuda").model_dump()["gpu"] == "cuda"


def test_file_ref_takes_lower_case_hex_only():
    with pytest.raises(ValidationError):
        c.FileRef(sha256="A" * 64, size=1, mtime=0, root="r", rel="x")
    with pytest.raises(ValidationError):
        c.FileRef(sha256="a" * 63, size=1, mtime=0, root="r", rel="x")


def test_judgment_winner():
    j = c.Judgment(a="x.jpg", b="y.jpg", winner="tie", rater="model:judge", at=c.utc(0))
    assert j.undone_at is None
    with pytest.raises(ValidationError):
        c.Judgment(a="x", b="y", winner="x", rater="r", at=c.utc(0))
