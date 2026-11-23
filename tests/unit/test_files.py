"""files: atomic writes on real files, hashes against hashlib, paths under a root, the disk cache with a moved clock."""

import hashlib
import json
import os
import subprocess
import sys
import threading

import pytest

from strata_python import files


def test_atomic_path_replaces_on_success_and_keeps_the_extension(tmp_path):
    target = tmp_path / "out" / "voice.wav"
    with files.atomic_path(target) as tmp:
        assert os.path.dirname(tmp) == str(target.parent)  # same folder: os.replace is atomic only on one file system
        assert tmp.endswith(".part.wav")  # ffmpeg picks the format from the extension
        assert os.path.basename(tmp).startswith(".voice.")
        with open(tmp, "wb") as f:
            f.write(b"new")
        assert not target.exists()  # nothing visible until the block ends
    assert target.read_bytes() == b"new"
    assert os.listdir(target.parent) == ["voice.wav"]


def test_atomic_path_keeps_the_old_file_and_removes_the_temp_on_error(tmp_path):
    target = tmp_path / "a.json"
    target.write_text("old")

    def write_then_fail():
        with files.atomic_path(target) as tmp:
            with open(tmp, "w") as f:
                f.write("half")
            raise RuntimeError("the tool failed")

    with pytest.raises(RuntimeError, match="tool failed"):
        write_then_fail()
    assert target.read_text() == "old"
    assert os.listdir(tmp_path) == ["a.json"]


def test_atomic_path_temp_names_differ_so_threads_do_not_share_one(tmp_path):
    names = set()
    for _ in range(20):
        with files.atomic_path(tmp_path / "x.txt") as tmp:
            names.add(tmp)
            with open(tmp, "w") as f:
                f.write("x")
    assert len(names) == 20


def test_atomic_open_writes_and_refuses_modes_that_are_not_a_new_file(tmp_path):
    with files.atomic_open(tmp_path / "t.txt") as f:
        f.write("héllo")
    assert (tmp_path / "t.txt").read_text(encoding="utf8") == "héllo"
    with files.atomic_open(tmp_path / "b.bin", "wb") as f:
        f.write(b"\x00\x01")
    assert (tmp_path / "b.bin").read_bytes() == b"\x00\x01"
    for mode in ("a", "r+", "w+"):
        with pytest.raises(ValueError, match="not 'w' or 'wb'"), files.atomic_open(tmp_path / "z", mode):
            pass


def test_write_helpers_and_read_json(tmp_path):
    files.write_json_atomic(tmp_path / "d" / "j.json", {"when": tmp_path, "n": 1, "name": "Zoë"})
    raw = (tmp_path / "d" / "j.json").read_text(encoding="utf8")
    assert "Zoë" in raw  # not escaped: the file is read by people too
    assert files.read_json(tmp_path / "d" / "j.json") == {"when": str(tmp_path), "n": 1, "name": "Zoë"}
    files.write_text_atomic(tmp_path / "t", "text")
    files.write_bytes_atomic(tmp_path / "b", b"bytes")
    assert (tmp_path / "t").read_text() == "text"
    assert (tmp_path / "b").read_bytes() == b"bytes"
    files.write_json_atomic(tmp_path / "compact.json", [1, 2], indent=None)
    assert (tmp_path / "compact.json").read_text() == "[1, 2]"


def test_read_json_gives_the_default_for_missing_or_broken_files(tmp_path):
    assert files.read_json(tmp_path / "nope.json") is None
    assert files.read_json(tmp_path / "nope.json", default={}) == {}
    assert files.read_json(tmp_path / "nope" / "deeper.json", []) == []
    (tmp_path / "half.json").write_text('{"a": ')
    assert files.read_json(tmp_path / "half.json", "d") == "d"
    (tmp_path / "file").write_text("x")
    assert files.read_json(tmp_path / "file" / "under-a-file.json", 0) == 0


