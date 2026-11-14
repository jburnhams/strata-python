"""strata_python.clients.models against a stand-in strata-models over real HTTP (live_app): the same endpoints, the
same long-poll and lease steps, scripted results in the service's own shapes (copied from its runtimes). strata-models
tests the client against its real API in its own suite."""

import threading
import warnings

import pytest
from fastapi import FastAPI, HTTPException

from strata_python.clients import models as sm

OCR = {
    "width": 1200, "height": 1800, "text": "CHAPTER I\nIt was a dark night.",
    "lines": [
        {"text": "CHAPTER I", "box": [100, 80, 300, 40], "conf": 96.5, "block": 1,
         "words": [{"text": "CHAPTER", "box": [100, 80, 200, 40], "conf": 97.0}, {"text": "I", "box": [320, 80, 20, 40], "conf": 96.0}]},
        {"text": "It was a dark night.", "box": [100, 160, 600, 30], "conf": 91.2, "block": 2, "words": []},
    ],
    "engine": "tesseract 5.5", "lang": "eng", "psm": 3, "clean": [], "seconds": 1.2, "model": "tesseract", "pdf": "/tmp/p.pdf",
}  # fmt: skip
TRANSCRIPT = {
    "language": "en", "language_probability": 0.99, "duration": 3.5,
    "segments": [{"start": 0.0, "end": 1.5, "text": " Hello", "avg_logprob": -0.2, "no_speech_prob": 0.01,
                  "words": [{"start": 0.0, "end": 1.5, "word": " Hello", "probability": 0.9}]},
                 {"start": 1.5, "end": 3.5, "text": " world.", "avg_logprob": -0.3, "no_speech_prob": 0.02}],
}  # fmt: skip
FACES = {"width": 640, "height": 480, "faces": [{"bbox": [1, 2, 30, 40], "kps": [[5, 6]] * 5, "det_score": 0.87, "embedding": [0.1] * 4, "pose": [1, 2, 3]}, {"bbox": [9, 9, 20, 20]}]}  # fmt: skip
SPEECH = {"path": "/tmp/s.wav", "duration_s": 2.5, "sample_rate": 24000, "seconds": 0.8, "engine": "kokoro", "voice": "af_heart", "seed": 1, "speech_s": 2.1}  # fmt: skip
RESULTS = {"ocr": OCR, "transcribe": TRANSCRIPT, "faces": FACES, "speak": SPEECH}


class Service:
    """The stand-in: a job runs on its first poll and is done on the second; a lease is granted on its first poll."""

    def __init__(self) -> None:
        self.jobs: dict[int, dict] = {}
        self.leases: dict[int, dict] = {}
        self.heartbeats: list[int] = []
        self.released: list[int] = []
        self.fail_next = False
        self.grant = True  # False: a lease stays queued (the GPU is busy)
        self.refuse = False  # True: a lease request ends at once without the GPU
        self.lock = threading.Lock()
        self.app = FastAPI()
        a = self.app

        @a.get("/api/health")
        def health():
            return {"ok": True, "version": "9", "gpu": "stand-in"}

        @a.get("/api/registry")
        def registry():
            return {"roles": [{"key": "ocr", "model": "tesseract"}, {"key": "faces", "model": "buffalo_l"}]}

        @a.post("/api/jobs")
        def submit(body: dict):
            if body["role"] not in RESULTS:
                raise HTTPException(422, f"unknown role {body['role']!r}")
            with self.lock:
                jid = len(self.jobs) + 1
                self.jobs[jid] = {"id": jid, "status": "queued", **body, "polls": 0}
            return self._row(self.jobs[jid])

        @a.get("/api/jobs/{jid}")
        def poll(jid: int, wait: float = 0):
            j = self.jobs[jid]
            j["polls"] += 1
            if j["polls"] == 1:
                j["status"] = "running"
            elif self.fail_next:
                j.update(status="failed", error="CUDA out of memory")
            else:
                j.update(status="done", result=RESULTS[j["role"]])
            return self._row(j)

        @a.post("/api/leases")
        def lease(body: dict):
            lid = len(self.leases) + 100
            self.leases[lid] = {"id": lid, "status": "cancelled" if self.refuse else "queued", **body}
            return self.leases[lid]

        @a.get("/api/leases/{lid}")
        def lease_poll(lid: int, wait: float = 0):
            if self.grant:
                self.leases[lid]["status"] = "running"
            return self.leases[lid]

        @a.post("/api/leases/{lid}/heartbeat")
        def beat(lid: int):
            self.heartbeats.append(lid)
            return {"ok": True}

        @a.delete("/api/leases/{lid}")
        def release(lid: int):
            self.released.append(lid)
            self.leases[lid]["status"] = "done"
            return {"ok": True}

        @a.get("/api/broken")
        def broken():
            raise HTTPException(503, "starting")

    @staticmethod
    def _row(j: dict) -> dict:
        return {k: v for k, v in j.items() if k != "polls"}


