# Repository Guidelines

This file is for people and coding agents (`CLAUDE.md` imports it).

## What this is

The shared Python server code for the strata apps: the modules every app copied from [python-app-template](https://github.com/jburnhams/python-app-template) (`oslib`, `settings`, the server skeleton, the CLI commands, the hermetic test fixtures and the `scripts/check` gate), and the components, data shapes and service clients those apps have in common. Apps depend on it from PyPI with a version range (see the README). The repo is public and every release is uploaded to PyPI, where an upload is permanent.

## Rules for every change

1. **Run `scripts/check` before every commit** (`--fast` skips the tests, `--fix` applies safe ruff fixes), and `uv run pytest tests/integration`. CI runs the same. Never skip, disable or quarantine a test or a check to get green. Never use `ruff --unsafe-fixes`: it rewrites code in ways only tests can catch.
2. **Changelog, development log and docs in the same commit**: `CHANGELOG.md` is for the apps: a line under `## Unreleased` in `### Added`, `### Changed`, `### Fixed` or `### Breaking` for anything an app can see, written as what an app can now do or must do (a breaking line ends with the one-line migration); the release workflow turns `Unreleased` into the version heading and uses that section as the GitHub release notes. `DEVLOG.md` is for us: newest day first; a bold one-line title, what was asked, then bullets naming each file touched and what changed; **new** for new files; call out corrections to earlier claims. Update the README and this file where they stop being true.
3. **Write the why**: dense code with comments that say why, not what. Link instead of repeating.
4. **No secrets, no personal or machine-specific details**: people's names, email addresses, hostnames, addresses, user names, local paths, port maps and the owner's infrastructure never appear here, in code, docs, tests or the changelog (examples use placeholders such as `/home/ada/...`, `myapp`). Ports, URLs and service locations come from settings or the environment. `strata-public-check` enforces what it can in CI and before every upload; the owner's own names are in the `STRATA_PUBLIC_DENY` secret, never in the repo.
5. **Types are not optional**: everything in `src/` passes `pyright` (strict) and `ty`, is fully annotated and ships `py.typed`. Style is `ruff format`, line length 120.
6. **Tests come with the change**, and follow the policy below.
7. **Say what you ran**: list the commands and their results; name what you could not run.

## Library rules

- **Opinionated, for the strata apps.** This is not a general-purpose library: it is built for the strata apps and should cater for them directly. A module belongs here when two or more apps need the same thing, or when it is a strata service's contract and client (the shapes a service's endpoints return and the typed client every app gets for free). Each app should be a thin layer of plumbing and business logic over what lives here. Naming a strata app or service is fine; naming a machine or a person is not (rule 4). Behaviour that differs per app is still passed in (a `Settings` declaration, an import list, a `run` callback), never configured by editing this package.
- **Backward compatibility matters**: apps depend on a version range (`strata-python>=0.2,<0.3`) and upgrade deliberately. Breaking a public name, a function signature or a fixture needs a major (or, while 0.x, a minor) version bump, a `### Breaking` line in CHANGELOG.md that ends with the migration in one sentence. The version lives only in `pyproject.toml`.
- **Windows is supported**: OS differences live in `oslib`; do not import `fcntl` or call `os.kill(pid, 0)` elsewhere. Windows-only branches (`if WIN:`) are excluded from the coverage number but run on the Windows CI leg.
- **The pytest plugin changes every app's tests**: its autouse fixtures must stay cheap and safe to have installed (`isolated_env` only touches the app prefix when `strata_env_prefix` is set). This repo loads both plugins from `tests/conftest.py` and disables their entry points (`-p no:strata_python -p no:strata_python_db`) so coverage sees their lines. The db plugin is registered in every app: without the db extra it must import nothing beyond the standard library and pytest. With the extra it imports the installed Postgres drivers as it loads, on purpose: the guard has to wrap them before conftest.py and the app's modules can open a connection, so it cannot wait for a fixture. SQLAlchemy and Alembic still load only when a fixture runs.

## Testing policy

Prefer real code over mocks. A test that runs the real thing finds the bugs that matter; a mock only proves the code agrees with the test.

- Real databases: SQLite files and a throwaway Postgres (the private test server; CI sets `STRATA_TEST_PG=1` so a missing server fails instead of skipping).
- Real HTTP requests against real servers (`live_app`, `http_server`), real subprocesses (`kill_tree`, `file_lock` across processes, `serve`, the `strata-check` gate on a tiny project), real files in `tmp_path`.
- Mocks only for a true external, in quick unit tests, and never for this package's own modules. The apps' fakes for those edges (`strata_python.testing`, exposed as the plugin's `fake_*` fixtures) live here so every app uses the same ones; they are tested here against real `urllib` and `subprocess` calls. The one stdlib patch here is `importlib.metadata.version` (the uninstalled-source-tree fallback).
- No silent skips and no swallowed errors; `pytest.importorskip` is banned (ruff). A deliberate skip is a marker or `skipif` with a reason and an explicit switch; any new skip is mentioned in the PR.
- Unit tests (`tests/unit`, the coverage number, `fail_under` in `pyproject.toml`, only ratchets up) are fast; `tests/integration` runs processes end to end.

## Layout

```
src/strata_python/   oslib, settings, web, cli, http_server, live, testing, pytest_plugin, check, public_check; py.typed
  db/                the [db] extra: config, guard, engine, models, project, migrations, scratch, testserver, testing, cli, pytest_plugin, _alembic/
tests/unit/          one test module per module; tests/conftest.py loads the plugin and holds the tiny_project fixture
tests/integration/   the gate on a tiny project; serve in a separate process
scripts/check        the gate (uv run --locked strata-check)
.github/workflows/   ci.yml; release.yml (bump PR, tag, GitHub release); pypi-publish.yml (check, build, public check, upload)
```

## Tooling (uv)

`uv sync` builds `.venv` from `uv.lock`; `uv add <pkg>` / `uv add --group dev <pkg>` change dependencies (commit `pyproject.toml` and `uv.lock` together). CI installs with `uv sync --locked`. Releases are automatic: after CI passes on main, `release.yml` opens and merges a version-bump PR (major for a commit saying major or breaking, minor for minor or feat, else patch; while 0.x, an additive change is a patch so apps on `>=0.N,<0.N+1` get it, so keep "feat" and "minor" out of its commit messages and PR title; mark a breaking change "minor", since "breaking" or "major" releases 1.0.0), tags it `vX.Y.Z` and creates the GitHub release, and `pypi-publish.yml` checks, builds, runs `strata-public-check` on the sdist and wheel, and uploads to PyPI with trusted publishing.

## Before you say it is done

`scripts/check` passes, the integration tests pass, the changelog has its entry, the docs match the code. Report the commands and their results.
