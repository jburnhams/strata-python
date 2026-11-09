import json
import os

import pytest

from strata_python.settings import Setting, Settings, absolute_path, non_empty


@pytest.fixture
def settings(isolated_env):
    return Settings(
        "demo",
        [
            Setting("data_dir", default=lambda: str(isolated_env / "default-data"), normalise=absolute_path),
            Setting("greeting", default="Hello", normalise=non_empty),
            Setting("odd", default="x", env="DEMO_CUSTOM_NAME"),
        ],
    )


def test_defaults_when_nothing_is_set(settings):
    assert settings.get("greeting") == "Hello"
    assert settings.keys == ("data_dir", "greeting", "odd")


def test_precedence_environment_then_file_then_default(settings, monkeypatch):
    settings.save({"greeting": "Hi"})
    assert settings.get("greeting") == "Hi"
    monkeypatch.setenv("DEMO_GREETING", "Hey")
    assert settings.get("greeting") == "Hey"
    assert settings.env_name("odd") == "DEMO_CUSTOM_NAME"


def test_home_follows_the_variable_then_the_user_folder(settings, monkeypatch):
    assert settings.home() == os.environ["DEMO_HOME"]
    monkeypatch.delenv("DEMO_HOME")
    assert settings.home() == os.path.join(os.path.expanduser("~"), ".demo")


def test_save_validates_and_leaves_the_file_alone_on_error(settings):
    settings.save({"greeting": "Hi"})
    with pytest.raises(KeyError, match="unknown setting 'nope'"):
        settings.save({"greeting": "Yo", "nope": "1"})
    with pytest.raises(ValueError, match="data_dir: must be an absolute path"):
        settings.save({"greeting": "Yo", "data_dir": "relative/dir"})
    assert settings.load() == {"greeting": "Hi"}


def test_none_resets_to_the_default(settings):
    settings.save({"greeting": "Hi"})
    settings.save({"greeting": None})
    assert settings.load() == {}
    assert settings.get("greeting") == "Hello"


def test_a_bad_stored_or_environment_value_falls_back_instead_of_crashing(settings, monkeypatch, isolated_env):
    os.makedirs(settings.home())
    with open(settings.path(), "w", encoding="utf8") as f:
        json.dump({"greeting": "   ", "data_dir": 5}, f)
    monkeypatch.setenv("DEMO_DATA_DIR", "relative")
    assert settings.get("greeting") == "Hello"
    assert settings.get("data_dir") == str(
        isolated_env / "default-data"
    )  # relative env value and non-string file value both ignored


def test_a_broken_or_non_object_file_reads_as_empty(settings):
    os.makedirs(settings.home())
    for text in ("{not json", "[1, 2]"):
        with open(settings.path(), "w", encoding="utf8") as f:
            f.write(text)
        assert settings.load() == {}


def test_all_and_folder(settings, isolated_env):
    assert settings.all()["greeting"] == "Hello"
    folder = settings.folder("data_dir")
    assert os.path.isdir(folder)
    assert folder == str(isolated_env / "default-data")


def test_absolute_path_expands_the_user_folder():
    assert absolute_path("~/x") == os.path.join(os.path.expanduser("~"), "x")


def test_prefix_of_a_dashed_app_name():
    assert Settings("my-app", []).prefix == "MY_APP"


def test_an_empty_value_round_trips_when_the_setting_allows_it(settings):
    settings.save({"odd": ""})
    assert settings.get("odd") == ""


def test_concurrent_saves_of_different_keys_lose_nothing(settings):
    import threading

    keys = [f"k{i}" for i in range(12)]
    many = Settings("demo", [Setting(k, default="") for k in keys])
    errors: list[BaseException] = []

    def write(k: str) -> None:
        try:
            many.save({k: "v"})
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=write, args=(k,)) for k in keys]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert many.load() == dict.fromkeys(keys, "v")
    assert [n for n in os.listdir(many.home()) if n.endswith(".tmp")] == []
