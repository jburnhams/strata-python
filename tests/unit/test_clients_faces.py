"""strata_python.clients.faces against a scripted server over real HTTP: each call sends what strata-models' face
store expects (paths, methods, bodies, query strings) and reads its answers into the typed shapes. strata-models
tests the client against its real API in its own suite."""

import pytest

from strata_python.clients import faces as fs
from strata_python.clients import models as sm

FACE = {
    "id": 7,
    "image_id": 3,
    "box": [10, 20, 50, 70],
    "det_score": 0.91,
    "kps": [[1, 2]],
    "pose": [1.0, 2.0, 3.0],
    "identity_id": 4,
    "status": "confirmed",
}
IMAGE = {
    "id": 3,
    "app": "myapp",
    "ref": "p:1",
    "path": "/data/p.jpg",
    "url": None,
    "tags": ["myapp", "kids"],
    "width": 800,
    "height": 600,
    "faces": [FACE],
}
PERSON = {
    "id": 4,
    "name": "Ada",
    "fields": {"name": "Ada", "kind": "child"},
    "global_fields": {},
    "apps": {"myapp": {"fields": {"kind": "child"}, "app_ref": "c1"}},
    "merged_into": None,
    "faces": 12,
}


@pytest.fixture
def server(http_server, monkeypatch):
    monkeypatch.setenv("STRATA_MODELS_URL", http_server.url)
    return http_server


def test_index_and_read_back(server):
    server.route("POST", "/api/faces/images", IMAGE)
    server.route("GET", "/api/faces/images/3", IMAGE)
    server.route("GET", "/api/faces/images", {"items": [IMAGE]})
    img = fs.index("myapp", "p:1", "/data/p.jpg", tags=["kids"], taken_at=1.5)
    body = server.requests[0].json()
    assert body["tags"] == ["kids"]
    assert body["priority"] == "low"
    assert body["faces"] is None
    assert body["taken_at"] == 1.5
    assert (img.id, img.tags, img.faces[0].box, img.faces[0].identity_id, img.faces[0].kps) == (
        3,
        ["myapp", "kids"],
        (10.0, 20.0, 50.0, 70.0),
        4,
        [[1.0, 2.0]],
    )
    assert fs.image(3).ref == "p:1"
    found = fs.find_image("myapp", "p:1")
    assert found is not None
    assert found.path == "/data/p.jpg"
    assert server.requests[-1].query == {"app": ["myapp"], "ref": ["p:1"]}
    fs.index("myapp", "p:2", "/data/q.jpg", faces=[{"box": [1, 2, 3, 4], "embedding": [0.1]}], model="buffalo_l")
    assert server.requests[-1].json()["faces"] == [{"box": [1, 2, 3, 4], "embedding": [0.1]}]


def test_find_image_answers_none_when_missing(server):
    server.route("GET", "/api/faces/images", {"items": []})
    assert fs.find_image("myapp", "nope") is None


def test_search_and_cluster(server):
    server.route(
        "POST", "/api/faces/search", {"hits": [{"face": FACE, "distance": 0.25, "app": "myapp", "ref": "p:1"}]}
    )
    server.route("POST", "/api/faces/cluster", {"clusters": [{"faces": [1, 2, 3], "size": 3}]})
    hits = fs.search(app="myapp", face_id=7, include=["kids"], named=False)
    assert server.requests[0].json() == {
        "app": "myapp",
        "face_id": 7,
        "embedding": None,
        "path": None,
        "k": 20,
        "tags": None,
        "include": ["kids"],
        "named": False,
        "max_distance": None,
    }
    assert (hits[0].face.id, hits[0].distance, hits[0].app) == (7, 0.25, "myapp")
    groups = fs.cluster(app="myapp", tags=["kids"], include=["kids"], min_size=3)
    assert server.requests[1].json()["min_size"] == 3
    assert (groups[0].faces, groups[0].size) == ([1, 2, 3], 3)


