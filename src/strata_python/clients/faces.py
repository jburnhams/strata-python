"""Client for strata-models' face store: one database of faces, embeddings and people for every app. Standard library
only, like `clients.models`, whose service URL (STRATA_MODELS_URL) and errors it shares.

    from strata_python.clients import faces as fs

    img = fs.index("bright-horizons", "photo:123", "/data/p.jpg", tags=["nursery"])  # detect, embed, store (idempotent)
    hits = fs.search(app="bright-horizons", face_id=img.faces[0].id, k=10)           # every app's faces, but opt-in tags
    hits = fs.search(app="strata-stills", path="/x.jpg", tags=["strata-stills"])     # only faces from these tags
    person = fs.create_identity(app="strata-360", app_fields={"name": "Me"}, app_ref="me")
    batch = fs.assign([f.id for f in img.faces[:1]], person.id, app="strata-360")    # confirmed, undoable as a batch
    groups = fs.cluster(app="bright-horizons", tags=["nursery"], include=["nursery"])  # unnamed faces, grouped

What strata-models owns: the faces, their embeddings (one per model, so a better model is a backfill), people
(`identities`: one per person across every app, with each app's own `fields` laid over the global ones when that app
reads), the decisions about which face is whom (`assign`, append-only, undone by batch), clustering, merge suggestions,
face crops and a medium copy of every indexed picture. What stays in the app: what a match means for it (auto-approve
thresholds, review screens' workflow, consent).

Tags: every picture carries its app's name as a tag, plus any the app adds. `search` and `cluster` cover every tag
by default except tags marked opt-in (`set_tag(tag, opt_in=True)`, e.g. other people's children), which are searched
only when named in `include`; `tags` narrows to pictures with any of those tags."""

from __future__ import annotations

import urllib.parse
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from strata_python.clients import models

Json = dict[str, Any]
Status = Literal["confirmed", "rejected", "suggested"]
Box = tuple[float, float, float, float]


def _q(**params: object) -> str:
    """A query string of the params that are set (lists repeat the key)."""
    pairs: list[tuple[str, str]] = []
    for k, v in params.items():
        if v is None:
            continue
        values: list[object] = list(cast("Iterable[object]", v)) if isinstance(v, (list, tuple)) else [v]
        for x in values:
            pairs.append((k, str(x).lower() if isinstance(x, bool) else str(x)))
    return ("?" + urllib.parse.urlencode(pairs)) if pairs else ""


def _list(v: object) -> list[Any]:
    return list(cast("Iterable[Any]", v)) if isinstance(v, (list, tuple)) else []


def _dict(v: object) -> Json:
    return dict(cast("Mapping[str, Any]", v)) if isinstance(v, dict) else {}


@dataclass(frozen=True)
class StoredFace:
    """A face in the store: its box in the original picture's upright pixels, and whom it is assigned to (the newest
    decision that was not undone; None when nobody yet)."""

    id: int
    image_id: int
    box: Box
    det_score: float
    kps: list[list[float]] = field(default_factory=lambda: [])
    pose: list[float] = field(default_factory=lambda: [])
    identity_id: int | None = None
    status: str | None = None
    raw: Json = field(default_factory=lambda: {}, repr=False)

    @classmethod
    def from_json(cls, d: Json) -> StoredFace:
        b = [float(x) for x in _list(d.get("box"))]
        return cls(
            id=int(d["id"]),
            image_id=int(d["image_id"]),
            box=(b[0], b[1], b[2], b[3]),
            det_score=float(d.get("det_score") or 0.0),
            kps=[[float(c) for c in _list(p)] for p in _list(d.get("kps"))],
            pose=[float(x) for x in _list(d.get("pose"))],
            identity_id=None if d.get("identity_id") is None else int(d["identity_id"]),
            status=d.get("status"),
            raw=d,
        )


@dataclass(frozen=True)
class IndexedImage:
    """A picture an app indexed: where its original is (`path`, `url`), its tags, and the faces found in it."""

    id: int
    app: str
    ref: str
    path: str | None
    url: str | None
    tags: list[str]
    width: int
    height: int
    faces: list[StoredFace]
    raw: Json = field(default_factory=lambda: {}, repr=False)

    @classmethod
    def from_json(cls, d: Json) -> IndexedImage:
        return cls(
            id=int(d["id"]),
            app=str(d["app"]),
            ref=str(d["ref"]),
            path=d.get("path"),
            url=d.get("url"),
            tags=[str(t) for t in _list(d.get("tags"))],
            width=int(d.get("width") or 0),
            height=int(d.get("height") or 0),
            faces=[StoredFace.from_json(f) for f in _list(d.get("faces"))],
            raw=d,
        )


