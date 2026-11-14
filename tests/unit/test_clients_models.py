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
ANSWER = {"text": '{"scene": "beach"}', "json": {"scene": "beach"}, "tokens_in": 812, "tokens_out": 9, "seconds": 1.4, "model": "qwen3.5-9b-vl", "peak_vram_mb": 7100}  # fmt: skip
IMAGE = {"path": "/tmp/g.png", "width": 1024, "height": 768, "seed": 1234, "steps": 4, "seconds": 9.5, "peak_vram_mb": 9000, "model": "flux3-klein"}  # fmt: skip
EMBED = {"images": [[0.6, 0.8]], "texts": [[1.0, 0.0], [0.0, 1.0]], "dim": 2}
FACE_KIND = {"faces": [{"real": True, "kind": "person", "probs": {"person": 0.97, "statue": 0.03}}, {"real": False, "kind": "statue", "probs": {}}], "head": "trained head.npz"}  # fmt: skip
CLEAN = {"path": "/tmp/c.wav", "duration_s": 4.0, "sample_rate": 48000, "seconds": 0.7, "engine": "dfn3", "model": "deepfilternet3", "lufs_in": -31.2, "lufs_out": -14.0, "removed_db": 6.5}  # fmt: skip
QUALITY = {"dnsmos": {"sig": 3.4, "bak": 3.9, "ovrl": 3.0}, "squim": {"stoi": 0.93, "pesq": 2.8, "si_sdr": 14.2}, "duration_s": 4.0}  # fmt: skip
VIDEO = {
    "path": "/tmp/v.mp4", "width": 1440, "height": 1080, "fps": 50.0, "frames": 400, "seconds": 61.0, "engine": "ffmpeg", "model": "video-lanczos",
    "crops": [{"id": "face", "label": "Gran", "box": [100, 50, 80, 80], "path": "/tmp/v-face.mp4", "stills": ["/tmp/v-face-3.png"]}],
    "stills": ["/tmp/v-1.png"], "enhance_s": 40.0, "quality": {"ssim": 0.91, "skipped": "no numpy"},
}  # fmt: skip
RESULTS = {
    "ocr": OCR, "transcribe": TRANSCRIPT, "transcribe-fast": TRANSCRIPT, "faces": FACES, "speak": SPEECH,
    "vision-language": ANSWER, "text": ANSWER, "judge-text": ANSWER, "judge-vision": ANSWER, "judge-audio": ANSWER,
    "image-generate": IMAGE, "image-embed": EMBED, "face-kind": FACE_KIND, "denoise-audio": CLEAN, "audio-quality": QUALITY,
    "video-enhance": VIDEO,
}  # fmt: skip


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


def test_every_role_the_service_runs_has_a_result_here():
    assert set(sm.ROLES) == set(RESULTS)


def test_the_other_shortcuts_send_what_the_service_expects(service):
    sm.ask("What is here?", app="strata-stills", image="a.jpg", schema={"type": "object"})
    sm.ask(None, app="x", role="text", preset="image-prompt-bfl", context={"subject": "a kiln"})
    sm.generate_image("a kiln at dusk", app="x", ref_images=["r.png"], seed=7)
    sm.embed("strata-stills", paths=["a.jpg"], texts=["a beach"], array={"path": "p.npz", "bgr": True})
    sm.face_kind("a.jpg", app="strata-stills", boxes=[(1, 2, 30, 40)])
    sm.face_kind(None, app="x", array={"path": "crops.npy"})
    sm.denoise_audio("talk.wav", app="strata-360", clip=(0, 5), lufs=None)
    sm.audio_quality("talk.wav", app="x", reference="clean.wav", metrics=["intrusive"])
    sm.audio_quality("talk.wav", app="x")
    sm.enhance_video("tape.mkv", app="strata-tapes", size=[1440, 1080])
    jobs = [service.jobs[i] for i in sorted(service.jobs)]
    assert [j["role"] for j in jobs] == [
        "vision-language", "text", "image-generate", "image-embed", "face-kind", "face-kind", "denoise-audio",
        "audio-quality", "audio-quality", "video-enhance",
    ]  # fmt: skip
    p = [j["params"] for j in jobs]
    assert p[0]["prompt"] == "What is here?"
    assert p[0]["image"].endswith("a.jpg")
    assert p[0]["image"] != "a.jpg"  # made absolute: the service resolves paths from its own folder
    assert "prompt" not in p[1]
    assert p[1]["preset"] == "image-prompt-bfl"
    assert (p[2]["prompt"], p[2]["seed"], p[2]["ref_images"][0].endswith("r.png")) == ("a kiln at dusk", 7, True)
    assert (p[3]["texts"], p[3]["array"]["bgr"], p[3]["array"]["path"].endswith("p.npz")) == (["a beach"], True, True)
    assert p[4]["boxes"] == [[1, 2, 30, 40]]
    assert "boxes" not in p[5]
    assert (p[6]["clip"], p[6]["lufs"]) == ([0, 5], None)
    assert (p[7]["metrics"], p[7]["reference"].endswith("clean.wav")) == (["intrusive"], True)
    assert set(p[8]) == {"path"}
    assert p[9]["size"] == [1440, 1080]


def test_the_other_typed_results():
    a = sm.Answer.from_json(ANSWER)
    assert (a.json, a.tokens_in, a.cost_usd, a.raw["peak_vram_mb"]) == ({"scene": "beach"}, 812, None, 7100)
    assert sm.Answer.from_json({"text": "hi", "cost_usd": 0.002, "provider": "claude"}).provider == "claude"
    g = sm.GeneratedImage.from_json(IMAGE)
    assert (g.path, g.seed, g.steps) == ("/tmp/g.png", 1234, 4)
    e = sm.Embeddings.from_json(EMBED)
    assert (e.images, len(e.texts), e.dim, e.out) == ([[0.6, 0.8]], 2, 2, None)
    to_file = sm.Embeddings.from_json({"out": "/tmp/e.npz", "images": 3, "texts": 0, "dim": 512})
    assert (to_file.images, to_file.out, to_file.raw["images"]) == ([], "/tmp/e.npz", 3)
    k = sm.FaceKinds.from_json(FACE_KIND)
    assert [(f.real, f.kind) for f in k.faces] == [(True, "person"), (False, "statue")]
    assert (k.faces[0].probs["person"], k.head) == (0.97, "trained head.npz")
    c = sm.CleanAudio.from_json(CLEAN)
    assert (c.sample_rate, c.lufs_out, c.removed_db) == (48000, -14.0, 6.5)
    q = sm.AudioQuality.from_json(QUALITY)
    assert (q.dnsmos["ovrl"], q.squim["si_sdr"], q.intrusive) == (3.0, 14.2, {})
    v = sm.EnhancedVideo.from_json(VIDEO)
    assert (v.fps, v.frames, v.crops[0].box, v.crops[0].stills, v.quality["skipped"]) == (
        50.0, 400, (100, 50, 80, 80), ["/tmp/v-face-3.png"], "no numpy",
    )  # fmt: skip
