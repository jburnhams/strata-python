"""strata_python.speech: real files in tmp_path, real WAVs, the speak role through the real client against the shared
stand-in strata-models (`fake_models`), and the routes through FastAPI's TestClient."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from strata_python import web
from strata_python.clients.models import Provenance, RoleSettings
from strata_python.speech import Built, Clip, ClipStore, add_clip_routes, build, stale
from strata_python.testing import FakeModels, sine_wav
from strata_python.wav import WavError

SPEECH_RATE = 24_000  # Kokoro's


def made(tmp_path: Path, hz: float = 220.0, seconds: float = 0.5) -> Path:
    p = tmp_path / f"out-{hz:g}-{seconds:g}.wav"
    p.write_bytes(sine_wav(seconds, rate=SPEECH_RATE, hz=hz))
    return p


class Engine:
    """A speak role run in the test: a WAV per line (its pitch from the line, so two lines are two files)."""

    def __init__(self, tmp_path: Path, model: str = "kokoro", settings_hash: str = "h1") -> None:
        self.tmp_path, self.model, self.settings_hash = tmp_path, model, settings_hash
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []
        self.fail: set[str] = set()

    def __call__(self, line: str, params: Mapping[str, Any], model: str | None) -> tuple[dict[str, Any], Provenance]:
        self.calls.append((line, dict(params), model))
        if line in self.fail:
            raise RuntimeError(f"refused {line}")
        path = made(self.tmp_path, hz=200 + len(self.calls) * 10 + len(line))
        return {"path": str(path), "voice": params["voice"]}, Provenance(
            model or self.model, None, self.settings_hash, 1
        )


def test_put_names_clips_by_content_and_serves_only_what_the_manifest_holds(tmp_path: Path) -> None:
    store = ClipStore(tmp_path / "voice")
    assert store.read().clips == {}
    assert store.clip("Hi.") is None
    wav = sine_wav(0.5, rate=SPEECH_RATE)
    a = store.put("Hi.", wav, source="generated", voice="bf_emma", model="kokoro", settings_hash="h")
    b = store.put("Hello.", wav, source="generated", voice="bf_emma")
    assert a.file == b.file
    assert a.file.startswith("clips/")
    assert a.name.endswith(".wav")
    assert (a.seconds, a.model, a.settings_hash, a.source) == (0.5, "kokoro", "h", "generated")
    assert store.path_of(a.name) == tmp_path / "voice" / a.file
    assert store.path_of("../manifest.json") is None
    assert store.urls("/api/v1/voice/") == {
        "Hi.": f"api/v1/voice/clips/{a.name}",
        "Hello.": f"api/v1/voice/clips/{a.name}",
    }
    assert ClipStore(tmp_path / "voice").clip("Hi.") == a  # the manifest is on disk
    assert store.remove("Hi.")
    assert not store.remove("Hi.")
    assert (tmp_path / "voice" / a.file).is_file()  # still Hello.'s, and a cached URL keeps working
    with pytest.raises(WavError, match=r"longest kept is 0\.2"):
        store.put("Long.", wav, source="recorded", voice="recording", max_seconds=0.2)
    with pytest.raises(WavError):
        store.put("Bad.", b"not a wav", source="recorded", voice="recording")


def test_a_recording_always_wins(tmp_path: Path) -> None:
    store = ClipStore(tmp_path)
    rec = store.put("/m/", sine_wav(0.3, rate=SPEECH_RATE, hz=300), source="recorded", voice="recording")
    got = store.put("/m/", sine_wav(0.5, rate=SPEECH_RATE), source="generated", voice="bf_emma")
    assert got == rec
    assert store.clip("/m/") == rec
    again = store.put("/m/", sine_wav(0.4, rate=SPEECH_RATE, hz=500), source="recorded", voice="recording")
    assert store.clip("/m/") == again != rec  # a new recording replaces the old one


def test_edits_from_many_threads_lose_no_line(tmp_path: Path) -> None:
    store = ClipStore(tmp_path)
    wav = sine_wav(0.1, rate=SPEECH_RATE)
    threads = [
        threading.Thread(target=store.put, args=(f"Line {i}.", wav), kwargs={"source": "generated", "voice": "v"})
        for i in range(12)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(store.read().clips) == 12


def test_build_makes_keeps_skips_and_reports(tmp_path: Path) -> None:
    store = ClipStore(tmp_path / "voice")
    speak = Engine(tmp_path)
    speak.fail.add("Broken.")
    progress: list[str] = []
    out = build(
        store,
        ["Hi.", "/a/", "/m/", "Broken."],
        app="myapp",
        voice="bf_emma",
        params={"language": "en-gb", "seed": 0},
        per_line=lambda line: None if line == "/m/" else ({"phonemes": "a"} if line == "/a/" else {}),
        speak=speak,
        say=progress.append,
    )
    assert out == Built(made=["Hi.", "/a/"], skipped=["/m/"], failed={"Broken.": "refused Broken."})
    assert speak.calls[1] == ("/a/", {"language": "en-gb", "seed": 0, "phonemes": "a", "voice": "bf_emma"}, None)
    assert progress[-1] == "FAILED Broken.: refused Broken."
    clip = store.clip("Hi.")
    assert clip is not None
    assert (clip.model, clip.settings_hash, clip.voice) == ("kokoro", "h1", "bf_emma")

    now = RoleSettings("kokoro", "h1")
    again = build(store, ["Hi.", "/a/"], app="myapp", voice="bf_emma", speak=speak, now=now)
    assert again == Built(kept=["Hi.", "/a/"])  # same voice, model and settings: nothing made
    assert build(store, ["Hi."], app="myapp", voice="bm_george", speak=speak).made == ["Hi."]  # another voice
    assert build(store, ["Hi."], app="myapp", voice="bm_george", speak=speak, force=True).made == ["Hi."]
    changed = RoleSettings("kokoro", "h2")
    assert build(store, ["Hi."], app="myapp", voice="bm_george", speak=speak, now=changed).made == ["Hi."]
    store.put("Hi.", sine_wav(0.2, rate=SPEECH_RATE), source="recorded", voice="recording")
    assert build(store, ["Hi."], app="myapp", voice="x", speak=speak, force=True).kept == ["Hi."]  # never remade


def test_a_recording_kept_while_build_runs_stands(tmp_path: Path) -> None:
    store = ClipStore(tmp_path)
    engine = Engine(tmp_path)

    def speak(line: str, params: Mapping[str, Any], model: str | None) -> tuple[dict[str, Any], Provenance]:
        store.put(line, sine_wav(0.2, rate=SPEECH_RATE, hz=900), source="recorded", voice="recording")  # meanwhile
        return engine(line, params, model)

    progress: list[str] = []
    out = build(store, ["/s/"], app="myapp", voice="bf_emma", speak=speak, say=progress.append)
    assert out.kept == ["/s/"]
    assert progress == ["kept  /s/ (recorded meanwhile)"]
    clip = store.clip("/s/")
    assert clip is not None
    assert clip.source == "recorded"


def test_stale() -> None:
    clip = Clip(
        file="clips/x.wav", source="generated", voice="v", model="kokoro", settings_hash="h1", seconds=1, made=""
    )
    assert not stale(clip, None)
    assert stale(clip, RoleSettings("other", "h1"))
    assert stale(clip, RoleSettings("kokoro", "h2"))
    assert not stale(clip, RoleSettings("kokoro", None))  # a hash only one side knows compares the model alone
    assert not stale(clip.model_copy(update={"settings_hash": ""}), RoleSettings("kokoro", "h2"))


def test_build_runs_the_speak_role_on_strata_models(fake_models: FakeModels, tmp_path: Path) -> None:
    wav = made(tmp_path)
    fake_models.results["speak"] = lambda params: {"path": str(wav), "voice": params["voice"], "seconds": 0.5}
    fake_models.models["speak"] = "kokoro-82m"
    store = ClipStore(tmp_path / "voice")
    out = build(store, ["Find the cat."], app="myapp", voice="bf_emma", params={"seed": 0}, model="kokoro-82m")
    assert out.made == ["Find the cat."]
    job = fake_models.jobs[0]
    assert (job["role"], job["app"], job["params"]) == (
        "speak",
        "myapp",
        {"seed": 0, "voice": "bf_emma", "text": "Find the cat."},
    )
    clip = store.clip("Find the cat.")
    assert clip is not None
    assert clip.model == "kokoro-82m"
    assert clip.settings_hash


@pytest.fixture
def served(tmp_path: Path) -> tuple[TestClient, ClipStore]:
    store = ClipStore(tmp_path / "voice")
    app = FastAPI()
    web.add_error_handlers(app)
    add_clip_routes(app, store, "/api/v1/voice", recordable=lambda line: line.startswith("/"), max_seconds=1.0)
    return TestClient(app), store


def test_routes_list_serve_record_and_forget(served: tuple[TestClient, ClipStore]) -> None:
    client, store = served
    assert client.get("/api/v1/voice").json() == {"clips": {}}
    made_clip = store.put("Hi.", sine_wav(0.5, rate=SPEECH_RATE), source="generated", voice="v")
    listed = client.get("/api/v1/voice").json()["clips"]
    assert listed == {"Hi.": f"api/v1/voice/clips/{made_clip.name}"}
    got = client.get("/" + listed["Hi."])
    assert got.status_code == 200
    assert got.headers["content-type"] == "audio/wav"
    assert got.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert got.content == sine_wav(0.5, rate=SPEECH_RATE)
    assert client.get("/api/v1/voice/clips/nothing.wav").status_code == 404

    take = sine_wav(0.4, rate=SPEECH_RATE, hz=330)
    put = client.put("/api/v1/voice/recordings", params={"line": "/m/"}, content=take)
    assert put.status_code == 200, put.text
    body = put.json()
    assert (body["line"], body["source"], body["seconds"]) == ("/m/", "recorded", 0.4)
    assert client.get("/" + body["url"]).content == take
    assert client.delete("/api/v1/voice/recordings", params={"line": "/m/"}).status_code == 204
    assert client.delete("/api/v1/voice/recordings", params={"line": "/m/"}).status_code == 404
    assert client.delete("/api/v1/voice/recordings", params={"line": "/x/"}).status_code == 404  # never recorded


def test_routes_refuse_what_is_not_kept(served: tuple[TestClient, ClipStore]) -> None:
    client, store = served
    wav = sine_wav(0.4, rate=SPEECH_RATE)
    not_recordable = client.put("/api/v1/voice/recordings", params={"line": "Hi."}, content=wav)
    assert not_recordable.status_code == 400
    assert "not a line that is recorded" in not_recordable.json()["detail"]
    long = client.put("/api/v1/voice/recordings", params={"line": "/m/"}, content=sine_wav(2, rate=SPEECH_RATE))
    assert long.status_code == 400
    assert "longest kept is 1" in long.json()["detail"]
    big = client.put("/api/v1/voice/recordings", params={"line": "/m/"}, content=b"\0" * 4_000_001)
    assert big.status_code == 400
    assert "at most 1 seconds" in big.json()["detail"]
    assert client.put("/api/v1/voice/recordings", params={"line": "/m/"}, content=b"junk").status_code == 400
    store.put("Hi.", wav, source="generated", voice="v")
    assert client.delete("/api/v1/voice/recordings", params={"line": "Hi."}).status_code == 400
    assert store.read().clips.keys() == {"Hi."}