@dataclass(frozen=True)
class Hit:
    face: StoredFace
    distance: float
    app: str
    ref: str
    raw: Json = field(default_factory=lambda: {}, repr=False)

    @classmethod
    def from_json(cls, d: Json) -> Hit:
        return cls(StoredFace.from_json(_dict(d["face"])), float(d["distance"]), str(d["app"]), str(d["ref"]), d)


@dataclass(frozen=True)
class Identity:
    """One person. `fields` is what the asking app sees (the global fields with its own laid over them), `name` the
    same for the name; `global_fields` and `apps` (app -> its own fields and its key for the person) are the parts."""

    id: int
    name: str | None
    fields: Json
    global_fields: Json
    apps: dict[str, Json]
    merged_into: int | None
    faces: int
    raw: Json = field(default_factory=lambda: {}, repr=False)

    @classmethod
    def from_json(cls, d: Json) -> Identity:
        return cls(
            id=int(d["id"]),
            name=d.get("name"),
            fields=_dict(d.get("fields")),
            global_fields=_dict(d.get("global_fields")),
            apps={str(k): _dict(v) for k, v in _dict(d.get("apps")).items()},
            merged_into=None if d.get("merged_into") is None else int(d["merged_into"]),
            faces=int(d.get("faces") or 0),
            raw=d,
        )


@dataclass(frozen=True)
class Cluster:
    faces: list[int]
    size: int
    raw: Json = field(default_factory=lambda: {}, repr=False)

    @classmethod
    def from_json(cls, d: Json) -> Cluster:
        f = [int(x) for x in _list(d.get("faces"))]
        return cls(f, int(d.get("size") or len(f)), d)


@dataclass(frozen=True)
class Suggested:
    """The known person a group of tracks looks like, and the share of its nearest confirmed faces that are theirs."""

    identity_id: int
    name: str | None
    share: float


@dataclass(frozen=True)
class TrackGroup:
    """Tracks (the app's keys) that look like one person, and who that may be (`group_tracks`)."""

    tracks: list[str]
    size: int
    suggested: Suggested | None = None
    raw: Json = field(default_factory=lambda: {}, repr=False)

    @classmethod
    def from_json(cls, d: Json) -> TrackGroup:
        keys = [str(k) for k in _list(d.get("tracks"))]
        s = d.get("suggested")
        who = None
        if isinstance(s, dict):
            s = cast("Json", s)
            who = Suggested(int(s["identity_id"]), s.get("name"), float(s.get("share") or 0.0))
        return cls(keys, int(d.get("size") or len(keys)), who, d)


@dataclass(frozen=True)
class TrackGroups:
    """`group_tracks`' answer: the groups, and the tracks left out because none of their faces is over the bar."""

    groups: list[TrackGroup]
    hidden: list[str]
    model: str | None = None

    @classmethod
    def from_json(cls, d: Json) -> TrackGroups:
        return cls(
            [TrackGroup.from_json(_dict(g)) for g in _list(d.get("groups"))],
            [str(k) for k in _list(d.get("hidden"))],
            d.get("model"),
        )


@dataclass(frozen=True)
class MergeSuggestion:
    a: int
    b: int
    distance: float
    raw: Json = field(default_factory=lambda: {}, repr=False)

    @classmethod
    def from_json(cls, d: Json) -> MergeSuggestion:
        return cls(int(d["a"]), int(d["b"]), float(d["distance"]), d)


def index(
    app: str,
    ref: str,
    path: str,
    *,
    tags: Sequence[str] = (),
    url: str | None = None,
    taken_at: float | None = None,
    priority: models.Priority = "low",
    det_thresh: float | None = None,
    faces: Sequence[Mapping[str, Any]] | None = None,
    model: str | None = None,
    width: int | None = None,
    height: int | None = None,
    timeout: float = 600,
) -> IndexedImage:
    """Detect, embed and store the faces of one picture (`ref` is the app's own key for it: a sha256, a photo id,
    `clip:frame`). Indexing the same `ref` again with the same model settings answers the stored picture without a
    job. `faces` gives detections already made (an import of embeddings made with `model`, boxes in the picture's own
    pixels), and then no job runs; `width` and `height` are the picture's, read from the file when not given."""
    body = {
        "app": app,
        "ref": ref,
        "path": path,
        "url": url,
        "tags": list(tags),
        "taken_at": taken_at,
        "priority": priority,
        "det_thresh": det_thresh,
        "faces": None if faces is None else [dict(f) for f in faces],
        "model": model,
        "width": width,
        "height": height,
    }
    return IndexedImage.from_json(models.request("POST", "/api/faces/images", body, timeout=timeout))


