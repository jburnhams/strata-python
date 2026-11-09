# Repository Guidelines

This file is for people and coding agents (`CLAUDE.md` imports it).

## What this is

The shared Python server code for Jonathan's apps: the modules every app copied from [python-app-template](https://github.com/jburnhams/python-app-template) (`oslib`, `settings`, the server skeleton, the CLI commands, the hermetic test fixtures and the `scripts/check` gate). Apps depend on it through a git source pinned to a tag (see the README). It is public.

## Rules for every change

1. **Run `scripts/check` before every commit** (`--fast` skips the tests, `--fix` applies safe ruff fixes), and `uv run pytest tests/integration`. CI runs the same. Never skip, disable or quarantine a test or a check to get green. Never use `ruff --unsafe-fixes`: it rewrites code in ways only tests can catch.
2. **Changelog and docs in the same commit**: `CHANGELOG.md` (newest day first; a bold one-line title, what was asked, then bullets naming each file touched and what changed; **new** for new files; call out corrections to earlier claims), plus the README and this file where they stop being true.
3. **Write the why**: dense code with comments that say why, not what. Link instead of repeating.
4. **No secrets, no machine-specific details**: hostnames, addresses, user names, local paths and the owner's infrastructure never appear here (examples use placeholders such as `/home/ada/...`, `myapp`).
5. **Types are not optional**: everything in `src/` passes `pyright` (strict) and `ty`, is fully annotated and ships `py.typed`. Style is `ruff format`, line length 120.
6. **Tests come with the change**, and follow the policy below.
7. **Say what you ran**: list the commands and their results; name what you could not run.

## Library rules

- **Generic or not here.** A module belongs here only if two or more apps need the same thing. Nothing here names an app, a domain or a machine. App-specific behaviour is passed in (a `Settings` declaration, an import list, a `run` callback), never configured by editing this package.
- **Backward compatibility matters**: apps pin a tag and upgrade deliberately. Breaking a public name, a function signature or a fixture needs a major (or, while 0.x, a minor) version bump, a CHANGELOG line that says so, and the migration in one sentence. The version lives only in `pyproject.toml`.
- **Windows is supported**: OS differences live in `oslib`; do not import `fcntl` or call `os.kill(pid, 0)` elsewhere. Windows-only branches (`if WIN:`) are excluded from the coverage number but run on the Windows CI leg.
- **The pytest plugin changes every app's tests**: its autouse fixtures must stay cheap and safe to have installed (`isolated_env` only touches the app prefix when `strata_env_prefix` is set). This repo loads the plugin from `tests/conftest.py` and disables the entry point (`-p no:strata_python`) so coverage sees its lines.

## Testing policy

Prefer real code over mocks. A test that runs the real thing finds the bugs that matter; a mock only proves the code agrees with the test.

- Real HTTP requests against real servers (`live_app`, `http_server`), real subprocesses (`kill_tree`, `file_lock` across processes, `serve`, the `strata-check` gate on a tiny project), real files in `tmp_path`.
- Mocks only for a true external, in quick unit tests, and never for this package's own modules. The one stdlib patch here is `importlib.metadata.version` (the uninstalled-source-tree fallback).
- No silent skips and no swallowed errors; `pytest.importorskip` is banned (ruff). A deliberate skip is a marker or `skipif` with a reason and an explicit switch; any new skip is mentioned in the PR.
- Unit tests (`tests/unit`, the coverage number, `fail_under` in `pyproject.toml`, only ratchets up) are fast; `tests/integration` runs processes end to end.

## Layout

```
src/strata_python/   oslib, settings, web, cli, http_server, live, pytest_plugin, check; py.typed
tests/unit/          one test module per module; tests/conftest.py loads the plugin and holds the tiny_project fixture
tests/integration/   the gate on a tiny project; serve in a separate process
scripts/check        the gate (uv run --locked strata-check)
```

## Tooling (uv)

`uv sync` builds `.venv` from `uv.lock`; `uv add <pkg>` / `uv add --group dev <pkg>` change dependencies (commit `pyproject.toml` and `uv.lock` together). CI installs with `uv sync --locked`. Releases are git tags `vX.Y.Z` on main after the version in `pyproject.toml` and the CHANGELOG say so.

## Before you say it is done

`scripts/check` passes, the integration tests pass, the changelog has its entry, the docs match the code. Report the commands and their results.
