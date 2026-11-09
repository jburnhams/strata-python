import pytest
from fastapi import FastAPI

from strata_python.live import live_server


def test_live_server_stops_after_the_block():
    import urllib.error
    import urllib.request

    app = FastAPI()
    with live_server(app) as base:
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + "/nothing")
        assert e.value.code == 404
    with pytest.raises(urllib.error.URLError):
        urllib.request.urlopen(base + "/nothing")


def test_live_server_reports_a_server_that_is_not_up_in_time():
    with pytest.raises(RuntimeError, match="did not start"), live_server(FastAPI(), timeout=0):
        pass