def image(image_id: int) -> IndexedImage:
    return IndexedImage.from_json(models.request("GET", f"/api/faces/images/{int(image_id)}"))


def images(app: str | None = None, *, tag: str | None = None, page: int = 500) -> Iterator[IndexedImage]:
    """Every picture `app` indexed (only those carrying `tag`, when given), newest first, with their faces and whom
    each is assigned to: read a page at a time, so a long list is not held at once."""
    before: object = None
    while True:
        d = models.request("GET", "/api/faces/images" + _q(app=app, tag=tag, limit=page, before=before))
        yield from (IndexedImage.from_json(i) for i in _list(d.get("items")))
        before = d.get("next")
        if not before:
            return


def find_image(app: str, ref: str) -> IndexedImage | None:
    """The picture `app` indexed as `ref`, or None."""
    items = _list(models.request("GET", "/api/faces/images" + _q(app=app, ref=ref)).get("items"))
    return IndexedImage.from_json(items[0]) if items else None


def search(
    *,
    app: str,
    face_id: int | None = None,
    embedding: Sequence[float] | None = None,
    path: str | None = None,
    k: int = 20,
    tags: Sequence[str] | None = None,
    include: Sequence[str] = (),
    named: bool | None = None,
    max_distance: float | None = None,
) -> list[Hit]:
    """The faces nearest one face (`face_id`), an embedding, or the largest face in a picture (`path`), closest
    first. `named` True or False keeps only faces with or without a confirmed person."""
    body = {
        "app": app,
        "face_id": face_id,
        "embedding": None if embedding is None else list(embedding),
        "path": path,
        "k": k,
        "tags": None if tags is None else list(tags),
        "include": list(include),
        "named": named,
        "max_distance": max_distance,
    }
    return [Hit.from_json(h) for h in _list(models.request("POST", "/api/faces/search", body).get("hits"))]


def cluster(
    *,
    app: str,
    tags: Sequence[str] | None = None,
    include: Sequence[str] = (),
    threshold: float | None = None,
    min_size: int = 2,
    limit: int = 5000,
    min_score: float | None = None,
) -> list[Cluster]:
    """Faces with no confirmed person, grouped (average linkage on cosine distance, `threshold` from the service's
    default for the model when None), largest group first. `limit` caps how many faces are grouped (the newest)."""
    body = {
        "app": app,
        "tags": None if tags is None else list(tags),
        "include": list(include),
        "threshold": threshold,
        "min_size": min_size,
        "limit": limit,
        "min_score": min_score,
    }
    return [Cluster.from_json(c) for c in _list(models.request("POST", "/api/faces/cluster", body).get("clusters"))]


def group_tracks(
    tracks: Mapping[str, Sequence[float]],
    *,
    app: str,
    faces: Mapping[str, Sequence[int]] | None = None,
    apart: Iterable[tuple[str, str]] = (),
    threshold: float | None = None,
    tags: Sequence[str] | None = None,
    include: Sequence[str] = (),
    model: str | None = None,
) -> TrackGroups:
    """A video's face tracks (key -> the track's embedding, `models.FaceTracks`) grouped into people, largest group
    first, never two tracks `apart` (seen in the same frame) in one group; each group with the known person the
    confirmed faces nearest it agree on (`suggested`), searched in `tags` and `include` as `search` does. `faces`
    names each track's stills as stored (`index`): a track none of whose faces is over the store's bar is answered in
    `hidden`, not grouped. Nothing is stored: the app keeps its tracks and indexes their stills to have them named."""
    body = {
        "app": app,
        "tracks": [
            {"key": str(k), "embedding": [float(x) for x in v], "faces": [int(f) for f in (faces or {}).get(k, ())]}
            for k, v in tracks.items()
        ],
        "apart": [[str(a), str(b)] for a, b in apart],
        "threshold": threshold,
        "tags": None if tags is None else list(tags),
        "include": list(include),
        "model": model,
    }
    return TrackGroups.from_json(models.request("POST", "/api/faces/group-tracks", body))


def identities(*, app: str | None = None, q: str | None = None, limit: int = 200) -> list[Identity]:
    d = models.request("GET", "/api/identities" + _q(app=app, q=q, limit=limit))
    return [Identity.from_json(i) for i in _list(d.get("items"))]


