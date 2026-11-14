import json
import os

import pytest

from strata_python.settings import (
    IMPORTED,
    ListSetting,
    Setting,
    Settings,
    absolute_path,
    boolean,
    iso_date,
    non_empty,
    number_between,
    whole_between,
)


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


def test_an_explicit_prefix_names_the_variables(monkeypatch, isolated_env):
    s = Settings("my-app", [Setting("greeting", default="Hello")], prefix="MA")
    monkeypatch.setenv("MA_HOME", str(isolated_env / "ma-home"))
    monkeypatch.setenv("MA_GREETING", "Yo")
    assert s.home() == str(isolated_env / "ma-home")
    assert s.get("greeting") == "Yo"
    assert s.env_name("greeting") == "MA_GREETING"
    assert os.path.basename(Settings("my-app", []).home()) == ".my-app"  # the folder is still named for the app


# --- typed values, lists and stores (issues #10 and #16) ---


def test_number_whole_date_and_bool_normalisers_give_one_spelling():
    gb = number_between(0, 64)
    assert [gb(v) for v in (" 8 ", "8.50", "0", "64")] == ["8", "8.5", "0", "64"]
    for bad in ("65", "-1", "nan", "inf", "lots"):
        with pytest.raises(ValueError, match="number"):
            gb(bad)
    slots = whole_between(1, 16)
    assert slots(" 4 ") == "4"
    for bad in ("0", "17", "4.0", ""):
        with pytest.raises(ValueError, match="whole number"):
            slots(bad)
    day = iso_date()
    assert (day("2026-11-14"), day("  ")) == ("2026-11-14", "")
    for bad in ("2026-02-30", "20261114", "14/11/2026"):
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            day(bad)
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        iso_date(allow_blank=False)("")
    assert [boolean(v) for v in ("Yes", "1", "off", "FALSE")] == ["true", "true", "false", "false"]
    with pytest.raises(ValueError, match="true or false"):
        boolean("maybe")


@pytest.fixture
def typed(isolated_env):
    return Settings(
        "demo",
        [
            Setting("vram_gb", default="8", normalise=number_between(0, 64)),
            Setting("slots", default="2", normalise=whole_between(1, 16)),
            Setting("until", default="", normalise=iso_date()),
            Setting("gpu", default="false", normalise=boolean),
            ListSetting("source_roots", item=absolute_path, env="DEMO_SOURCES"),
            ListSetting("db_reads", default=("photos",), sep=","),
        ],
    )


def test_typed_getters(typed):
    typed.save({"vram_gb": "12.5", "slots": 4, "gpu": "yes"})  # a number from JSON is accepted as its text
    assert (typed.get_float("vram_gb"), typed.get_int("slots"), typed.get_bool("gpu")) == (12.5, 4, True)
    assert typed.load()["slots"] == "4"
    with pytest.raises(ValueError, match="slots: must be a whole number from 1 to 16"):
        typed.save({"slots": "40"})
    typed.save({"until": "2026-12-01"})
    typed.save({"until": ""})  # a blank date clears the value without resetting it
    assert typed.load()["until"] == ""


def test_list_settings_from_the_file_the_environment_and_save(typed, monkeypatch, isolated_env):
    a, b = str(isolated_env / "a"), str(isolated_env / "b")
    assert typed.get_list("source_roots") == []
    assert typed.get_list("db_reads") == ["photos"]
    typed.save({"source_roots": [a, " ", b]})  # blank entries dropped
    assert typed.load()["source_roots"] == [a, b]
    assert typed.get("source_roots") == os.pathsep.join([a, b])
    typed.save({"db_reads": "photos, faces,"})  # a joined string, as `set` and a hand edit give
    assert typed.get_list("db_reads") == ["photos", "faces"]
    monkeypatch.setenv("DEMO_SOURCES", b)
    assert typed.get_list("source_roots") == [b]
    assert typed.all()["source_roots"] == [b]
    with pytest.raises(ValueError, match="source_roots: must be an absolute path"):
        typed.save({"source_roots": ["relative"]})
    with pytest.raises(TypeError):
        typed.get_list("slots")


def test_a_hand_edited_list_file(typed, isolated_env):
    a = str(isolated_env / "a")
    os.makedirs(typed.home())
    with open(typed.path(), "w", encoding="utf8") as f:
        json.dump({"source_roots": a, "db_reads": [1, 2]}, f)  # a string is split; a list of numbers is ignored
    assert typed.get_list("source_roots") == [a]
    assert typed.get_list("db_reads") == ["photos"]


class DictStore:
    """A store kept in memory, standing in for a database table: the protocol is all Settings needs."""

    def __init__(self, rows=None):
        self.rows = dict(rows or {})

    def load(self):
        return dict(self.rows)

    def update(self, changes):
        for k, v in changes.items():
            if v is None:
                self.rows.pop(k, None)
            else:
                self.rows[k] = v
        return dict(self.rows)


def test_bind_copies_the_files_values_once_then_uses_the_store(typed):
    typed.save({"slots": "3", "db_reads": ["a"]})
    with open(typed.path(), "r+", encoding="utf8") as f:  # a bad value in the file is not carried over
        d = json.load(f)
        d["vram_gb"] = "999"
        f.seek(0)
        json.dump(d, f)
        f.truncate()
    store = DictStore()
    typed.bind(store)
    assert store.rows == {"slots": "3", "db_reads": ["a"], IMPORTED: typed.path()}
    typed.save({"slots": "5"})
    assert store.rows["slots"] == "5"
    assert typed.file.load()["slots"] == "3"  # the file is left as it was
    assert typed.get_int("slots") == 5
    store.rows["slots"] = "6"
    typed.bind(store)  # the marker is there: the file is not copied over the store's values again
    assert typed.get_int("slots") == 6


def test_a_store_given_up_front_and_bind_without_import(isolated_env):
    store = DictStore({"greeting": "Hi"})
    s = Settings("demo", [Setting("greeting", default="Hello")], store=store)
    assert s.get("greeting") == "Hi"
    other = DictStore()
    s.bind(other, import_file=False)
    assert other.rows == {}
    assert s.get("greeting") == "Hello"
