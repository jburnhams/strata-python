import importlib
import importlib.metadata

import pytest

import strata_python


def test_version_comes_from_the_installed_metadata():
    assert strata_python.__version__ == importlib.metadata.version("strata-python")


def test_version_of_an_uninstalled_source_tree(monkeypatch: pytest.MonkeyPatch):
    def missing(_name: str) -> str:
        raise importlib.metadata.PackageNotFoundError

    monkeypatch.setattr(importlib.metadata, "version", missing)
    try:
        assert importlib.reload(strata_python).__version__ == "0+unknown"
    finally:
        monkeypatch.undo()
        importlib.reload(strata_python)


def test_the_network_guard_stops_outbound_connections():
    import socket

    with pytest.raises(AssertionError, match="must not use the network"):
        socket.create_connection(("192.0.2.1", 80), timeout=1)
