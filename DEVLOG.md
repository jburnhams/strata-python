# Development log

Why each change was made and which files it touched, newest day first. For what changed in each release, as an app sees it, read [CHANGELOG.md](CHANGELOG.md).

## 2026-11-27

**Clients for face tracks and grouping them into people, and a fake face store (branch `claude/project-thread-l890x5`)**
strata-reels gets a people stage (a tape's faces followed, grouped, named by the person, then fed to the scene describer); the service's side is strata-models' `POST /api/faces/group-tracks` and the role `face-tracks`, and the typed clients every app uses belong here (strata-360 is the other user of face-tracks).
- `src/strata_python/clients/models.py`: `face_tracks` (absolute `path` and `out_dir`), `FaceTracks`, `FaceTrack`, `TrackStill`; `face-tracks` added to `ROLES`.
- `src/strata_python/clients/faces.py`: `group_tracks` (each track's stored stills as `faces`, so the store can hide a track under its bar), `TrackGroups`, `TrackGroup`, `Suggested`; `images` (paged by `before`); `index` sends `width` and `height`, which the service already took.
- `src/strata_python/testing.py`: **new** `FakeFaceStore`, mounted on `FakeModels` as `.faces`: index with faces given (detection is the service's, so a picture without faces stores none), list by app, ref and tag, assign and undo by batch, people, opt-in tags, a 1x1 PNG crop, and `group-tracks` by cosine to each group's first track with `apart` kept apart and `suggest` naming a person per key. `MODELS_RESULTS["face-tracks"]` in the service's shape.
- Tests: `tests/unit/test_clients_models.py` (paths made absolute, the typed answer), `tests/unit/test_clients_faces.py` (group-tracks body and groups, every page of images, the size sent), `tests/unit/test_testing.py` (the fake store through the real client).

**`render`: the sound trimmed to the first frame, and each file's interlacing measured once (review of PR #91)**

Two findings from the review. The input seek starts half a frame before a cut's first frame and drops the frame before it, but the sound kept that half frame, so it ran 20 ms early at 25 fps (measured: the onset of a tone that starts on the cut's first frame came 964 samples late in the output). And a file's interlacing was measured once per cut, not per file, because `setdefault` evaluates its default every time.

- `src/strata_python/render.py`: `atrim=start_sample` of the preroll (the first frame's time less the seek point) before the sound's timestamps are reset; a cut with no `interlace` of its own reads the file's from a cache filled on the first miss. `RECIPE` stays 1, since 1 was never released.
- `tests/unit/test_render.py`: a tone that starts on frame 25, cut from that frame, is heard from the output's first samples.

**An edit rendered to one file (`render`)**

strata-reels' next step (its spec S8) is a reel rendered from the tape without changing the tape: deterministic, frame exact, with a manifest of what made it. Cutting stretches of files and joining them is what strata-360 and any later app need too, so the ffmpeg side lives here and reels keeps only its table, job and manifest's own part (which reel and revision).

- `src/strata_python/render.py` **new**: one ffmpeg run, one input per cut sought half a frame before its first frame (input seeking decodes from the keyframe before and drops what precedes the seek point, so the first kept frame is the cut's and a cut near a tape's end does not decode the tape from its start), `trim=end_frame` to the cut's frame count, `setpts` and `fps` to the output rate, the first file's display shape at the output height (other shapes letterboxed), and per cut audio resampled, padded and trimmed to the samples its frames own, rounded once from the edit's start so nothing drifts. Bitexact flags, no metadata, x264 at a fixed 4 threads (its output depends on the count). Constant frame rate assumed.
- `tests/unit/test_render.py` **new**: sources whose frames carry their number in their brightness, read back with signalstats, so the tests check exactly which frames each cut kept (inside a GOP, on a keyframe, from the start, off-frame bounds); sound length, a file with no audio and one interlaced at another size; the same bytes twice and the manifest; height never above the source and a rate change; refusals and a failed run leaving nothing.
- `README.md` module table, `AGENTS.md` layout, `CHANGELOG.md` Unreleased.

## 2026-11-26

**A client for strata-models' speaker-embedding role**

strata-voice is getting an Upload screen for found recordings of its speaker, which hold other voices; it keeps the sentences that sound like the speaker's studio takes. strata-models now serves `speaker-embedding` (ECAPA in a worker venv of its own, same day), so its contract and client belong here.

- `src/strata_python/clients/models.py`: `speaker_embeddings` (items of path and optional clip, made absolute, in one job so a recording is decoded once) and `SpeakerEmbeddings` (a null in place of a stretch too short to place); the role joins `ROLES`.
- `src/strata_python/testing.py`: `MODELS_RESULTS["speaker-embedding"]`, in the worker's own shape.
- `tests/unit/test_clients_models.py`: the params sent and the typed result.
- `README.md` (section 5b): `speaker_embeddings` and `SpeakerEmbeddings` listed; speaker-embedding no longer among the roles without a runtime (review, Codex).

**images reads HEIC, JPEG XL and camera raw files**

The owner asked for strata-models' face store to take any picture format, HEIC first. The face store opens pictures through `images` (the kept medium copy, crops, a picture's size), so the readers belong here, where strata-stills and strata-360 get them too. Pillow 13 already reads AVIF, WebP, TIFF, PNG, GIF and BMP.

- `pyproject.toml`, `uv.lock`: the `[images]` extra adds `pillow-heif` (HEIC/HEIF, registered with Pillow), `pillow-jxl-plugin` (JPEG XL, registers on import) and `rawpy` (LibRaw), all with wheels for Linux, macOS and Windows.
- `src/strata_python/images.py`: `open_upright` registers the two Pillow plugins once and develops a raw file with rawpy (`open_raw`: camera white balance, 8-bit, LibRaw's own turn by the camera's orientation; at half size for a `draft` that leaves it big enough). It develops the raw data rather than taking the embedded preview, whose size differs between cameras, because a face found on one copy must land on the same pixels in every copy. New `extensions()`, `is_raw`, and `size_upright`, which reads a photo's size from its header with the sides swapped for EXIF orientations 5 to 8. strata-models read the stored size, so a phone photo taken upright had its width and height the wrong way round against boxes found on the upright picture.
- `src/strata_python/testing.py`: **new** `tiny_dng`, a linear-raw DNG written byte by byte (no camera's file can be committed to a public repo, and LibRaw reads this one). Being linear, LibRaw does not halve it, so the half-size path is exercised but not measured.
- `tests/unit/test_images.py`: HEIC, AVIF, JPEG XL, WebP, TIFF and PNG through `open_upright` and `size_upright`; a raw file developed and turned; a draft and a thumbnail of one; unreadable files as `OSError`; swapped sizes for orientation 6.
- `CHANGELOG.md`, `README.md`.
- Then macOS CI failed twice in `test_oslib`'s priority tests: pytest itself was at nice 19, so its children could not set a lower value. Cause, traced with strace: libheif's x265 encoder calls `setpriority(PRIO_PROCESS, 0, 10)` in each worker thread. On Linux that changes only the thread; on macOS it changes the process. Saving a HEIC in the test process did it. `testing.py`: **new** `tiny_picture`, which encodes in a child; `tests/unit/test_images.py` uses it and checks this process's nice is unchanged. It also imports `rawpy` at the top, because a pytester run that loads numpy (pgvector) removes it from `sys.modules` afterwards, and numpy cannot load twice (3 raw tests failed in a fixed-order local run). `tests/unit/test_oslib.py`: a priority child that fails shows its stderr and the parent's nice value; `README.md`, `CHANGELOG.md`: `tiny_picture`.

## 2026-11-25

**Windows CI installs ffmpeg even when the Chocolatey feed refuses it**

The Windows leg failed twice on the same head: the Chocolatey community feed answered an error for `ffmpeg` 9.1.0, `choco` reported "installed 0/0 packages" and exited 0, and 28 media and derive tests then failed for want of ffmpeg.

- `.github/workflows/ci.yml`: on Windows, when `choco` does not leave `ffmpeg.exe` behind, download the static win64 build from BtbN/FFmpeg-Builds and put its `bin` on the path; a new step checks `ffmpeg` and `ffprobe` run on every OS, so a missing tool fails at install, not as test errors.

**strata-models' bulk endpoint in the models client: normal and low work goes there (branch `claude/models-bulk`)**

The owner asked strata-models to stop holding Claude jobs for a possible batch (no app ever sent a burst, and every call waited 10 to 15 s), to keep `POST /api/jobs` for one high priority job and to move all other work to the bulk endpoint. Every app reaches the service through this client, so it moves them all.

- `src/strata_python/clients/models.py`: `submit` sends a `high` job to `/api/jobs` (without a priority, the only one there) and anything else to `/api/jobs/bulk` as a call of one, then reads the row back; `submit_many` and `run_many` are new.
- `src/strata_python/testing.py`: `FakeModels` has the bulk endpoint, refuses a normal or low single job (400, as the service), and a poll without `?wait=` no longer moves a job on (the real service only reads it; `submit` now reads the row back, which made a fresh job run).
- `tests/unit/test_clients_models.py`: the new routes, failures in `run_many`.


**The PyPI upload installs pgvector too (branch `claude/project-thread-2fs8bi`)**

v0.2.20 (#81, `db.vectors`) was tagged and released on GitHub but refused by `pypi-publish.yml`: its gate found the runner's Postgres, ran the vector tests against it, and `CREATE EXTENSION vector` failed, because only `ci.yml` installed pgvector.

- `.github/workflows/pypi-publish.yml`: installs `postgresql-<v>-pgvector` for the runner's newest server before the gate, as `ci.yml` does.

**Embeddings, crops and the face store client, for strata-models' one face database (branch `claude/project-thread-2fs8bi`)**

The owner decided that strata-models owns one face database for every app (faces, an embedding per model, people with per-app fields over global ones, opt-in tags left out of default search), and asked for common code to go here so other apps can build their own face screens. Three apps already keep embeddings in pgvector, each with `pgvector.sqlalchemy.Vector`, which has no SQLite form, so none of their unit tests could store one; review screens in three apps cut face crops by hand.

- `src/strata_python/db/vectors.py` **new** (the type, operator and operator class are named with the extension's schema, `public`, because an app's connection searches only its own schema: bright-horizons sets `search_path TO brighthorizons, public` before every vector query instead): `Vector` (a `TypeDecorator`: `vector(dim)` on Postgres through a small `UserDefinedType` speaking pgvector's text form, JSON text elsewhere; the length is checked at the write), `hnsw_index`, `cosine_distance` (a zero vector is at distance 1, where pgvector answers NaN), `nearest` (Postgres orders by `<=>` and turns on `hnsw.iterative_scan` when the installed pgvector has it, 0.8+, since an older one refuses the name; SQLite reads the matching rows and orders them in Python).
- `src/strata_python/images.py`: `webp_bytes`, `FORMATS`, `thumbnail(fmt=)`, `crop`, and `_cached` (the made-once logic `thumbnail` had inline, now shared).
- `src/strata_python/clients/faces.py` **new**: the face store's endpoints as typed calls; `clients/models.py`: `request`, a public door to `_call` for it.
- `tests/unit/test_db_vectors.py` **new** (SQLite and Postgres with pgvector), `tests/unit/test_images.py` (WebP thumbnails, crops: padding, squaring, clipping, refusals, the app's reader), `tests/unit/test_clients_faces.py` **new** (a scripted server over real HTTP).
- `.github/workflows/ci.yml`: installs pgvector for the installed Postgres on Linux, so the Postgres vector tests run there.
- `tests/unit/test_db_vectors.py`: the Postgres cases skip on Windows, with a reason: the Windows runner's Postgres has no pgvector package (CI failed there on `CREATE EXTENSION vector`); Linux CI still requires them.
- `src/strata_python/db/vectors.py`: `nearest` on Postgres leaves out rows with no vector (review finding): their NULL distance sorted last, filled `k` when there were fewer real matches, and crashed on `float(None)`; the test now asks for more than there are.

**`wer.find`: the prompt inside a longer take (branch `claude/project-thread-cvjjmf`)**

strata-voice's hands-free studio cuts takes out of an always-open microphone, so a take can hold a false start or a word to someone else before or after the prompt. The owner asked for a quick way to "extract the relevant bit for the prompt". Finding a known text inside a transcript is the same word alignment `word_errors` does, so it lives beside it.

- `src/strata_python/wer.py`: `find` and `Found`. It uses a fitting alignment, which is Levenshtein with free words heard before and after the run. Ties go to the later end and then the later start, so "the quick, sorry, the quick brown fox" keeps the second reading. Heard words are taken one by one, as a recogniser gives them with timings. Each keeps its index through `normalise`, so a word that splits ("well-known") or vanishes ("—") still maps back to its timing.
- `tests/unit/test_wer.py`: 3 tests (a restart, chatter, two full readings, a misreading inside chatter, split and empty words, nothing heard).
- Review (Codex): a run could start or end inside a heard word that normalised to several, then be handed back whole, so `find("known", ["known", "well-known"])` chose the second word with an error. Runs now start and end only between heard words.

## 2026-11-24

**`derive.frames` past 100 frames on ffmpeg 9 (branch `derive-frames-many`)**

Found by strata-reels on a real 21-minute tape on omarchy (ffmpeg 9.0.2), once it asked for a keyframe every six seconds: 209 frames, and ffmpeg stopped with "Cannot allocate memory" while setting up `select`. A plain `eq(n,a)+eq(n,b)+...` parses into a tree as deep as it has terms, and ffmpeg 9 refuses a depth over 100 (100 terms pass, 101 fail, 400 in sums of 20 pass). Any tape with over 100 shots would have had no keyframes. `FRAMES_PER_RUN` stays 400, so a tape is still decoded once or twice.

- `src/strata_python/derive.py`: `TERMS_PER_SUM`, `_pick`, used by `frames`.
- `tests/unit/test_derive.py`: 401 frames of a real clip (the one test fails on ffmpeg 9 without the change).

**Spoken clips and the outbox's server half, from Flashtap (branch `claude/project-thread-5rvxef`, issues #73 and #74)**

Asked to fix the open issues. Both were raised from Flashtap, which built each by hand: its voice (`voice.py` `Store` and `build`, `server/app.py` `add_voice_routes`) and its answer log (`learners/store.py` `record`, `_same`, `hold`). Read both and kept their rules: a made clip never replaces a recording, decided under the manifest's lock so a recording taken while `build` runs stands; a duplicate is found before the app does its own work on a write, and compared with what the client sent, not with a row rebuilt now.

Decisions: `insert_once` takes a `make` callable rather than a ready row (the issue's sketch), because the check has to come before the app's work and the row is that work's result; a ready row is `make=lambda: row`. It also answers two tries of one id at once as a duplicate (the second's insert fails on the key once the first commits; the session is rolled back and the stored row read), which Flashtap avoided only by holding the learner's lock first. `lock_row` is `SELECT ... FOR UPDATE` on Postgres and Flashtap's no-op `UPDATE` on SQLite (which ignores FOR UPDATE), and returns the row read under the lock with `populate_existing`, so a row already in the session is not served stale. `ConflictingWrite` subclasses `tasks.Conflict` so `add_error_handlers` already answers 409. In `speech`, what Flashtap kept about phonics became two hooks: `per_line` adds a line's own parameters (its `phonemes`) or returns None for a line a person records (Flashtap's consonants, listed as `skipped`, was `to_record`); the speak role's model goes as the client's `model` argument, not a parameter. The list route returns line -> URL only (`ClipsOut`), the shape strata-react #318's clip list reads; Flashtap's sounds list stays its own route. A recording's body is refused while it streams in once past `max_bytes`, instead of after it is read. `path_of` serves any clip file the store wrote, by a name checked to be a content hash, not only the manifest's current ones (Flashtap's rule): a review on the PR pointed out that a URL listed before its line was remade would otherwise 404, which also made `remove`'s promise false.

- `src/strata_python/speech.py` **new**: `Clip`, `Manifest`, `ClipStore` (`read`, `clip`, `path_of`, `urls`, `put`, `remove`), `clip_url`, `Built`, `Speak`, `speak_with_models`, `stale`, `build`, `ClipsOut`, `ClipOut`, `add_clip_routes`.
- `src/strata_python/db/writes.py` **new**: `ConflictingWrite`, `insert_once`, `lock_row`.
- `src/strata_python/db/__init__.py`: the three names, loaded lazily like the rest.
- `tests/unit/test_speech.py` **new**: the store, a recording winning (also while `build` runs), twelve threads editing the manifest, `build`'s made, kept, skipped, failed and remade lines, the speak role through the real client on `fake_models`, every route.
- `tests/unit/test_db_writes.py` **new**: once by id, a retry, changed contents, two tries at once, a failing `make`, two sessions queued on `lock_row`, composite keys, the 409 and 404, on SQLite and Postgres.
- `README.md`, `AGENTS.md`, `CHANGELOG.md`: the module rows, the 5b and 8 sections, the layout, the Unreleased lines.

**`workflow`: stages, gates and state machines the apps had each built (branch `claude/project-thread-daa05w`)**

Asked to find what strata-listory, strata-stills and strata-reels share in their workflow, pipeline and state-machine code and to make one module they could all adopt. Read listory's ingest (`ingest/pipeline.py`, `ingest/store.py`: eleven stages, append-only events with metrics and data, resume, three review gates whose approval is bound to a signature of the evidence and can be given by policy) and its processing plan (`catalogue.PLAN_STATES`, `claim_plan`'s conditional UPDATE); stills' develop pipeline (`pipeline.py`: ordered stages with settings and versions, a key of all of them that finds a picture already made, timing per stage, rules that list what is missing); reels' `jobs.py`/`derived.py`/`results.py` (hash, then three assets keyed on the hash, the recipe's version and the parameters, each job queueing the next). reels' overview names strata-360's stage runner as the one to generalise "if it generalises cleanly", so that was read too (`pipeline/stages.py`, `runner.py`: stage version, config keys and dependencies in the key, `clear` with dependents, a state matrix) and is the model here. What differs per app (what a stage does, where heavy files go, stills' in-memory picture between stages) stays in the app.

Decisions: a need's answer enters its dependents' keys with its key (not the answer alone), so a stage that writes files and returns little cannot leave the stages after it on old files; a stage whose need is not done is `blocked`, never run, even if its own record still matches; one queue job advances one subject through every stage it can, rather than a job per stage, because every reels job holds `disk` (they ran one at a time anyway) and listory ran its stages in one process; a failed stage lets the stages that do not need it run (strata-360's fail-soft) and then raises, so the queue's backoff and programming-error rules apply unchanged. Standard library apart from `workflow.store`, which uses the db extra as `tasks.queue` does.

Jonathan then asked that storage be abstracted so stills could keep its run in memory or use its own key: `Store` was already a protocol (`MemoryStore`, `DbStore`, or the app's own), so `Pipeline` gained `key=` (a `KeyFn`, `default_key` otherwise; stills keeps its 10-character picture keys, which name its files, and regenerates nothing) and `json=False` (answers kept as returned, so a stage hands the next one the picture itself and stills' `State` object can go). An answer JSON cannot hold is compared by its record's key alone.

He also asked how this sits with the job queue, which overlaps, and for common endpoints so strata-react can have one UI. The split kept: the queue is how work runs (one attempt, retry and backoff, resources, cancel, progress), the pipeline is what has been done to a subject (which stages hold for which inputs, which approvals stand); a pipeline never retries or schedules, and a queue job never knows about stages beyond its progress `stage` and its result. They meet in three places: `enqueue` (kind `workflow:<pipeline>`, domain `<pipeline>:<subject>`, the stages' resources), the progress stage, and `WorkflowRun.job`, found by that domain. No app had shared job routes (each spelled its own), so `tasks.web.add_jobs` adds them with `add_workflow` beside it; the contracts carry the state words so the generated TypeScript names them.

- `src/strata_python/tasks/web.py` **new**: `add_jobs`, `job_task`.
- `src/strata_python/workflow/web.py` **new**: `add_workflow`, `workflow_run`, `event`.
- `src/strata_python/contracts.py`: `StageState`, `WorkflowState`, `StageEvent`, `StageRun`, `WorkflowRun`; `workflow.pipeline` takes its state words from here.
- `tests/unit/test_workflow_web.py` **new**: every route through TestClient on a real SQLite queue and store.
- `src/strata_python/workflow/__init__.py` **new**: the package's exports and where each rule came from.
- `src/strata_python/workflow/pipeline.py` **new**: `Stage`, `step`, `gate`, `Context`, `Record`, `Store`, `MemoryStore`, `Outcome`, `Pipeline` (`key`, `states`, `advance`, `approve`, `clear`, `status`, `dependents`, `upto`), `request_key`.
- `src/strata_python/workflow/machine.py` **new**: `StateMachine`, `InvalidTransition`.
- `src/strata_python/workflow/store.py` **new**: `workflow_table`, `DbStore`, `enqueue`, `move`.
- `tests/unit/test_workflow.py` **new**: keys, resume, fail-soft, gates (by hand and by policy), out-of-date approvals, clears, states, stopping, progress, the machine.
- `tests/unit/test_workflow_store.py` **new**: the store, a queue job advancing a subject and `move`, on SQLite and Postgres.
- `README.md`, `CHANGELOG.md`, `AGENTS.md`: the module row, the 6b migration note, the Unreleased line, the layout.

**Frames by number and a typed shot-boundaries client, for strata-reels' scenes (branch `claude/project-thread-36vaj1`)**

strata-reels' plan step 2 splits a tape into shots (strata-models' `shot-boundaries`) and groups them into scenes by how alike neighbouring shots look (CLIP, `image-embed`), which needs one picture per shot. A VHS shot is often shorter than the thumbnails' 10 s step, so the pictures are picked by frame number, and both pieces are general: any app that cuts footage into shots wants them.

- `src/strata_python/derive.py`: `frames` and `Frame`; `RECIPE["frames"]`. A `select` of `eq(n,k)` terms after the deinterlacer (bwdif reads the frames either side, so it runs before the pick), `-frames:v` so ffmpeg stops after the last one. Runs of `FRAMES_PER_RUN` (400) numbers, each decoding from the start: the filter is about 16 characters a frame and Windows caps a command line at 32,767, and a tape's few hundred shots are one or two runs. Seeking each run to its first frame was not used, as a frame count after an input seek is not the frame number. A number past the end is a `MediaError` naming it, and the old folder is kept.
- `src/strata_python/clients/models.py`: `shot_boundaries`, `Shots`, `Cut`, `Stretch`; `ROLES` names `shot-boundaries` (strata-models serves it since its #102).
- `src/strata_python/testing.py`: `MODELS_RESULTS["shot-boundaries"]`, in the runtime's shape.
- `tests/unit/test_derive.py`: frames of a ramp whose brightness is 40 times the frame number, so each picture is checked to be its frame (read by shrinking it to one grey pixel: a lavfi `movie=` source failed on Windows, whose paths have a colon); several runs; past the end; negative numbers. `tests/unit/test_clients_models.py`: the shortcut's request and the typed answer.

**`wav.measure` and `wer` for scoring recorded takes (branch `claude/project-thread-cvjjmf`)**

strata-voice's step 2 scores each take of a prompt (clipping, level, noise floor, and the word error rate of its transcript against the prompt). The owner wants shared audio code in the libraries, and strata-models' transcription evals compute the same word error rate, so both live here.

- `src/strata_python/wav.py`: `measure` and `WavLevels`. Standard library only: `array` holds the samples and `map(operator.mul, w, w)` squares a window in C, about 0.2 s for a minute at 48 kHz, so no numpy for an app that only judges takes. 24-bit is widened by slicing the bytes into the top three of four (scaled by 256, sign kept). Noise is the 10th percentile and speech the 95th of 20 ms window RMS, so pauses are the floor and one cough is not the voice. A sample within 0.1 % of full scale counts as clipped; the count is skipped when the peak is below that, the usual case.
- `src/strata_python/wer.py` (**new**): `normalise` (the same rules as strata-models' eval metrics, so the numbers compare) and `word_errors`, a Levenshtein alignment that keeps its edits, so an app can name the words missed rather than give a bare rate.
- `src/strata_python/testing.py`: `sine_wav` takes `lead`, `tail` and a seeded `noise`, and clamps above full scale, so tests can make a take with pauses, a room and clipping.
- Review (Codex on #65): `measure` refuses a header with a sample rate of 0 as a `WavError` (it divided by it); `normalise` drops characters by Unicode category instead of `[^\w]`, which split Devanagari words at their combining vowel signs and scored different words as equal.
- `tests/unit/test_wav.py`, `tests/unit/test_wer.py` (**new**): levels of a known tone, clipping, 24-bit, silence, refusals; each kind of edit.
- `README.md`, `AGENTS.md`, `CHANGELOG.md`: the new names.

**`media.configure`: the app's own setting says where ffmpeg and ffprobe are (#60, branch `claude/project-thread-wov55p`)**

Tubes declares `ffmpeg` on its `Settings` and its doctor checks that path, but `media` only read `STRATA_FFMPEG` and the PATH, so Tubes kept its own `subprocess` call and the setting and the variable could name two programs. The issue offered an `exe=` keyword or a resolver set once; the resolver was chosen, because every call site (including `derive`, which calls `media` itself) would otherwise have to pass it.

- `src/strata_python/media.py`: `configure()` and `_from_app()`; `tool()` asks the app first, then the environment, then the PATH. The function is read on every lookup so `app set ffmpeg` takes effect while the server runs. A configured value that is not runnable raises instead of falling back, so the doctor row and the run never disagree about which program is used.
- `tests/unit/test_media.py`: the order of the lookup and a changing setting, and a real probe and ffmpeg run found only through `configure` with the environment variables pointing elsewhere.
- `README.md`: the module row and the migration note.

**The publish workflow installs ffmpeg (branch `claude/project-thread-42bdk4`)**

v0.2.11 was tagged and released on GitHub but not uploaded: `pypi-publish.yml` runs `strata-check` on the tag, and with no ffmpeg on its runner the `media` and `derive` tests skip, which put unit coverage under the 95% floor. `ci.yml` installs ffmpeg and sets `STRATA_TEST_FFMPEG=1`; the publish job now does the same. v0.2.11 then needs publishing by hand (the workflow's `tag` input), since the tag's own run has failed.

- `.github/workflows/pypi-publish.yml`: an ffmpeg step and `STRATA_TEST_FFMPEG=1`.

## 2026-11-23

**Main's CI red on a Ticker test; `derive` listed under the wrong release (branch `claude/project-thread-42bdk4`)**

After #56 merged, CI on main failed on Windows in `test_ticker_fn_chooses_the_next_wait_and_poke_wakes_it` (from #58), so no release ran and `derive` is not on PyPI. The test waited for `next_at` to be set after the second run, which it also is for the 1 ms wait between the second and third runs; Windows' coarse clock made that moment long enough to be read. It now waits for the long wait itself.

- `tests/unit/test_tasks.py`: the wait condition. No library code changes.
- `CHANGELOG.md`: a correction. 0.2.10 was released from #58 while #56 was open, and merging main into #56 put its two lines under the 0.2.10 heading; `derive` and `media.ffmpeg(sink=)` are not in 0.2.10, so they move back to Unreleased, for the next release.

**`derive`: proxies, thumbnails and audio peaks beside `media.probe` (branch `claude/project-thread-42bdk4`)**

strata-reels' first step (ingest) needs a browser proxy, thumbnails and audio peaks for every tape, and its plan puts them here because every app that shows footage needs the same three. The owner set that ingest starts from the raw footage: restoration is a strata-models job later in reels' pipeline, so the proxy only deinterlaces for display and changes nothing else.

- `src/strata_python/derive.py` (**new**): `interlacing`, `proxy`, `thumbnails`, `Thumb`, `peaks`, `Peaks`, `RECIPE`. The proxy keeps the source's frames at their times (`-fps_mode passthrough`, bwdif `send_frame`), so a time chosen on it is the time in the source; the digitised VHS tapes are interlaced but do not say so (strata-models' VHS work had to force top field first), so a stream's "progressive" is checked with `idet` rather than believed, and an even split between the two field orders (fine detail in progressive footage) counts as progressive. Thumbnails use a `select` on each frame's time rather than the `fps` filter, which drops the picture for the last stretch (a 12 s clip at 10 s steps gave one thumbnail, not two). Each thumbnail run stages in its own temporary folder (Codex on #56: two runs into one folder shared a fixed `.part` name and deleted each other's). Peaks are min/max pairs because that is what strata-react's `Waveform` draws.
- `src/strata_python/media.py`: `ffmpeg(sink=...)`, and the progress reader and the new stdout reader share one `_piped` runner (stderr drained on a thread, timeout kill, a reader that raises stops ffmpeg).
- `tests/unit/test_derive.py` (**new**): on generated PAL-shaped interlaced clips (with and without a field order), progressive clips and a square tone; `tests/unit/test_media.py`: the sink.
- README (module row), AGENTS.md (layout), CHANGELOG.

**Issues #52 to #55, from tubes moving onto 0.2.7 (branch `claude/project-thread-w34kcw`)**

The owner asked for the four issues tubes (tubes#89) raised; each one was a workaround it had to carry.

- `src/strata_python/settings.py`: `Setting.parse` reads a JSON number for `number_between`/`whole_between` and a bool for `boolean` (#52). Only there: elsewhere `str(5)` would still pass a hand-edited number off as a deliberate string, so a number for a path is still refused. An int is written with `str` (a float of 1e16 would come back as `1e+16`), a float through `_num`, so `4.0` reads as `"4"` for a whole number. `Settings.value` logs an ignored value once per key, place and value (it is read on every `get`, so once per read would flood the log). `number_between` takes `hi=None` and `lo_open`, `whole_between` takes `hi=None`, and `positive(hi)` is the open-at-0 case (#53); a flag on the existing class rather than a new one, so `model()` keeps recognising it and an app's `number_between(lo, hi)` is untouched. The schema says `exclusiveMinimum` for an open bound and leaves `maximum` out when there is none.
- `src/strata_python/tasks/ticker.py`: `fn`'s number is the next wait (a bool and NaN are not, a negative one means now, `inf` waits for a poke), `poke()` sets a second Event so a wake does not stop the loop, and the Event is cleared before the run so a poke during a run runs it again straight after instead of being lost (#54). `next_at` is wall-clock, for a status page.
- `src/strata_python/web.py`: `Status = int | Callable[[Exception], int]`, called in the one handler (#55).
- `tests/unit/test_settings.py`, `tests/unit/test_tasks.py`, `tests/unit/test_web.py`: each case above, with the prefetcher's shape for the Ticker and a 502/503 by cause through a live server.
- `README.md`, `CHANGELOG.md`: the settings, Ticker and error-handler notes.

**`wav.read_wav` and `testing.sine_wav`, from strata-voice's studio (branch `claude/project-thread-cvjjmf`)**

The owner asked for the recording studio's common code to live in the shared libraries. strata-voice checked each uploaded take's header with `wave` before keeping it; any app that takes recorded speech (or imports old recordings) needs the same check and the same test WAVs, so both move here. strata-react gets the browser half (capture, WAV writer, level meter) in the same change.

- `src/strata_python/wav.py` (**new**): `read_wav`, `WavInfo`, `WavError`, `SPEECH_RATE`. Only the header is read; nothing is decoded, so the cost does not grow with the file. The limits are keyword arguments with the speech defaults (mono, 16/24-bit, 16 kHz) because each app's are its own. A correction to the studio's copy: it wrote `{e or 'it ends early'}`, but an exception instance is always truthy, so a truncated file read "()" instead of "it ends early"; here it is `str(e) or ...`, with a test.
- Review fixes (Codex on #50): a path is opened, not read whole, and the last frame is looked for, because `wave` takes `getnframes()` from the data chunk's declared size: a one-second WAV cut to its 44-byte header passed as one second long. Both checks cost the same at any length.
- `src/strata_python/testing.py`: `sine_wav` (8, 16, 24 and 32-bit, any channel count, silence at amplitude 0).
- `tests/unit/test_wav.py` (**new**): every refusal sentence, bytes and paths, a chunk that claims more than follows, and the tone's amplitude.
- `README.md`, `AGENTS.md`, `CHANGELOG.md`: the module row, the layout line and the Unreleased entry.

**Provenance for model results: `run_with_provenance`, `role_settings` (branch `claude/project-thread-3e79cp`)**
strata-models issue #103: strata-reels stores every analysis result with the model, version and config that made it, and keys its cache on them. `role_model()` gives only the key, which stays the same when the service's models.toml changes a model's options, and `run()` dropped which model served the job. strata-models now records `options` and `settings_hash` on each job when it starts and shows `settings_hash` per role in `/api/registry`; this is the typed client side.
- `src/strata_python/clients/models.py`: `Provenance` (`from_job`), `run_with_provenance`, `RoleSettings`, `role_settings`. New names rather than a flag on `run`/`role_model`, so their return types stay exact and existing callers are untouched.
- `src/strata_python/testing.py`: `FakeModels.options` (role -> load options); a job records `model`, `options` (merged with the submit's own) and `settings_hash` when it starts, `null` while queued as on the service; the registry's roles carry `settings_hash`.
- `tests/unit/test_clients_models.py`: the result with its provenance, the hash changing when the options change under the same key, a variant's options, a row from an older service.
- README (clients row), CHANGELOG.

**Issues #45 and #46: the shared responses and `/api/system`'s own fields, exact in the schema (branch `claude/project-thread-xzb4kc`)**

The owner asked for the two issues raised while moving python-app-template onto 0.2.6. PR #42 had left the contracts on `BaseModel` so apps' generated types would not move; the template showed the move is what apps want (its `page.next ?? null`), and a field turning from optional to required only removes a `?`.

- `src/strata_python/contracts.py`: `Health`, `Folder`, `ServiceStatus`, `SystemInfo`, `ErrorBody`, `Progress`, `Task` and `Page` on `ApiModel` (#45); `extra="allow"` merges with the inherited config. `FileRef` and `Judgment` stay on `BaseModel`: an app may take them as a request body, where `ApiModel` would split the component into `-Input` and `-Output`.
- `src/strata_python/web.py`: `add_system` takes `extra_model`, or reads the model from `extra`'s return annotation, and declares a `SystemInfo` subclass with those fields named `<App>SystemInfo` after the app's title, as `Settings.model()` names `<App>Settings` (#46). A distinct name, not `SystemInfo` again: two components of one name make FastAPI fall back to long module-path names. `extra` may return the model itself.
- `tests/unit/test_contracts.py`: every response shape lists all its fields as required, and `Page[Task]` in a served schema; `tests/unit/test_web.py`: the extra fields in the served schema, inferred and named, and the plain mapping unchanged.
- README (server section), CHANGELOG.

**Exact media time: `mediatime` (branch `claude/project-thread-1p0er6`)**
strata-reels, the new video editor (milestone R1 of its plan: a digitised tape in, a highlight reel out), needs time that does not drift: source, edit and render time kept apart, frame-exact on 25 fps PAL and 30000/1001 NTSC, with the map from an edit back to its sources. strata-360 had its own (float seconds plus a UTC map); strata-vid keeps float seconds and its cuts drift. Both are apps that need the same thing, so it lives here. Rationals (the standard library's `Fraction`) rather than integer ticks: a tick base has to be chosen to fit every rate at once, a Fraction fits any, and the numbers here are small.
- `src/strata_python/mediatime.py`: **new**. `rational`, `text`, `Rate`, `Span`, `Piece`, `TimeMap`. Spans are half-open, so two shots that meet share no frame; snapping goes to the earlier frame at exactly halfway, so a cut never moves past the frame it was in; a float is read by its shortest repr, so 0.1 is 1/10.
- `tests/unit/test_mediatime.py`: **new**; includes 300 random edits at five rates where output, source and output again land on the same time exactly, and the float drift it replaces (0.04 added a thousand times lands a frame early).
- `CHANGELOG.md` (Unreleased, Added), `README.md` (the module table).
- Review: `Span.overlaps` said an empty span inside another overlapped it while `&` said they share nothing; it is now `&`'s answer (`mediatime.py`, a test in `test_mediatime.py`).

**Issues #36 to #41: public-check for web apps, an openapi command, settings enums, and a server's records (branch `claude/project-thread-xzb4kc`)**

The owner asked for the six issues flashtap raised. Each came from code flashtap wrote around a gap here.

- `src/strata_python/public_check.py`: the web suffixes and `.env.example` are read, `storybook-static` and `baselines` skipped (#37). A folder in a git checkout is listed with `git ls-files -co --exclude-standard`, so `.coverage` after `pytest --cov` is not read; a folder git ignores is walked in full when named, since `strata-public-check dist` asks for exactly that; no git or no checkout falls back to the walk. A suffixless file with a NUL byte in its first 8 KiB is skipped, for `.coverage` outside a checkout. `[tool.strata-public-check] deny` (#38): strings or `{pattern, kind}` tables, compiled case-insensitive, checked with the built-ins (so an exclude and the ignore comment apply, and the match is printed: the pattern is public already). The issue's "scan any UTF-8 file" option was not taken: it would read `uv.lock` and every unknown format, and the suffix list is what reviewers can see.
- `src/strata_python/web.py`: `openapi_text` (#36); `PUT /api/settings` takes numbers, so a web app on `numbers=True` can send back what it got.
- `src/strata_python/cli.py`: `add_openapi`, writing with `\n` line ends on Windows too, or `--check` would never agree with a checkout; `set` shows a setting's choices.
- `src/strata_python/testing.py`: `assert_openapi_current`, importing FastAPI only when called.
- `src/strata_python/contracts.py`: `ApiModel` (`json_schema_serialization_defaults_required`), from the issue's note on flashtap's response model. The existing contracts are left as they are: changing them would change every app's generated types.
- `src/strata_python/settings.py`: `one_of`, `Setting.choices` (applied after `normalise`), `allowed()`, and `model()` emitting a `Literal` (#41). `whole_between` and `number_between` became frozen dataclasses so `model()` can read their range. Numbers in the schema are behind `Settings(numbers=True)`: by default the wire stays strings, because turning `"2"` into `2` breaks every web app that reads the setting as text, and that should be the app's change, not a library upgrade's.
- `src/strata_python/db/project.py`: `find_table` takes a `strata-db.toml` (top level keys) before a pyproject.toml and follows `file = ...` from `[tool.strata-db]` (#40); `migrations` is relative to the file's folder; a declaration inside a package puts the folder above its top package on `sys.path`, not the package itself. Of the issue's three options this is the one strata-db, the pytest plugin and the service all read; `Project.for_package` would have been a second declaration in Python. `Project.open` (#39).
- `src/strata_python/db/cli.py`: `init --package DIR`; `revision` prints the file relative to the working folder.
- `src/strata_python/db/config.py`: `local_url`, and `default=` on `resolve_url`/`url_for`, used only when no URL variable and no shared database is set (#39).
- `src/strata_python/db/engine.py`: `connect` makes a SQLite file's folder.
- `src/strata_python/db/web.py` **new**: `Database` (lazy, locked, retried after a failure), `add_database` wrapping the app's lifespan so the engine is disposed after it, `db_session` and `DbSession` (#39). In `db/` rather than `web.py`, so `web` never imports SQLAlchemy.
- Tests: `test_public_check.py` (web suffixes, a real git checkout with ignored, untracked and deleted files, no git, repo patterns through `main`, bad entries), `test_cli.py` (the openapi command and its check, choices in `set`), `test_contracts.py`, `test_settings.py`, `test_web.py` (the enum and the numbers over real HTTP), `test_db.py` (a package declaration copied to a site-packages folder with no pyproject.toml and migrated from there; `local_url`; `open`; `add_database` over real HTTP with the app's own lifespan).
- README (module table, settings, CLI, database), AGENTS.md layout, CHANGELOG.

**Files, a polite HTTP client, ffprobe and ffmpeg, thumbnails and pairwise statistics (branch `claude/project-thread-8zfbch`)**

The owner asked for the rest of the shared modules, prioritised by how many apps duplicate the code. A new survey of the apps' current code (cloned from GitHub) ranked what was left after the job queue: the HTTP client (listory, 360, takeout, tubes, planning and models' scripts, about ten copies), atomic writes and hashing (360, listory, stills, takeout, filecat, models), ffprobe and ffmpeg (360, listory, models: eight wrappers), thumbnails (360, stills, bright-horizons, scan) and pairwise statistics (stills' arena, models' evals). All five are here. Decision records stay out until an app needs them (candidate 10 of the plan).

- `src/strata_python/files.py` **new**: from 360's and stills' temp-then-replace writes, which mostly put the temp file beside the target but shared one temp name between threads and lost the extension ffmpeg needs. `atomic_path` keeps the extension and a random part. `inside` is the check a file route needs and none of the apps had in one place. `DiskCache` is 360's places cache and listory's `match_cache` without the database: one file per key, the time it was stored in its first line.
- `src/strata_python/tasks/status.py`, `tasks/__init__.py`: `write_json_atomic` moved to `files` and is re-exported, so status files and the rest share one writer.
- `src/strata_python/http.py` **new**: listory's `covers/net.py` rules (per-host gap, Retry-After as seconds or a date, cool-down, errors as a reply), its providers' rule that only reads are retried after a reset, and 360's `places._throttle` lock file for a gap shared across processes. One change from listory: concurrent threads take slots under a lock, so they queue `min_interval` apart instead of all starting after one gap. A 503 with Retry-After is waited out as a 429 is. `sleep` defaults to `time.sleep` looked up per call, so the plugin's `no_sleep` applies.
- `src/strata_python/media.py` **new**: listory's `process/probe.py` command (`-v warning`, so its "estimating duration from bitrate" check can read `Probe.warnings`), models' `ffmpeg` flags, and a `-progress` reader with stderr drained on a thread. `STRATA_FFMPEG`/`STRATA_FFPROBE` pick another build.
- `src/strata_python/images.py` **new** and the `[images]` extra in `pyproject.toml` (Pillow 13, as `[pdf]`): stills' `browser.cached` (stat key, a semaphore, a second request waiting for the first) and 360's `photos.thumb` flattening of alpha and 16-bit greys.
- `src/strata_python/stats.py` **new**: models' `evals/stats.py` (Wilson, McNemar, Holm) and stills' `arena._fit` (votes summed per pair, the left-side advantage), in pure Python. The numpy bootstraps over items stay in models until another app needs them.
- `tests/unit/test_files.py`, `test_http.py`, `test_media.py`, `test_images.py`, `test_stats.py` **new**: real files (a reader process that never sees a half-written file), the `http_server` fixture for every HTTP rule (one fake opener, for a reset while reading an error body), the real ffmpeg on a generated two-second clip with chapters, real pictures with EXIF orientation, and Bradley-Terry recovering the strengths it simulated from.
- `.github/workflows/ci.yml`: installs ffmpeg on Linux, macOS and Windows and sets `STRATA_TEST_FFMPEG=1`, so the media tests fail rather than skip in CI. Elsewhere they skip with that reason.
- README (module table, step 6c), AGENTS.md layout, CHANGELOG.
- From the Codex review of PR #34, each with a test that fails without the fix: a 503 with Retry-After is retried only for an idempotent request (it does not say the write was not applied, unlike a 429); the cache key includes the headers sent, so two callers with different credentials never share a cached answer; `bradley_terry(side=True)` stops only when the side advantage has settled too (equal strengths at the start stopped it after one step); `ffmpeg(input=..., progress=...)` now feeds the input instead of dropping it.
- Windows CI: `test_timeouts` hung for five minutes. Chocolatey's `ffmpeg.exe` is a shim that starts the real ffmpeg, and `subprocess.run`'s timeout killed only the shim, so the real ffmpeg kept the pipes open and the read never ended. `media` now stops ffmpeg with `oslib.kill_tree`. Two `test_files` tests also failed there; the run was cut off before it printed why. The likely cause is that Windows refuses to replace a file another process has open, and refuses to open one while it is being replaced. So `atomic_path`'s replace, `read_json` and `DiskCache.get` now retry a `PermissionError` for up to 2 s on Windows. This is unconfirmed until the next Windows run.
- The next Windows run confirmed it: the writer's retries held, and the one failure left was the test's own reader subprocess, which opened the file with a bare `open` and got the same `PermissionError` mid-replace. That is a refused open, not a half-written read, so the reader in `test_a_reader_never_sees_a_half_written_file` now retries it, as `read_json` does. It still counts every read that is not whole JSON.

**Issues #27 and #28: `rasterise` at the scan's size, `lower_priority` sets the nice value (branch `claude/project-thread-u9wgtq`)**

The two open issues, both reported from apps that had worked around them.

- `src/strata_python/oslib.py`: `lower_priority` used `os.nice`, which adds to the current value. A macOS runner starts at -10, so listory's worker ended at 9 and its tools did not get idle priority (#28). It now reads the current value and `setpriority`s to `nice` when that is higher, as the docstring always said.
- `src/strata_python/pages/render.py`: pypdfium2 sizes the bitmap with `ceil(points * scale)`. A 2660 px page at 500 dpi is 383.04 pt, stored a hair over, so it came back 2661 px (#27; strata-models#75 pasted it onto a white canvas). `rasterise` now crops to `round(points * scale)`; ceil is at most a pixel over round, so this only ever trims a sliver. pdfium's page sizes already follow `/Rotate`, so no swap is needed.
- `tests/unit/test_oslib.py`: the nice value set absolutely from 5, kept from 15, and reaching 19 from -10. Each fails on the old code. Starting below 0 needs root with CAP_SYS_NICE, so that last case asks the kernel in a child first and skips with its reason where it is refused (CI is not root). `tests/unit/test_pages.py`: three scan sizes at 500 dpi and at half the dpi, and a rotated PDF.
- `tests/unit/test_tasks.py`: CI on this branch caught a race in the `Detached` test that was already there. The child writes "done" and only then prints "finished", so the log could still be empty. The test now waits for the line.
- `tests/unit/test_oslib.py`: the in-process test called `lower_priority(0)` to change nothing. Once the value is absolute, that raised the pytest process from -10 to 0 on the macOS runner, so the below-zero test's child could not start at -10. It now passes -20.

## 2026-11-14

**Every queue job in a supervised process, from a pool that is reused: listory's supervisor in `oslib` and `tasks.child` (branch `claude/project-thread-aefumh`)**

Jonathan asked whether listory's process-group supervisor applied to every job. It does: a thread can only be asked to stop, so the queue's cancel and timeout were cooperative and a handler that never checked `job.stopping` ran to the end. The first version started a new interpreter per job, as listory does, which cost about 0.3 s a job. He asked for processes to be reused, like a thread pool, so quick jobs stay cheap. Each worker thread now keeps one job process that runs job after job. Measured on SQLite over 300 trivial jobs: 3.5 ms a job in job processes, against 2.9 ms on threads. Only a kill starts a new process.

- `src/strata_python/oslib.py`: `start_supervised`, `watch_supervisor`, `terminate_own_group`, `kill_group`, from listory's `job_process`, `start_job`, `watch_supervisor`, `terminate_job_group` and `kill_job`. The child is started in its own session (a new process group on Windows) with a pipe on stdin that the parent never writes to. `watch_supervisor` reads it until EOF and then kills the group, so a parent that crashes takes its jobs with it. It pops `STRATA_SUPERVISED` so the job's own children do not think they are supervised.
- `src/strata_python/tasks/child.py` **new**: the job process and `JobProcess`, the parent's end of it. The process imports handlers by `module:qualname` (cached), connects once with the same URL (passed in the environment, not argv, because it can hold a password), and reads one job a line on stdin, answering a line each on its original stdout. Its fd 1 is pointed at stderr, so prints and tools' output go to the log and not into the answers. A thread reads stdin, so the parent closing it (or dying) kills the group even mid-job. Before running an attempt it checks the claim token and skips a replaced one. Errors cross the process boundary as a type name, their builtin base class and a message. The parent rebuilds them so `next_state` treats them as it would in-process: a `TypeError` still fails at once, a `RetryLater` keeps its pause, and `KeyError('x')` still reads `KeyError: 'x'`. `JobProcess` starts the process when its thread starts, reuses it, kills it on cancel, timeout or stop, and replaces it on the next job, or after `max_jobs`.
- `src/strata_python/tasks/queue.py`: `Worker(isolate=True, lower_priority=False, max_jobs=None)`, with one `JobProcess` per thread (`worker.slots`). `_run_isolated` sends the job and waits for the answer while it watches the cancel flag, the timeout and the stop event. It records cancelled, `TimeoutError`, released, `WorkerLost` (the process died without answering) or the process's own result or error. Handlers are checked at construction, so a lambda fails at start-up instead of on the first job.
- `src/strata_python/oslib.py`: `start_supervised(..., text=True)` also pipes stdout, as text, for the answers.
- Windows CI caught a hang: a tool the handler starts inherits the job process's real stdout handle on Windows, because `dup2` on fd 1 does not move the Win32 handle. Its output reached the answer pipe, the parent could not parse it, and the worker thread died. Answers now start with a marker (`child.MARK`), and any other line on the pipe goes to the log (`child.route`). A worker thread that hits its own error now fails that job, replaces its process and carries on.
- The next Windows run still hung on that job, which waits on a tool from inside the job process. `oslib.stdout_to_stderr()` **new** now also moves the Win32 standard output handle (`SetStdHandle`), so the tools a handler starts write to the log on Windows too. The test's tool call has a timeout, so a hang shows up as the job's error rather than a stuck wait. Its own test checks that what the process and a tool print reaches stderr and only the answer reaches stdout. The Windows behaviour is still unconfirmed until CI runs it.
- **Correction:** that was not the cause. With the timeout in place, CI showed the tool itself never exiting on Windows. The job loop's thread blocks reading the command pipe on stdin, a synchronous pipe serialises I/O, and the tool inherited that pipe as its stdin and blocked starting up. `oslib.stdin_from_devnull()` **new** moves the commands to a private fd and gives fd 0 (and the Win32 standard input handle) to the null device, so tools get an empty stdin. The oslib test now holds a thread blocked on the pipe while a tool runs and reads its stdin.
- `tests/unit/job_handlers.py` **new** (module-level handlers a process can import). `tests/unit/test_queue.py`: one worker running seven kinds of outcome; cancel, timeout and stop each killing the job and the tool it started (pids checked); 40 jobs in one process under 0.25 s a job, a printing handler leaving the protocol intact, and a cancel replacing the process; `max_jobs`; handlers that cannot be imported refused; the job loop run in-process for coverage, including a replaced attempt; rebuilt errors. The existing thread-worker test now passes `isolate=False`. `tests/unit/test_oslib.py`: closing the lifeline kills the child and its tool.
- README (oslib row, migration note, the queue section), AGENTS.md layout, CHANGELOG.

**Queue and `Detached` fixes from the review of PR #25 (branch `claude/project-thread-aefumh`)**

The Codex review of PR #25 found four real races; each fix has a test that fails without it.

- `src/strata_python/tasks/queue.py`: a `claim_token` column, new per claim; heartbeat, report, finish, fail and release update only the row with this attempt's token, so a worker whose job was recovered and claimed again writes nothing over the new attempt (and `on_fail` does not run for it), and its heartbeat tells it to stop. `claim` reads candidates `batch` (200) at a time and goes on past the batch, keyed on (priority, id), so jobs waiting for a held resource no longer hide the rest. On SQLite the claim starts with a write that changes nothing, which takes the database's write lock, so another process or `Queue` waits before reading which resources are held (the earlier entry's "two workers cannot start two jobs that share a resource" held only within one `Queue` on SQLite).
- `src/strata_python/tasks/status.py`: `StatusFile.locked()` (`<path>.lock`, `oslib.file_lock`); `write` and `update` hold it, and `Detached.start` holds it from before the child exists until its "running" is written, so a child that finishes at once is no longer written over and then read as died.
- `tests/unit/test_queue.py`: blocked jobs beyond a batch, a second `Queue` waiting on a claim in progress (both backends), a recovered attempt that cannot touch its replacement. `tests/unit/test_tasks.py`: a child that finishes at once, an update in another process waiting for the lock.

**A durable job queue: `strata_python.tasks.queue` (branch `claude/project-thread-aefumh`)**

The plan's last step before the later modules: one queue for the seven job systems. It follows the survey's design: listory's schema and claim loop on SQLAlchemy Core, the strata-models store's style (injectable clock, conditional-UPDATE claim, cursor paging, `recover`, `prune`, `step`), and `tasks.retry`. The first adopter the plan names is strata-scan, with listory replacing its own queue as the proof; neither has moved yet.

- `src/strata_python/tasks/queue.py` **new**: `jobs_table`, `Job`, `Queue`, `Worker`, `WorkerLost`, `JobCancelled`, `dedupe_key`. Times are float epochs in the table so both backends store them the same; `Task` turns them into aware datetimes. Claims hold a lock in the process and, on Postgres, `pg_advisory_xact_lock`, so two workers cannot start two jobs that share a resource. Cancel and timeout are cooperative (`job.stopping`): killing a thread is not possible, and work that must be killable is a `Detached` process. Subprocess isolation per job, which listory has, is not here yet.
- The method that lists jobs is `page`, not `list`: inside the class `list` would shadow the builtin in its own annotations (ty refuses `list[str]` there).
- `tests/unit/test_queue.py` **new**: every case on a SQLite file and on the throwaway Postgres: paging, priority and delay, dedupe, resources, backoff then failure with `on_fail`, `RetryLater` and progress resetting the count, cancel (queued, running, raising), recovery of a dead pid and of a stale heartbeat (another host's job only when stale), prune, and real worker threads with a timeout and a shutdown that releases its job.
- `README.md` (module table, step 6b), `CHANGELOG.md`.


**Contracts, a service client base, web extras and the task primitives (branch `claude/project-thread-aefumh`)**

The owner asked for the rest of the library work in the move-out plan while the apps move onto 0.2.x. These are its steps 3 to 5 (web extras, contracts and `ServiceClient`, task primitives), designed from the survey's notes on each app's copy. The models client stays standard library only and keeps its own calls (decided earlier today); only its `Unavailable` now subclasses the shared one, so `add_error_handlers` answers it as 503.

- `src/strata_python/contracts.py` **new**: `Health`, `SystemInfo`, `Folder`, `ServiceStatus`, `ErrorBody`, `Task`, `TaskState`, `FINISHED`, `Progress`, `Page[T]` (written with `Generic[T]`, since 3.11 is supported), `FileRef`, `Judgment`, `utc`.
- `src/strata_python/client.py` **new**: `ServiceClient`, `Rejected`, `Unavailable`, `env_url`. Only idempotent methods retry, because a retried POST can submit a job twice.
- `src/strata_python/web.py`: `add_error_handlers` and `DEFAULT_ERRORS`, `add_system`, `source_commit`, `base_app(commit=)`, and the settings routes declare `Settings.model()` as their response. The PUT body stays an untyped dict on purpose: a typed body would answer an unknown key with 422 instead of the 400 apps and their tests expect.
- `src/strata_python/settings.py`: `Settings.model()` (pydantic imported inside it, so the CLI's settings reads stay light).
- `src/strata_python/tasks/` **new**: `retry.py` (after strata-360's `pipeline/retry.py` as the survey describes it: programming errors fail at once, progress resets the count, `RetryLater` names its pause), `progress.py`, `flight.py` (`SingleFlight`, `Flight`, `Conflict`), `status.py` (`StatusFile`, `Detached`, `write_json_atomic`; `Detached` reaps its own exited child, since a zombie still passes `pid_alive`), `ticker.py`, `__init__.py`.
- `src/strata_python/oslib.py`: `detached_kwargs`, `load_average`. listory's process-group supervisor is not here yet: its code was not read in this pass.
- `src/strata_python/clients/models.py`: `Unavailable` subclasses `client.Unavailable`.
- `tests/unit/test_contracts.py`, `tests/unit/test_client.py`, `tests/unit/test_tasks.py` **new**; `tests/unit/test_web.py` (error bodies, `/api/system` with a real service up, one down and one not ok, `source_commit` on a made-up checkout and worktree, the settings schema in OpenAPI), `tests/unit/test_oslib.py` (`detached_kwargs` and `load_average` checked on each platform: a new session on POSIX, a detached child that still runs and no load average on Windows, which the first CI run caught). All over real HTTP, real threads, real files and real child processes.
- `README.md` (module table, step 4, step 6, new step 6b), `AGENTS.md` (layout), `src/strata_python/__init__.py`, `CHANGELOG.md`.


**A stand-in strata-models for the apps' tests (branch `claude/project-thread-aefumh`)**

The move-out plan's next step for the models client: every app that calls strata-models would otherwise write its own fake service or mock the client. The stand-in that tested the client here becomes public, so apps test their own calls against the same service steps over real HTTP.

- `src/strata_python/testing.py`: `FakeModels` (FastAPI, imported inside the constructor so importing the fakes stays light) with the job, lease, heartbeat, release, registry and health endpoints, scripted results and failures, `busy`, `refuse` and `down`; `MODELS_RESULTS`, the example replies that were in the test, with output paths under `/out/` (ruff's S108 refuses `/tmp` in `src`).
- `src/strata_python/pytest_plugin.py`: the `fake_models` fixture (served by `live_server`, `STRATA_MODELS_URL` set).
- `tests/unit/test_clients_models.py`: uses the fixture instead of its own `Service` class. `tests/unit/test_testing.py`: scripted results, a failure for one role, a missing job and lease, an unknown role, an outage, `running_polls=0`.
- `README.md` (module table, step 5b, fixture table), `CHANGELOG.md`.


**Pillow 13 as the `[pdf]` extra's floor (branch `claude/project-thread-aefumh`)**

The audit job failed on main after `strata_python.pages` landed: the lock held Pillow 12.3.0, which has eight advisories fixed in 13.0.0. Dependabot's PR #23 raised the cap and the lock; this raises the floor too, so an app that installs `[pdf]` cannot resolve to a vulnerable Pillow either.

- `pyproject.toml`: `pillow>=13,<14`. `uv.lock`.
- `CHANGELOG.md`.


**The strata-models client moves in as `strata_python.clients.models` (issue #18; branch `claude/project-thread-vzy9v6`)**

Apps installed strata-models' client from a path or a git URL into their own venvs. The library rules say a strata service's client belongs here. The owner chose standard library and dataclasses for it, with no pydantic and no `[models]` extra, because the client is called from code and does not validate requests. That replaces the morning's note about `strata_python.services.models` on pydantic.

- `src/strata_python/clients/__init__.py`, `src/strata_python/clients/models.py` **new**: the client's code as it is in strata-models' `client/` (same names, defaults, long-poll and lease steps), fully annotated, with typed result shapes for the roles apps call today: `ocr`, `transcribe`, `faces` and `speak`, read from the service's runtimes (`runtimes/tesseract.py`, `workers/whisper.py`, `runtimes/faces.py`, `workers/tts.py`). The shortcuts still return the JSON, so a move is one import line, and each shape keeps the whole reply in `raw`. The issue also named `image-describe`, `font-match` and `page-build`, but the service has no `image-describe` role (that is `vision-language`) and the other two do not exist yet, so they are left until it serves them.
- `tests/unit/test_clients_models.py` **new**: a stand-in service in FastAPI over real HTTP (`live_app`), covering the shortcuts' requests, the typed shapes, a failed job, a refused request, a 503, no service, a wait that times out, a lease with heartbeats and release, a queued lease withdrawn after its timeout, and `gpu_lease` with no service.
- `README.md` (module table, step 5b), `AGENTS.md` (layout), `CHANGELOG.md`.
- Every role the service runs, as the owner asked after the first version: the roles in `models.toml` whose runtime is in `runtimes.RUNTIMES` (16 of 22), with params and result shapes from each runtime's docstring (`runtimes/llama.py` and `runtimes/cloud.py` for `ask`, `sdcpp.py`, `clip.py`, `facekind.py`, `workers/audio.py`, `workers/audio_quality.py`, `workers/video.py`). The other six (sound-events, speaker-embedding, detect-people, upscale, denoise, nsfw) have models listed but no runtime (`transformers`, `speechbrain`, `ultralytics`, `spandrel`, `nudenet` are not in `RUNTIMES`), so they are left out until the service serves them. A test keeps `ROLES` and the stand-in's results the same set.
- Left for strata-models: test the client against its real API in its integration suite, then delete `client/`.


**Settings: typed values, lists and a pluggable store (issues #10, #16; branch `claude/project-thread-vzy9v6`)**

strata-stills could not adopt `Settings` because two of its settings are lists, and strata-models because its settings move into a database table and it has numbers, ranges and dates. Both kept a copy of `settings.py`. Typed int, float and bool settings were left as normalisers plus getters, not new kinds. The values stay canonical strings, so the file is still hand-editable and every existing app reads the same. Only lists needed a new kind.

- `src/strata_python/settings.py`: `number_between`, `whole_between`, `iso_date`, `boolean`; `get_int`, `get_float`, `get_bool`, `get_list` and `value`; `ListSetting`; `SettingsStore` (a protocol: `load`, `update`), `JsonFileStore`, which is the old file code moved as it was, lock and atomic replace included, and `Settings(store=)`, `Settings.file` and `bind(store, import_file=True)`, which copies the file's valid values in once and leaves a marker row. Each kind parses through one `parse` method, so the environment, the store and `save` agree on what a value is.
- `src/strata_python/db/settings.py` **new**: `settings_table(metadata)` (key, value as JSON text, the same on both backends) and `TableStore`, an upsert per key in one transaction so concurrent saves of different keys keep both.
- `src/strata_python/web.py`: the settings routes carry lists.
- `tests/unit/test_settings.py`: each normaliser and getter, lists from the file, the environment and `save`, a hand-edited list file, `bind` copying once and not again. `tests/unit/test_db.py`: the table store on SQLite and Postgres with concurrent saves. `tests/unit/test_web.py`: lists over real HTTP. `tests/unit/test_cli.py`: `set` on a list.
- `README.md` (module table, step 3), `AGENTS.md` (layout), `CHANGELOG.md`.


**Doctor checks and health fields an app can add (issues #15, #17; branch `claude/project-thread-vzy9v6`)**

strata-stills and strata-models each kept their own `doctor`, and strata-models its own `/api/health`, because the shared ones could not be extended. The two issues asked for the same doctor hook in two shapes (`(ok | None, line)` and a four-word status); this takes the four-word one, which says more, with `optional=` for the "not available, not a failure" case.

- `src/strata_python/cli.py`: `Module`, `Check`, `Status`, `program()` and `doctor()`; `add_doctor` takes `checks` and `hint`. Rows keep the existing columns (a failing row is upper case, as `MISSING` was), and a check that raises is a `broken` row rather than a crashed doctor. Shared GPU checks (torch present, a device, a kernel runs) are left for when an app moves its doctor here and shows the exact checks it needs.
- `src/strata_python/web.py`: `base_app(health=...)`, merged after `ok` and `version`, called per request.
- `tests/unit/test_cli.py`, `tests/unit/test_web.py`: labels, optional modules, each status, a crashing check, a missing program with its hint, `add_doctor` passing them through; health fields over real HTTP, including `ok` turned false.
- `README.md` (module table, steps 4 and 5), `CHANGELOG.md`.


**Models: declare the base class, since ty cannot subclass `make_base()` (issue #20; branch `claude/project-thread-vzy9v6`)**

strata-stills found that ty reports `unsupported-base` on `class Photo(Base)` when `Base = make_base()`, and the gate fails on ty's warnings. Reproduced with this repo's ty: a value typed `type[DataclassBase]` has no MRO ty can know. pyright accepts it, which is why PR #7's checks passed.

- Considered a shared base class to subclass instead (the issue's second option): it cannot work, because SQLAlchemy gives a registry and MetaData only to a *direct* subclass of DeclarativeBase, so every app's tables would share one MetaData. So the docs show the two-line class with `NAMING_CONVENTION` (the issue's first option) and `make_base()` stays for run-time use.
- `src/strata_python/db/models.py`, `src/strata_python/db/__init__.py`: docstrings show the class and say why.
- `tests/unit/test_db.py`: ty and pyright run on a models file written the documented way; both pass it, and both fail a misspelt constructor argument, so the check sees through the base.
- `README.md` (module table, step 8), `CHANGELOG.md`.


**`oslib.lower_priority(io=True)` (issue #12; branch `claude/project-thread-vzy9v6`)**

Three strata-stills scripts each carry `os.nice(15)` plus `ionice -c3 -p <pid>`; it moved the CPU half to `lower_priority` and kept the I/O half, which belongs here.

- `src/strata_python/oslib.py`: `io=True` calls `ioprio_set` (idle class) through `ctypes` on the Linux architectures in a small syscall table, falling back to `ionice` elsewhere or when refused; `PROCESS_MODE_BACKGROUND_BEGIN` on Windows (this process only: Windows does not pass background mode on); `setiopolicy_np` throttling on macOS, excluded from coverage since no CI leg counts it. All best effort, as the CPU part was.
- `tests/unit/test_oslib.py`: each path aimed at a child process (the internal helper takes a pid, so the test process keeps its own priority) and read back with `ionice`, the syscall path with `ionice` hidden so a fallback cannot pass for it; and the public call in a child, whose own child inherits the class. They failed before the change.
- `README.md` (step for `oslib`), `CHANGELOG.md`.


**Release workflow: check the token before pushing anything (issue #8; branch `claude/project-thread-vzy9v6`)**

The release after #7 pushed its bump branch and then failed at the first `gh` call with "invalid header field value for Authorization", most likely a trailing newline in the pasted `RELEASE_TOKEN` (git base64-encodes it, `gh` sends it raw). The secret was re-saved and 0.2.1 is on PyPI; this makes a bad token fail first, with a message saying what to fix.

- `.github/workflows/release.yml`: a first step that fails when the secret is empty or holds whitespace, then reads the repository with it. It does not trim the token, as the issue offered: the checkout and release actions take the secret as it is, so a trimmed copy would only move the failure later.


**`strata-public-check`: versions are not addresses, and a way to silence what is left (issue #13; branch `claude/project-thread-vzy9v6`)**

An app with a web front end got 28 findings, most of them npm versions (`10.0.0`, `10.4.2`) matched as private IPv4 addresses, because the pattern took two or three parts after the prefix. It also found real home-folder paths in two of that app's files, so the check is worth having there once it is quiet.

- `src/strata_python/public_check.py`: the address pattern takes exactly four parts and not inside a longer run of digits and dots; `exclude` globs from `[tool.strata-public-check]` in ./pyproject.toml and `--exclude`, matched against a path or its end (an sdist's member path carries the sdist's folder); `strata-public-check: ignore` on a line skips the built-in patterns there. Both deliberately leave the owner's `STRATA_PUBLIC_DENY` patterns alone, so an excluded file is still checked against them (a review finding on the first version, which skipped it whole): they live in a secret, and an exception for one would have to be written in the public repo.
- `tests/unit/test_public_check.py`: versions and longer dotted numbers, an address before a full stop, the ignore marker (and that it does not cover deny patterns), excludes from pyproject.toml, the command line and inside an sdist.
- `README.md` (the module table), `CHANGELOG.md`.


**The db pytest plugin switches on only for apps that use it, and its `--pg` no longer clashes (issues #11, #19; branch `claude/project-thread-vzy9v6`)**

Two apps reported that with 0.2.1 installed pytest would not start when the old database package was installed too: both plugins add `--pg`, and a second `addoption` for a name raises. The plugin also switched its guard on whenever SQLAlchemy and Alembic were importable, which refuses the old package's throwaway databases in the same run. So an app could not take 0.2.x first and move its database later, the order README step 8 suggests.

- `src/strata_python/db/pytest_plugin.py`: "on" now means the extra is installed and the project asks (`[tool.strata-db]` from the rootdir up, or the new `strata_db` ini option, which wins either way). The guard starts in `pytest_load_initial_conftests` (still before conftest.py and the app's modules; `pytest_configure` when the plugin is loaded from a conftest.py, as here) instead of at import. `pytest_addoption` adds only names of our own (`--strata-pg`, the ini option); `--pg` is added once on, after every entry-point plugin has added its options, and a `ValueError` there means another plugin owns `--pg`, whose value `db_backend` reads. The state is in `config.stash`, since pytester runs nested sessions in one process. A fixture used while the plugin is off fails saying what turns it on, rather than running unguarded.
- `src/strata_python/db/project.py`: `find_table()`, the standard-library half of `Project.load`, so the plugin can ask without SQLAlchemy.
- `pyproject.toml`: `strata_db = true` for this repo's own tests.
- `tests/unit/test_db.py`: a stand-in plugin that adds `--pg` first; `--pg` and `--strata-pg` offered once on; the plugin off (guard not started, drivers not wrapped, `--pg` left to the other plugin, fixtures failing with the reason) with no table and with `strata_db = false`, in a subprocess since the guard is per process. All four failed before the change.
- `README.md` (step 8, tests), `AGENTS.md` (the db plugin rule), `CHANGELOG.md`.

**Scanned pages drawn as PDFs: `strata_python.pages` (branch `claude/project-thread-dvu0cz`, issue #9)**

The owner wants evals of how close a rebuilt PDF of a scanned book page gets to the cleaned scan (layout, figures, typefaces), with strata-models owning the models and the scoring, and the rendering here so the apps that print rebuilt books (strata-scan first) draw with the code that was scored.

- `src/strata_python/pages/model.py` **new**: the description, plain dataclasses with no third-party import so an app can build and send one without the extra. Boxes are `(x, y, w, h)` in scan pixels like Tesseract's; styles, roles, alignments and figure kinds are closed sets checked on construction (`PageError`); `to_dict` leaves defaults out. `Span` carries a font matcher's per-word fit (`x`, `baseline_y`, `size_pt`, `tracking_em`, `hscale`, `stroke_em`), the shape agreed with the strata-models font-match role.
- `src/strata_python/pages/render.py` **new**: ReportLab writes (BSD), pdfium reads back (BSD/Apache); PyMuPDF was ruled out for its AGPL licence. One text object a run, because ReportLab's text cursor ignores tracking and horizontal scale, so positions are worked out here. A line is fitted to its box by glyph spacing only, at most 0.1 em a glyph, so a near face lands where the print was without stretching glyphs; placed words are never refitted. Small capitals are synthesised at 0.78 unless the face is a small-caps cut; extra weight is a stroke round the glyphs (render mode 2). `invariant=1` makes the bytes depend only on the input. Faces are registered once a process under a name from their path, because ReportLab's font registry is global. Both libraries are untyped, so they are reached through `Any`; this module's own signatures are complete (pyright strict, ty).
- `tests/unit/test_pages.py` **new**: real renders read back by pdfium (no mocks): ink inside the line box, fitting within its limit, alignment, size, baseline, tracking, scale, weight, placed words, small capitals, an embedded TrueType face (ReportLab's own Vera), figures, several pages, the text read back, and the description's JSON round trip and refusals.
- `pyproject.toml`: the `pdf` extra, in this repo's dev group so its tests run. `README.md`, `AGENTS.md`: module table and layout.

**The shared database code moves in as `strata_python.db` (branch `claude/project-thread-t4kvbv`)**

The owner asked whether the Python client in the database repo should move here, since the apps pull it from that repo by git commit, and agreed it was worth doing for the SQLite test databases as well. The inventory found eight apps on it at five different commits (four of them from before scratch databases moved off the live cluster), the names carrying a machine's name throughout (package, environment variables, database prefixes, the advisory-lock key, the pyproject table), untyped code, f-string SQL, and a test server that only ran on POSIX. The owner chose `STRATA_DB_*` and `[tool.strata-db]` as the new names.

- `src/strata_python/db/` **new**: `config`, `guard`, `engine`, `models`, `project`, `migrations`, `scratch`, `testserver`, `testing`, `cli`, `pytest_plugin`, `_alembic/env.py` and the revision template, ported and typed (pyright strict, ty). Behaviour is the same except for what follows.
- Names: `STRATA_DB_*` variables, `[tool.strata-db]`, `strata_db_*` throwaway databases, `strata-db-pg-*` server folders, the `strata_db` lock namespace, the `strata-db` command. **Correction to the plan posted to the owner:** it said the old names would keep working here. They cannot: they contain the machine's name, which rule 4 keeps out of this public repo. Apps rename when they move instead (README step 8), and the old package stays where it is for apps that have not moved.
- The shared database has no default name (it was the machine's name). `STRATA_DB_DATABASE` names it, `DatabaseNotConfigured` says what to set, and `new-app` takes `--database` or the variable. `refuse_real_server` checks only when a name is set; the guard keeps tests on throwaway names either way.
- Identifiers in DDL (schemas, roles, databases) go through the dialect's `quote_identifier`, not hand-written quotes.
- `testserver`: binaries also from `PGBIN` and the Windows install folder; on Windows the server listens on loopback on a free port (no Unix socket); the watchdog is a Python process using `oslib.pid_alive` instead of `sh` and `kill -0`, so it works on both.
- `pytest_plugin`: a second entry point (`strata_python_db`). It is registered with every install and imports SQLAlchemy only inside fixtures; the guard is switched on only when SQLAlchemy and Alembic are installed. `strata_python/db/__init__.py` loads its names on first use, so importing the guard needs nothing else.
- `make_base()` is typed as returning a dataclass declarative base (a `TYPE_CHECKING`-only class), so an app's `Note(text=...)` is checked against its annotations.
- Review findings fixed: a failed SQLite migration kept the tables it had created, since pysqlite begins a transaction only before DML (true of the old package too; `_run` now issues `BEGIN`); `new-app` run by a CREATEROLE login that is not superuser could not create the schema it owns, since that login gets only ADMIN on the role (it now grants itself membership). Both have a test that failed first. A third finding, that the plugin imports psycopg as it loads, is the guard working as designed: AGENTS.md now says so instead of claiming the plugin imports nothing.
- Left for later: strata-models' store helpers (`connect(migrate=...)`, `StaleDatabase`, `revisions`, `fast_commits`), which come with the strata-models client; the database repo's `app-db.yml` and docs, which belong to that repo.
- `tests/unit/test_db.py` **new** (the old suite, in-process with `run_cli`, plus the guard outside tests, async and psycopg2 connections, the fixtures on SQLite in this run and on Postgres through `pytester` three ways), `tests/integration/test_db_cli.py` **new** (the flows that rewrite models between steps, as `python -m strata_python.db`). The Postgres tests no longer skip silently: CI sets `STRATA_TEST_PG=1` on Linux and Windows, where a missing server fails them; they skip on macOS, which has no server binaries, and say why.
- `pyproject.toml`: the `db` extra (in this repo's dev group so its tests run), the `strata-db` script, the second plugin entry point (disabled in addopts like the first), `S608` allowed in tests (SQL naming generated test schemas), `__main__.py` omitted from coverage. `tests/conftest.py` loads both plugins.
- `.github/workflows/ci.yml`: PostgreSQL server binaries on Linux, `STRATA_TEST_PG`.
- `README.md` (step 8 and the module table), `CHANGELOG.md`, `AGENTS.md` (layout, plugins).

**A shared testing module and an app-facing changelog (branch `claude/project-thread-t4kvbv`)**

The owner asked for a test module so every app uses the same testing code, and for a changelog apps can read to see what each release adds. The survey found `tests/utils/fakes.py` in seven apps in five versions whose behaviour is the same (only docstrings, formatting and one app's torch mocks differ), the same three fixtures in every root conftest, the same integration auto-marker, and `cli`/`cli_subprocess` fixtures that differ only in a docstring.

- `src/strata_python/testing.py` **new**: the fakes, typed, with three fixes over the copies: `FakePopen.fresh()` gives each test its own `instances` (the copies shared a class list that each fixture reset); `FakeRun.check_output` raises on a non-zero `.code` like the real one; `FakeUrlopen.calls` holds `Call` objects that keep `call["url"]` working. `run_cli` and `run_module` replace the integration conftests' `cli` and `cli_subprocess`, with the app's `main` and module passed in. Torch and transformers mocks stay in the one app that uses them.
- Review findings fixed: `FakeResponse.read(n)` now consumes its body, so a chunked read loop reaches EOF (the copies returned the first chunk forever); the integration marker looks at the path relative to the pytest root, so a checkout under a folder called `integration` is not all marked.
- `src/strata_python/pytest_plugin.py`: `fake_urlopen`, `fake_popen`, `fake_run`, `no_sleep` fixtures; `strata_env` ini option (KEY=VALUE, `{env}` for the isolated dir), which covers what the apps' own `isolated_env` copies added; the `integration` marker registered and added under `tests/integration` (`strata_auto_integration`).
- `tests/unit/test_testing.py` **new**, `tests/unit/test_plugin.py`: the fakes through real `urllib` and `subprocess` calls; the runners against a real module; the new ini options in a child pytest.
- `CHANGELOG.md` **new** (app-facing, by version, `Unreleased` first) and `DEVLOG.md` (this file, the old `CHANGELOG.md` renamed): apps had only a dated development log to read. `release.yml` turns `## Unreleased` into the version heading in its bump PR and uses that section as the GitHub release notes; the sdist ships `CHANGELOG.md`.
- `pyproject.toml`: a `dev` extra with the tools and pytest plugins (this repo's own dev group is now `strata-python[dev]`, so it tests the same set the apps get); caps on the runtime dependencies. The owner asked whether the library could hold every app's dependencies so dependabot stops raising a PR per package per app: the shared ones live here now, but each app's `uv.lock` still needs its own bumps, so the README gives apps a dependabot config that groups them into one weekly PR. Exact pins (`==`) were rejected: every app would move in lockstep and one app's need would block the rest.
- `README.md`: rewritten as the guide to moving an app onto the library (asked for by the owner): dependency, gate, settings, server, CLI, oslib, tests, a done-when check and how to keep current, each step saying what the app deletes.
- `AGENTS.md`: the two logs, the testing module, and that additive releases are patches while 0.x (a "feat" commit would release 0.3.0, which apps capped at `<0.3` do not get).

**Publish to PyPI, licence, and an opinionated scope (branch `claude/project-thread-t4kvbv`)**

A survey of the strata apps found none pinning a tag (none exists; they track main at six different commits), and the owner asked to publish to PyPI and to make the library opinionated for the strata apps, with nothing personal or machine-specific in it since the repo is public. A PyPI upload is permanent, so the clean-up and a check come before the first release.

- `LICENSE` **new**: MIT, as the npm libraries use. The repo had no licence, so it was all rights reserved.
- `pyproject.toml`: version 0.2.0 (not 0.1.0, so the first release cannot be confused with the untagged commits apps use today); `license`, `license-files`, `authors` (a handle, no email), `urls`, `classifiers`; `hatchling>=1.27` for SPDX licences; an sdist `include` list, because Hatch put every tracked file (agent notes, CI, `uv.lock`) in it; the `strata-public-check` script.
- `src/strata_python/public_check.py` **new**, `tests/unit/test_public_check.py` **new**: `strata-public-check` scans folders, sdists and wheels for email addresses (noreply addresses allowed), private IPv4 addresses, home-folder paths (placeholder users allowed) and the owner's own regexes from `STRATA_PUBLIC_DENY`. Those come from a secret so the names never enter the repo, and their matches are printed as "(hidden)" because CI logs are public.
- `.github/workflows/release.yml` **new**: leaflet-node's release flow (bump PR, merge, tag, GitHub release) with `uv version` in place of `npm version`. With no tag yet the first release is exactly the version in `pyproject.toml`; dependabot's `update-type: version-update:semver-major` trailers are ignored when choosing the bump, since `\bmajor\b` matches them (leaflet-node's copy has the same problem).
- `.github/workflows/pypi-publish.yml` **new**: on a published release (or by hand for a tag), checks the tag matches the version, runs the gate and integration tests, builds, runs `strata-public-check` on the sdist and wheel, and uploads with PyPI trusted publishing.
- `.github/workflows/ci.yml`: runs `strata-public-check` on the tree and a fresh build.
- `AGENTS.md`: "Generic or not here" becomes "Opinionated, for the strata apps" (strata service contracts and clients belong here; naming an app is fine, a person or machine is not); rule 4 widened to names, emails and port maps, enforced by the check; apps depend on a version range; the release flow; layout.
- `README.md`: install from PyPI with a version range (it told apps to pin tag `v0.1.0`, which never existed); a git `rev` only to try an unreleased commit; `strata-public-check`; releases.
- `src/strata_python/__init__.py`: neutral module docstring (it named the owner); lists `public_check`.
- `CHANGELOG.md`: the 2026-11-09 entry no longer names the owner (correction for rule 4, not a change of fact).

## 2026-11-09

**`Settings(prefix=...)` (branch `claude/project-thread-iw7199`)**
An app that moves onto the library may already run as a service with short environment variables (`MA_*`); the default prefix (`MY_APP_`) would silently ignore them.

- `src/strata_python/settings.py`: optional keyword `prefix` names the environment variables; default unchanged. `tests/unit/test_settings.py`: test for it.

**Create the library (branch `claude/project-thread-iw7199`)**

The owner asked for a `strata-python` project holding the server code the template and the strata apps were duplicating (six drifted copies of `oslib.py`, five of `settings.py`, copies of the server skeleton, CLI commands, hermetic test fixtures and `scripts/check.py`). This is its first version, built from the template's typed modules and made generic. The apps and the template have not been moved onto it yet.

- `src/strata_python/oslib.py` **new**: from the template; `cli_command(module)` is now generic and no longer edits `PYTHONPATH`/`PYTHONWARNINGS` in the environment (the template's did, and assumed its own source layout).
- `src/strata_python/settings.py` **new**: `Settings`/`Setting` replace the per-app `settings.py` (fixed keys, `APP_*` variables, hand-written getters). Validation lives in the setting; a bad stored value falls back to the default instead of crashing. `save` holds a thread lock and a file lock around the whole read-merge-write and writes through a unique temp file (found in review: concurrent PUTs lost updates or hit a missing temp file), and an empty string is a value, not "unset".
- `src/strata_python/web.py` **new**: `with_base`, `static_file`, health and settings routes, `add_web` and `serve`, from the template's `server/app.py`. The settings routes are generic (`PUT /api/settings` takes any declared key; unknown key or invalid value is a 400).
- `src/strata_python/cli.py` **new**: `doctor`, `set` and `serve` subcommands.
- `src/strata_python/http_server.py`, `live.py`, `pytest_plugin.py` **new**: the scripted loopback HTTP server, a real uvicorn-on-loopback helper, and the pytest plugin (hermetic environment, no-network guard, `http_server` and `live_app` fixtures). The template's `fake_urlopen`, `fake_popen` and `fake_run` fixtures are deliberately **not** carried over (see AGENTS.md, Testing policy).
- `src/strata_python/check.py` **new**: `strata-check`, the template's `scripts/check.py` with configurable paths. `--fix` applies safe fixes only.
- `pyproject.toml`, `uv.lock`, `scripts/check`, `.github/workflows/ci.yml`, `.github/dependabot.yml`, `AGENTS.md`, `CLAUDE.md`, `README.md` **new**: uv, ruff + `ruff format` at 120, pyright strict and ty, CI on Linux (Python 3.11-3.13), macOS and Windows.
- CI gates the coverage floor on Linux only: oslib has a branch per platform, so each other OS covers a different half of it.
- Tests: 59 unit tests (96.6% coverage; Windows-only branches excluded, and `kill_tree`/`file_lock`/`lower_priority` are exercised with real processes) and 5 integration tests (the gate on a tiny project, `serve` in a separate process).
