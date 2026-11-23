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


def test_api_model_makes_defaulted_fields_required_in_the_response_schema():
    """Issue #36: a field the server always sends must not come out optional in the generated TypeScript."""

    class Plain(c.BaseModel):
        n: int = 0

    class Exact(c.ApiModel):
        n: int = 0

    assert "required" not in Plain.model_json_schema(mode="serialization")
    assert Exact.model_json_schema(mode="serialization")["required"] == ["n"]
    assert Exact().n == 0  # still optional to construct


@pytest.mark.parametrize(
    ("model", "fields"),
    [
        (c.Page[int], {"items", "next", "total"}),
        (c.Health, {"ok", "version", "commit"}),
        (c.SystemInfo, {"version", "commit", "python", "platform", "started_at", "folders", "services"}),
        (c.Folder, {"name", "path", "exists", "free_bytes", "total_bytes"}),
        (c.ServiceStatus, {"name", "url", "ok", "version", "detail"}),
        (c.ErrorBody, {"detail", "payload"}),
        (c.Progress, {"done", "total", "text", "stage", "at"}),
    ],
)
def test_shared_responses_list_every_field_they_send_as_required(model, fields):
    """Issue #45: `Page.next` came out `next?: string | null` in the web app's types though it is always sent."""
    assert set(model.model_json_schema(mode="serialization")["required"]) == fields


def test_shared_responses_in_the_served_schema(live_app):
    import json
    import urllib.request

    from fastapi import FastAPI

    app = FastAPI()

    @app.get("/page", response_model=c.Page[c.Task])
    def page() -> c.Page[c.Task]:
        return c.Page(items=[])

    with live_app(app) as base, urllib.request.urlopen(base + "/openapi.json") as r:
        schemas = json.load(r)["components"]["schemas"]
    assert "next" in schemas["Page_Task_"]["required"]
    assert {"error", "progress", "attempts"} <= set(schemas["Task"]["required"])
    assert c.Health(ok=True, version="1", gpu="x").model_dump()["gpu"] == "x"  # extra fields still allowed