@pytest.fixture
def service(live_app, monkeypatch):
    s = Service()
    with live_app(s.app) as base:
        monkeypatch.setenv("STRATA_MODELS_URL", base + "/")
        yield s


def test_run_and_the_shortcuts_send_what_the_service_expects(service, tmp_path):
    assert sm.available()
    assert sm.ocr("page.png", app="strata-scan", pdf="out.pdf", lang="eng") == OCR
    job = service.jobs[1]
    assert (job["role"], job["app"], job["priority"], job["params"]["lang"]) == ("ocr", "strata-scan", "normal", "eng")
    assert job["params"]["path"].endswith("page.png") and job["params"]["pdf"].endswith("out.pdf")  # noqa: PT018
    sm.transcribe("a.wav", app="strata-360", clip=(1, 2), priority="high")
    assert (service.jobs[2]["params"]["clip"], service.jobs[2]["priority"]) == ([1, 2], "high")
    sm.faces(None, app="bright-horizons", paths=["a.jpg"])
    assert service.jobs[3]["params"]["paths"][0].endswith("a.jpg")
    voice = tmp_path / "ref.wav"
    voice.write_bytes(b"")
    sm.speak("hi", app="x", voice=str(voice))
    assert service.jobs[4]["params"] == {"voice": str(voice), "text": "hi"}
    assert sm.role_model("faces") == "buffalo_l"
    assert sm.role_model("nope") is None


def test_typed_results():
    page = sm.OcrPage.from_json(OCR)
    assert (page.width, page.lines[0].words[0].box, page.lines[0].block, page.pdf) == (
        1200,
        (100, 80, 200, 40),
        1,
        "/tmp/p.pdf",
    )
    assert page.raw["psm"] == 3  # kept, though the class has no field for it
    t = sm.Transcript.from_json(TRANSCRIPT)
    assert (t.text, t.segments[0].words[0].probability, t.segments[1].words) == ("Hello world.", 0.9, [])
    f = sm.Faces.from_json(FACES)
    assert (f.faces[0].det_score, f.faces[0].kps[0], f.faces[1].embedding, f.faces[1].det_score) == (
        0.87,
        [5.0, 6.0],
        [],
        None,
    )
    assert [x.width for x in sm.Faces.many({"images": [FACES, FACES]})] == [640, 640]
    s = sm.Speech.from_json(SPEECH)
    assert (s.path, s.sample_rate, s.voice, s.raw["speech_s"]) == ("/tmp/s.wav", 24000, "af_heart", 2.1)


def test_failures(service, monkeypatch):
    service.fail_next = True
    with pytest.raises(sm.JobFailed, match="CUDA out of memory") as e:
        sm.run("faces", {}, app="x")
    assert e.value.job is not None
    assert e.value.job["status"] == "failed"
    assert sm.wait_job(1)["status"] == "failed"
    with pytest.raises(sm.JobFailed, match="unknown role"):
        sm.submit("nope", None, app="x")
    with pytest.raises(sm.Unavailable, match="answered 503"):
        sm._call("GET", "/api/broken")
    monkeypatch.setenv("STRATA_MODELS_URL", "http://127.0.0.1:9")  # the discard port: nothing listens
    assert not sm.available(timeout=1)
    with pytest.raises(sm.Unavailable, match="not reachable"):
        sm.run("faces", {}, app="x")


def test_wait_times_out(service):
    jid = sm.submit("faces", {}, app="x")["id"]
    with pytest.raises(TimeoutError, match="still running"):
        sm.wait(jid, timeout=0)


def test_gpu_lease_heartbeats_and_releases(service):
    with sm.gpu_lease("strata-stills", vram_gb=4, ttl_s=3, note="arena") as lease:
        assert lease is not None
        assert service.leases[lease.id]["vram_gb"] == 4
        assert lease.job["status"] == "running"
        lease._stop.wait(1.3)  # the heartbeat thread beats every ttl/3 (at least 1 s)
    assert lease.id in service.heartbeats
    assert service.released == [lease.id]


def test_gpu_lease_without_the_service(monkeypatch):
    monkeypatch.setenv("STRATA_MODELS_URL", "http://127.0.0.1:9")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with sm.gpu_lease("x") as lease:
            assert lease is None
    assert "without a lease" in str(caught[0].message)
    with pytest.raises(sm.Unavailable), sm.gpu_lease("x", required=True):
        pass


def test_acquire_times_out_and_withdraws(service):
    service.grant = False
    with pytest.raises(TimeoutError, match="not free"):
        sm.acquire("x", timeout=0.5, ttl_s=0.3)  # a short ttl, so the queued request is kept alive meanwhile
    assert service.released == [100]
    assert 100 in service.heartbeats


def test_a_lease_refused_and_a_finished_job_row(service):
    service.refuse = True
    with pytest.raises(sm.JobFailed, match="lease 100 was cancelled"):
        sm.acquire("x")
    jid = sm.submit("faces", {}, app="x")["id"]
    assert sm.wait_job(jid)["status"] == "done"
