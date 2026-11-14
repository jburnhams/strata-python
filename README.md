# strata-python

Common code for the strata Python web server apps (FastAPI + CLI), the Python counterpart of [strata-react](https://github.com/jburnhams/strata-react). Every app made from [python-app-template](https://github.com/jburnhams/python-app-template) used to carry its own drifting copy of these modules; they live here once. It is opinionated and built for those apps rather than as a general-purpose library, but it is public, MIT-licensed and on PyPI.

| Module | What it gives an app |
|---|---|
| `strata_python.oslib` | Cross-platform file locks, process liveness (`pid_alive`), `kill_tree`, `lower_priority`, `venv_python`, `cli_command(module)` |
| `strata_python.settings` | `Settings` / `Setting`: one JSON file in `~/.<app>` with `<APP>_*` environment overrides, validation (`absolute_path`, `non_empty`), atomic saves |
| `strata_python.web` | `base_app` (health + settings routes), `add_web` (built web app or stand-in page at every non-API path), `with_base` (reverse-proxy-safe `<base href>`), `static_file` (path-safe), `serve` |
| `strata_python.cli` | `add_doctor`, `add_set`, `add_serve` for the app's argparse subcommands |
| `strata_python.http_server` / `live` | A real scripted HTTP server and a real uvicorn server on loopback, for tests that make actual requests |
| pytest plugin | Loaded automatically: hermetic home and data folders, a no-network guard, `http_server` and `live_app` fixtures |
| `strata-check` | The one gate: lockfile, ruff, `ruff format`, pyright strict, ty, unit tests with coverage |
| `strata-public-check` | Fails on email addresses, private IPv4 addresses, home-folder paths and the regexes in `STRATA_PUBLIC_DENY` in a tree, sdist or wheel; run before anything public is published |

## Use it in an app

```toml
# pyproject.toml
[project]
dependencies = ["strata-python>=0.2,<0.3"]   # from PyPI; while 0.x a minor release may break, so cap below the next one

[tool.pytest.ini_options]
strata_env_prefix = "MYAPP"              # MYAPP_HOME and MYAPP_DATA point into a tmp dir for every test
strata_tuning_vars = ["MYAPP_GREETING"]  # removed from the environment for every test
```

```python
from strata_python import cli, web
from strata_python.settings import Setting, Settings, absolute_path, non_empty

SETTINGS = Settings(
    "myapp",
    [
        Setting("data_dir", default=lambda: "/srv/myapp", normalise=absolute_path),
        Setting("greeting", default="Hello", normalise=non_empty),
    ],
)


def create_app():
    app = web.base_app("myapp", __version__, SETTINGS)  # /api/health, GET/PUT /api/settings
    # ... the app's own /api routes ...
    web.add_web(app, STATIC_DIR, LANDING_HTML)  # last: it registers the catch-all page route
    return app
```

To try an unreleased commit, add `[tool.uv.sources] strata-python = { git = "https://github.com/jburnhams/strata-python", rev = "<sha>" }` for the moment and remove it once the release is out.

`scripts/check` in the app is `exec uv run --locked strata-check "$@"`; git hooks and CI call it. Optional `[tool.strata-check]` in `pyproject.toml` sets `paths` (default `src`, `tests`, `scripts`) and `tests` (default `tests/unit`).

## Develop

`uv sync`, then `scripts/check` (add `--fast` to skip the tests, `--fix` for safe ruff fixes). Integration tests (`uv run pytest tests/integration`) run the gate on a tiny project and `serve` in a separate process. Read [AGENTS.md](AGENTS.md) before changing anything; every change gets a [CHANGELOG.md](CHANGELOG.md) entry. Merging to main releases: `release.yml` bumps the version through its own PR, tags it and creates the GitHub release, and `pypi-publish.yml` uploads it to PyPI.