def test_a_reader_never_sees_a_half_written_file(tmp_path):
    """A reader process polls while this one rewrites the file 200 times: every read is a whole document."""
    path = tmp_path / "status.json"
    files.write_json_atomic(path, {"n": 0, "pad": "x" * 50_000})
    reader = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import json,sys,time\n"
            "bad=0\nend=time.time()+3\n"
            "while time.time()<end:\n"
            "    try:\n        d=json.load(open(sys.argv[1]))\n"
            "    except ValueError:\n        bad+=1\n"
            "    if d.get('n')==199: break\n"
            "print(bad)",
            str(path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    for n in range(200):
        files.write_json_atomic(path, {"n": n, "pad": "x" * 50_000})
    out, _ = reader.communicate(timeout=30)
    assert out.strip() == "0"


def test_hashes_match_hashlib_and_stream_big_files(tmp_path):
    data = os.urandom(files.CHUNK * 2 + 123)
    p = tmp_path / "big.bin"
    p.write_bytes(data)
    assert files.sha256(p) == hashlib.sha256(data).digest()
    assert files.sha256_hex(str(p)) == hashlib.sha256(data).hexdigest()
    (tmp_path / "empty").write_bytes(b"")
    assert files.sha256_hex(tmp_path / "empty") == hashlib.sha256(b"").hexdigest()


def test_quick_fingerprint_sees_size_and_ends_but_not_the_middle(tmp_path):
    block = 16
    base = bytes(range(64))
    a = tmp_path / "a"
    a.write_bytes(base)
    fp = files.quick_fingerprint(a, block)
    assert len(fp) == 64
    a.write_bytes(base[:-1] + b"\xff")  # last byte changed
    assert files.quick_fingerprint(a, block) != fp
    a.write_bytes(b"\xff" + base[1:])  # first byte changed
    assert files.quick_fingerprint(a, block) != fp
    a.write_bytes(base + b"\x00")  # one byte longer
    assert files.quick_fingerprint(a, block) != fp
    a.write_bytes(base[:30] + b"\xff" + base[31:])  # the middle: not seen, by design
    assert files.quick_fingerprint(a, block) == fp
    small = tmp_path / "small"
    small.write_bytes(b"abc")  # shorter than a block: read once
    assert files.quick_fingerprint(small, block) == hashlib.sha256(b"3" + b"abc").hexdigest()


def test_stat_key_changes_with_the_file_and_the_extras(tmp_path):
    p = tmp_path / "pic.jpg"
    p.write_bytes(b"one")
    k = files.stat_key(p, 320)
    assert files.stat_key(str(p), 320) == k
    assert files.stat_key(p, 640) != k
    os.utime(p, ns=(1, 2))
    assert files.stat_key(p, 320) != k


@pytest.mark.parametrize("bad", ["../outside.txt", "a/../../outside.txt", "/etc/passwd"])
def test_inside_refuses_paths_that_leave_the_root(tmp_path, bad):
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(ValueError, match="is outside"):
        files.inside(root, bad)


def test_inside_resolves_paths_under_the_root_and_refuses_a_link_out(tmp_path):
    root = tmp_path / "root"
    (root / "a").mkdir(parents=True)
    assert files.inside(root, "a/b.txt") == os.path.realpath(root / "a" / "b.txt")
    assert files.inside(root, str(root / "a")) == os.path.realpath(root / "a")
    assert files.inside(root, ".") == os.path.realpath(root)
    (tmp_path / "rootless").mkdir()  # a sibling whose name starts with the root's: not inside it
    with pytest.raises(ValueError, match="is outside"):
        files.inside(root, "../rootless/x")
    if sys.platform != "win32":
        os.symlink(tmp_path, root / "out")
        with pytest.raises(ValueError, match="is outside"):
            files.inside(root, "out/secret")


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_disk_cache_bytes_with_age(tmp_path):
    clock = Clock()
    c = files.DiskCache(tmp_path / "c", clock=clock)
    assert c.get("k") is None
    c.put("k", b"body\nwith newline")
    assert c.get("k") == b"body\nwith newline"
    clock.t += 50
    assert c.get("k", max_age_s=60) == b"body\nwith newline"
    assert c.get("k", max_age_s=40) is None
    assert c.get("k") == b"body\nwith newline"  # no age asked: any copy
    c.forget("k")
    c.forget("k")  # forgetting twice is fine
    assert c.get("k") is None
    assert os.path.dirname(c.path("k")).startswith(str(tmp_path / "c"))


def test_disk_cache_json_fetches_once_caches_none_and_not_errors(tmp_path):
    clock = Clock()
    c = files.DiskCache(tmp_path, clock=clock)
    calls = []

    def fetch():
        calls.append(1)
        return None  # "nothing there" is an answer

    assert c.json("isbn:1", fetch, 100) is None
    assert c.json("isbn:1", fetch, 100) is None
    assert len(calls) == 1
    clock.t += 101
    assert c.json("isbn:1", fetch, 100) is None
    assert len(calls) == 2

    def boom():
        raise OSError("down")

    with pytest.raises(OSError, match="down"):
        c.json("isbn:2", boom, 100)
    assert c.get("isbn:2") is None
    assert c.json("isbn:2", lambda: {"title": "T"}) == {"title": "T"}


def test_disk_cache_ignores_a_damaged_entry(tmp_path):
    c = files.DiskCache(tmp_path)
    c.put("k", b"[1]")
    with open(c.path("k"), "wb") as f:
        f.write(b"not a header\n[1]")
    assert c.get("k") is None
    assert c.json("k", lambda: [2]) == [2]
    with open(c.path("k"), "wb") as f:
        f.write(json.dumps({"at": 1}).encode() + b"\nnot json")
    assert c.json("k", lambda: [3]) == [3]


def test_disk_cache_is_safe_across_threads(tmp_path):
    c = files.DiskCache(tmp_path)
    errors = []

    def work(i):
        try:
            for _ in range(30):
                c.put("shared", str(i).encode() * 1000)
                got = c.get("shared")
                assert got is not None
                assert len(set(got.decode())) == 1  # never a mix of two writers
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