def test_identities(server):
    server.route("GET", "/api/identities", {"items": [PERSON]})
    server.route("GET", "/api/identities/4", PERSON)
    server.route("POST", "/api/identities", PERSON)
    server.route("PATCH", "/api/identities/4", PERSON)
    server.route("PUT", "/api/identities/4/apps/my%20app", PERSON)
    server.route("POST", "/api/identities/4/merge", PERSON)
    server.route("POST", "/api/identities/4/unmerge", PERSON)
    server.route("POST", "/api/identities/not-same", {})
    server.route("GET", "/api/identities/merge-suggestions", {"items": [{"a": 4, "b": 9, "distance": 0.3}]})
    people = fs.identities(app="myapp", q="Ad")
    assert server.requests[-1].query == {"app": ["myapp"], "q": ["Ad"], "limit": ["200"]}
    assert (people[0].name, people[0].fields["kind"], people[0].apps["myapp"]["app_ref"], people[0].faces) == (
        "Ada",
        "child",
        "c1",
        12,
    )
    assert fs.identity(4, app="myapp").id == 4
    found = fs.find_identity("myapp", "c1")
    assert found is not None
    assert server.requests[-1].query == {"app": ["myapp"], "app_ref": ["c1"]}
    fs.create_identity(app="myapp", app_fields={"kind": "child"}, app_ref="c1")
    assert server.requests[-1].json() == {
        "name": None,
        "fields": {},
        "app": "myapp",
        "app_fields": {"kind": "child"},
        "app_ref": "c1",
    }
    fs.update_identity(4, name="Ada L", fields={"born": None})
    assert server.requests[-1].json() == {"name": "Ada L", "fields": {"born": None}}
    fs.set_app_fields(4, "my app", {"name": "A"}, app_ref="c1")
    assert server.requests[-1].path == "/api/identities/4/apps/my%20app"
    assert server.requests[-1].json() == {"fields": {"name": "A"}, "app_ref": "c1"}
    fs.merge(4, 9, app="myapp")
    assert server.requests[-1].json() == {"into": 9, "app": "myapp"}
    fs.unmerge(4)
    fs.not_same(4, 9)
    assert server.requests[-1].json() == {"a": 4, "b": 9}
    s = fs.merge_suggestions(limit=5)
    assert (s[0].a, s[0].b, s[0].distance) == (4, 9, 0.3)


def test_assign_undo_tags_and_urls(server):
    server.route("POST", "/api/faces/assign", {"batch": 11})
    server.route("POST", "/api/faces/batches/11/undo", {})
    server.route("PUT", "/api/faces/tags/kids", {})
    server.route("GET", "/api/faces/tags", {"items": [{"tag": "kids", "images": 3, "opt_in": True}]})
    assert fs.assign([7, 8], 4, app="myapp", source="auto", confidence=0.97) == 11
    assert server.requests[0].json() == {
        "face_ids": [7, 8],
        "identity_id": 4,
        "app": "myapp",
        "status": "confirmed",
        "source": "auto",
        "confidence": 0.97,
        "note": None,
    }
    fs.undo(11)
    fs.set_tag("kids", opt_in=True)
    assert server.requests[-1].json() == {"opt_in": True}
    assert fs.tags() == [{"tag": "kids", "images": 3, "opt_in": True}]
    assert fs.crop_url(7, px=96) == f"{server.url}/api/faces/7/crop?px=96&pad=1.6"
    assert fs.picture_url(3) == f"{server.url}/api/faces/images/3/picture"


def test_errors_are_the_models_clients(server):
    server.route("POST", "/api/faces/search", {"detail": "no such face: 99"}, status=404)
    with pytest.raises(sm.JobFailed, match="no such face"):
        fs.search(app="myapp", face_id=99)
    server.route("GET", "/api/faces/images/5", {"detail": "down"}, status=503)
    with pytest.raises(sm.Unavailable):
        fs.image(5)