def identity(identity_id: int, *, app: str | None = None) -> Identity:
    """One person as `app` sees it (a merged one answers the identity it was merged into)."""
    return Identity.from_json(models.request("GET", f"/api/identities/{int(identity_id)}" + _q(app=app)))


def create_identity(
    *,
    name: str | None = None,
    fields: Mapping[str, Any] | None = None,
    app: str | None = None,
    app_fields: Mapping[str, Any] | None = None,
    app_ref: str | None = None,
) -> Identity:
    body = {
        "name": name,
        "fields": dict(fields or {}),
        "app": app,
        "app_fields": None if app_fields is None else dict(app_fields),
        "app_ref": app_ref,
    }
    return Identity.from_json(models.request("POST", "/api/identities", body))


def update_identity(identity_id: int, *, name: str | None = None, fields: Mapping[str, Any] | None = None) -> Identity:
    """Change the global name or fields (fields are merged in; a None value removes that field)."""
    body = {"name": name, "fields": None if fields is None else dict(fields)}
    return Identity.from_json(models.request("PATCH", f"/api/identities/{int(identity_id)}", body))


def set_app_fields(identity_id: int, app: str, fields: Mapping[str, Any], *, app_ref: str | None = None) -> Identity:
    """Replace `app`'s own fields for this person (they override the global fields when `app` reads), and its key
    for the person (`app_ref`: a bright-horizons child id, strata-360's `me`)."""
    body = {"fields": dict(fields), "app_ref": app_ref}
    return Identity.from_json(
        models.request("PUT", f"/api/identities/{int(identity_id)}/apps/{urllib.parse.quote(app, safe='')}", body)
    )


def find_identity(app: str, app_ref: str) -> Identity | None:
    """The person `app` knows as `app_ref`, or None."""
    items = _list(models.request("GET", "/api/identities" + _q(app=app, app_ref=app_ref)).get("items"))
    return Identity.from_json(items[0]) if items else None


def assign(
    face_ids: Sequence[int],
    identity_id: int | None,
    *,
    app: str,
    status: Status = "confirmed",
    source: str = "human",
    confidence: float | None = None,
    note: str | None = None,
) -> int:
    """Record that these faces are (or, `rejected`, are not) this person, as one batch; returns the batch id for
    `undo`. `identity_id` None with `rejected` says a face is not a face at all."""
    body = {
        "face_ids": [int(f) for f in face_ids],
        "identity_id": identity_id,
        "app": app,
        "status": status,
        "source": source,
        "confidence": confidence,
        "note": note,
    }
    return int(models.request("POST", "/api/faces/assign", body)["batch"])


def undo(batch_id: int) -> None:
    models.request("POST", f"/api/faces/batches/{int(batch_id)}/undo", {})


def merge(identity_id: int, into: int, *, app: str) -> Identity:
    """Merge one person into another (a person decides: the service never merges on its own). Undone by `unmerge`."""
    return Identity.from_json(
        models.request("POST", f"/api/identities/{int(identity_id)}/merge", {"into": int(into), "app": app})
    )


def unmerge(identity_id: int) -> Identity:
    return Identity.from_json(models.request("POST", f"/api/identities/{int(identity_id)}/unmerge", {}))


def not_same(a: int, b: int) -> None:
    """Two people a person has said are different: never suggested for a merge again."""
    models.request("POST", "/api/identities/not-same", {"a": int(a), "b": int(b)})


def merge_suggestions(*, limit: int = 50) -> list[MergeSuggestion]:
    d = models.request("GET", "/api/identities/merge-suggestions" + _q(limit=limit))
    return [MergeSuggestion.from_json(s) for s in _list(d.get("items"))]


def set_tag(tag: str, *, opt_in: bool) -> None:
    models.request("PUT", f"/api/faces/tags/{urllib.parse.quote(tag, safe='')}", {"opt_in": opt_in})


def tags() -> list[Json]:
    """Every tag with its picture count and whether it is opt-in."""
    return [_dict(t) for t in _list(models.request("GET", "/api/faces/tags").get("items"))]


def crop_url(face_id: int, px: int = 160, pad: float = 1.6) -> str:
    """Where a face's crop is served (a cached WebP), for an app's own page to show."""
    return f"{models.url()}/api/faces/{int(face_id)}/crop" + _q(px=px, pad=pad)


def picture_url(image_id: int) -> str:
    """Where the stored medium copy of an indexed picture is served (1600 px WebP), to show a face in its picture."""
    return f"{models.url()}/api/faces/images/{int(image_id)}/picture"
