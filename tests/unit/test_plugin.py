"""The plugin runs inside a child pytest (pytester), so its fixtures are tested the way an app uses them."""

import pytest


@pytest.fixture
def child(pytester: pytest.Pytester):
    pytester.makeini("[pytest]\nstrata_env_prefix = APPX\nstrata_tuning_vars = APPX_TUNE\n")
    return pytester


def test_environment_is_isolated_and_tuning_vars_removed(child, monkeypatch):
    monkeypatch.setenv("APPX_TUNE", "fast")
    child.makepyfile(
        """
        import os

        def test_it(isolated_env, tmp_path):
            assert os.environ["HOME"] == str(isolated_env / "home")
            assert os.environ["APPX_HOME"] == str(isolated_env / "app-home")
            assert os.environ["APPX_DATA"] == str(isolated_env / "data")
            assert "APPX_TUNE" not in os.environ
            assert not str(tmp_path).startswith(str(isolated_env))
        """
    )
    child.runpytest_subprocess("-p", "no:randomly").assert_outcomes(passed=1)


def test_without_a_prefix_only_home_is_isolated(pytester: pytest.Pytester):
    pytester.makepyfile(
        """
        import os

        def test_it(isolated_env):
            assert os.environ["HOME"] == str(isolated_env / "home")
            assert "APPX_HOME" not in os.environ
        """
    )
    pytester.runpytest_subprocess().assert_outcomes(passed=1)


def test_outbound_connections_fail_but_loopback_works(child, http_server):
    http_server.route("GET", "/ok", "fine")
    child.makepyfile(
        f"""
        import socket, urllib.request, pytest

        def test_loopback():
            assert urllib.request.urlopen("{http_server.url}/ok").read() == b"fine"

        def test_outside():
            with pytest.raises(AssertionError, match="must not use the network"):
                socket.create_connection(("192.0.2.1", 80), timeout=1)
        """
    )
    child.runpytest_subprocess().assert_outcomes(passed=2)


def test_http_server_fixture_fails_the_test_for_an_unrouted_request(child):
    child.makepyfile(
        """
        import urllib.error, urllib.request

        def test_forgot_a_route(http_server):
            try:
                urllib.request.urlopen(http_server.url + "/missing")
            except urllib.error.HTTPError:
                pass
        """
    )
    r = child.runpytest_subprocess()
    r.assert_outcomes(passed=1, errors=1)
    r.stdout.fnmatch_lines(["*requests no route answered*"])


def test_live_app_fixture_serves_a_fastapi_app(child):
    child.makepyfile(
        """
        import urllib.request
        from fastapi import FastAPI

        def test_it(live_app):
            app = FastAPI()

            @app.get("/hi")
            def hi():
                return {"hi": 1}

            with live_app(app) as base:
                assert urllib.request.urlopen(base + "/hi").read() == b'{"hi":1}'
        """
    )
    child.runpytest_subprocess().assert_outcomes(passed=1)
