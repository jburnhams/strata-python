# Changelog

## 2026-11-09

**Create the library (branch `claude/project-thread-iw7199`)**

Jonathan asked for a `strata-python` project holding the server code the template and the strata apps were duplicating (six drifted copies of `oslib.py`, five of `settings.py`, copies of the server skeleton, CLI commands, hermetic test fixtures and `scripts/check.py`). This is its first version, built from the template's typed modules and made generic. The apps and the template have not been moved onto it yet.

- `src/strata_python/oslib.py` **new**: from the template; `cli_command(module)` is now generic and no longer edits `PYTHONPATH`/`PYTHONWARNINGS` in the environment (the template's did, and assumed its own source layout).
- `src/strata_python/settings.py` **new**: `Settings`/`Setting` replace the per-app `settings.py` (fixed keys, `APP_*` variables, hand-written getters). Validation lives in the setting; a bad stored value falls back to the default instead of crashing. `save` holds a thread lock and a file lock around the whole read-merge-write and writes through a unique temp file (found in review: concurrent PUTs lost updates or hit a missing temp file), and an empty string is a value, not "unset".
- `src/strata_python/web.py` **new**: `with_base`, `static_file`, health and settings routes, `add_web` and `serve`, from the template's `server/app.py`. The settings routes are generic (`PUT /api/settings` takes any declared key; unknown key or invalid value is a 400).
- `src/strata_python/cli.py` **new**: `doctor`, `set` and `serve` subcommands.
- `src/strata_python/http_server.py`, `live.py`, `pytest_plugin.py` **new**: the scripted loopback HTTP server, a real uvicorn-on-loopback helper, and the pytest plugin (hermetic environment, no-network guard, `http_server` and `live_app` fixtures). The template's `fake_urlopen`, `fake_popen` and `fake_run` fixtures are deliberately **not** carried over (see AGENTS.md, Testing policy).
- `src/strata_python/check.py` **new**: `strata-check`, the template's `scripts/check.py` with configurable paths. `--fix` applies safe fixes only.
- `pyproject.toml`, `uv.lock`, `scripts/check`, `.github/workflows/ci.yml`, `.github/dependabot.yml`, `AGENTS.md`, `CLAUDE.md`, `README.md` **new**: uv, ruff + `ruff format` at 120, pyright strict and ty, CI on Linux (Python 3.11-3.13), macOS and Windows.
- CI gates the coverage floor on Linux only: oslib has a branch per platform, so each other OS covers a different half of it.
- Tests: 59 unit tests (96.6% coverage; Windows-only branches excluded, and `kill_tree`/`file_lock`/`lower_priority` are exercised with real processes) and 5 integration tests (the gate on a tiny project, `serve` in a separate process).
