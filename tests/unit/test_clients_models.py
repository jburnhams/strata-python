"""strata_python.clients.models against the shared stand-in strata-models (strata_python.testing.FakeModels) over real
HTTP: the same endpoints, the same long-poll and lease steps, results in the service's own shapes (copied from its
runtimes). strata-models tests the client against its real API in its own suite."""

import warnings

import pytest

from strata_python.clients import models as sm
from strata_python.testing import MODELS_RESULTS as RESULTS
from strata_python.testing import FakeModels

OCR, TRANSCRIPT, FACES, SPEECH = RESULTS["ocr"], RESULTS["transcribe"], RESULTS["faces"], RESULTS["speak"]
ANSWER, IMAGE, EMBED, FACE_KIND = (
    RESULTS["vision-language"],
    RESULTS["image-generate"],
    RESULTS["image-embed"],
    RESULTS["face-kind"],
)
CLEAN, QUALITY, VIDEO = RESULTS["denoise-audio"], RESULTS["audio-quality"], RESULTS["video-enhance"]


@pytest.fixture
def service(fake_models: FakeModels) -> FakeModels:
    return fake_models


def test_run_and_the_shortcuts_send_what_the_service_expects(service, tmp_path):
    assert sm.available()
    assert sm.ocr("page.png", app="strata-scan", pdf="out.pdf", lang="eng") == OCR
    job = service.jobs[0]
    assert (job["role"], job["app"], job["priority"], job["params"]["lang"]) == ("ocr", "strata-scan", "normal", "eng")
    assert job["params"]["path"].endswith("page.png") and job["params"]["pdf"].endswith("out.pdf")  # noqa: PT018
    sm.transcribe("a.wav", app="strata-360", clip=(1, 2), priority="high")
    assert (service.jobs[1]["params"]["clip"], service.jobs[1]["priority"]) == ([1, 2], "high")
    sm.faces(None, app="bright-horizons", paths=["a.jpg"])
    assert service.jobs[2]["params"]["paths"][0].endswith("a.jpg")
    voice = tmp_path / "ref.wav"
    voice.write_bytes(b"")
    sm.speak("hi", app="x", voice=str(voice))
    assert service.jobs[3]["params"] == {"voice": str(voice), "text": "hi"}
    service.models["faces"] = "buffalo_l"
    assert sm.role_model("faces") == "buffalo_l"
    assert sm.role_model("nope") is None


def test_typed_results():
    page = sm.OcrPage.from_json(OCR)
    assert (page.width, page.lines[0].words[0].box, page.lines[0].block, page.pdf) == (
        1200,
        (100, 80, 200, 40),
        1,
        "/out/p.pdf",
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
    assert (s.path, s.sample_rate, s.voice, s.raw["speech_s"]) == ("/out/s.wav", 24000, "af_heart", 2.1)


def test_failures(service, monkeypatch):
    service.fail("CUDA out of memory")
    with pytest.raises(sm.JobFailed, match="CUDA out of memory") as e:
        sm.run("faces", {}, app="x")
    assert e.value.job is not None
    assert e.value.job["status"] == "failed"
    assert sm.wait_job(1)["status"] == "failed"
    with pytest.raises(sm.JobFailed, match="unknown role"):
        sm.submit("nope", None, app="x")
    service.down = True
    with pytest.raises(sm.Unavailable, match="answered 503"):
        sm.run("faces", {}, app="x")
    service.down = False
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
        assert service.leases[lease.id - 1]["vram_gb"] == 4
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
    service.busy = True
    with pytest.raises(TimeoutError, match="not free"):
        sm.acquire("x", timeout=0.5, ttl_s=0.3)  # a short ttl, so the queued request is kept alive meanwhile
    assert service.released == [1]
    assert 1 in service.heartbeats


def test_a_lease_refused_and_a_finished_job_row(service):
    service.refuse = True
    with pytest.raises(sm.JobFailed, match="lease 1 was cancelled"):
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
    jobs = service.jobs
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
    assert (g.path, g.seed, g.steps) == ("/out/g.png", 1234, 4)
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
        50.0, 400, (100, 50, 80, 80), ["/out/v-face-3.png"], "no numpy",
    )  # fmt: skip


def test_a_result_with_what_made_it_and_the_settings_a_cache_keys_on(service):
    service.models["shot-boundaries"] = "scene-score"
    service.results["shot-boundaries"] = {"cuts": []}
    service.options["shot-boundaries"] = {"threshold": 0.3}
    now = sm.role_settings("shot-boundaries")
    assert now is not None
    assert (now.model, len(now.settings_hash or "")) == ("scene-score", 12)
    result, p = sm.run_with_provenance("shot-boundaries", {"path": "/v.mp4"}, app="strata-reels")
    assert result == {"cuts": []}
    assert p == sm.Provenance("scene-score", {"threshold": 0.3}, now.settings_hash, p.job_id)
    assert service.jobs[p.job_id - 1]["settings_hash"] == now.settings_hash
    service.options["shot-boundaries"] = {"threshold": 0.4}  # the service's models.toml changed, the key did not
    later = sm.role_settings("shot-boundaries")
    assert later is not None
    assert later.model == now.model
    assert later.settings_hash != now.settings_hash
    _, q = sm.run_with_provenance("shot-boundaries", {"path": "/v.mp4"}, app="strata-reels")
    assert (q.options, q.settings_hash) == ({"threshold": 0.4}, later.settings_hash)
    job = sm.submit("shot-boundaries", {}, app="evals", options={"width": 320})
    assert (job["options"], job["settings_hash"]) == (None, None)  # recorded when it starts
    assert sm.Provenance.from_job(sm.wait_job(job["id"])).options == {"threshold": 0.4, "width": 320}
    assert sm.role_settings("nope") is None


def test_provenance_from_a_row_of_an_older_service():
    p = sm.Provenance.from_job({"id": 7, "model": "buffalo_l", "status": "done"})
    assert p == sm.Provenance("buffalo_l", None, None, 7)


def test_shot_boundaries_sends_an_absolute_path_and_reads_typed(service):
    sm.shot_boundaries("tape.mkv", app="strata-reels", priority="low", clip=(0, 60), scores=True)
    job = service.jobs[0]
    assert (job["role"], job["priority"], job["params"]["clip"], job["params"]["scores"]) == (
        "shot-boundaries", "low", [0, 60], True,
    )  # fmt: skip
    assert job["params"]["path"].endswith("tape.mkv")
    assert job["params"]["path"] != "tape.mkv"
    s = sm.Shots.from_json(RESULTS["shot-boundaries"])
    assert (s.cuts[0], s.shots[-1], s.black, s.fps, s.frames) == (
        sm.Cut(4.0, 100, 0.82), sm.Stretch(9.36, 12.0), [sm.Stretch(3.6, 4.0)], 25.0, 300,
    )  # fmt: skip
    assert (s.threshold, s.min_shot_s, s.scores, s.model, s.raw["width"]) == (0.3, 0.6, [], "ffmpeg-scene-score", 160)
    bare = sm.Shots.from_json({"cuts": [], "shots": [], "scores": [0.1, 0.9]})
    assert (bare.black, bare.fps, bare.scores) == ([], None, [0.1, 0.9])
