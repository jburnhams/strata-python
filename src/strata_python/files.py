"""Files the apps write, hash and cache: atomic writes, content hashes, safe paths under a root and a disk cache.

The survey found about 120 hand-written `tmp` + `os.replace` sites across strata-360, listory, stills, takeout, tubes
and models, four copies of a chunked sha256 (listory twice, filecat, stills' scripts) and two disk caches (360's places
lookups, listory's provider responses). The copies differ in the details that matter: whether the temp file is in the
same folder (`os.replace` across file systems is not atomic), whether a failed write leaves the temp file behind,
whether two threads writing the same file share a temp name, and whether a tool such as ffmpeg can still tell the
format from the temp name's extension. Here each is decided once:

    with atomic_path("out/voice.wav") as tmp:        # "out/.voice.<random>.part.wav": same folder, same extension
        ffmpeg(["-i", src, tmp])                     # replaced on success, removed on any error
    with atomic_open("notes.json", "w") as f: ...    # the same for a file you write yourself
    write_json_atomic(path, data); write_text_atomic(path, text); write_bytes_atomic(path, raw)
    read_json(path, default={})                      # missing or half-written by an old copy: the default

    sha256(path) -> bytes; sha256_hex(path) -> str   # 1 MiB reads, never the whole file in memory
    quick_fingerprint(path)                          # size + first and last MiB: tells files apart in milliseconds
    stat_key(path, *extra)                           # path, size and mtime (+ what you pass): a cache key that changes with the file

    inside(root, user_path)                          # the resolved path, or ValueError (FastAPI answers 400) if it leaves root
    DiskCache(folder).json(key, fetch, max_age_s)    # fetch() once per max_age_s, its JSON kept on disk

Nothing here imports beyond the standard library."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import sys
import time
from collections.abc import Callable, Generator
from typing import IO, Any, Protocol, TypeVar

T = TypeVar("T")

WIN = sys.platform == "win32"
CHUNK = 1 << 20  # 1 MiB: big enough that the hash, not the syscalls, is the cost; small enough to stay out of memory


def _windows_patient(fn: Callable[[], T], wait_s: float = 2.0) -> T:
    """`fn()`, retried for up to `wait_s` on Windows while it raises PermissionError. Windows refuses to replace a file
    another process (or thread) has open, and refuses to open one while it is being replaced, so a writer and a
    reader that would never see each other on POSIX collide there for a few milliseconds. Elsewhere it runs once."""
    if WIN:
        end = time.monotonic() + wait_s
        while True:
            try:
                return fn()
            except PermissionError:
                if time.monotonic() >= end:
                    raise
                time.sleep(0.01)
    return fn()


# ---- atomic writes ---------------------------------------------------------------------------------------------------


def _temp_name(path: str) -> str:
    """A temp file beside `path`, hidden, unique per call, keeping the extension so tools infer the format from it."""
    folder, name = os.path.split(os.path.abspath(path))
    stem, ext = os.path.splitext(name)
    return os.path.join(folder, f".{stem}.{secrets.token_hex(4)}.part{ext}")


@contextlib.contextmanager
def atomic_path(path: str | os.PathLike[str]) -> Generator[str]:
    """A temp path to write `path` through: on a clean exit it replaces `path` in one step, on an error it is removed.
    A reader sees the old file or the new one, never half of either. The folder is created."""
    target = os.fspath(path)
    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
    tmp = _temp_name(target)
    try:
        yield tmp
        _windows_patient(lambda: os.replace(tmp, target))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def atomic_open(path: str | os.PathLike[str], mode: str = "w", encoding: str | None = "utf8") -> Generator[IO[Any]]:
    """`open(path, mode)` through `atomic_path`: write or text mode only (`w`, `wb`, `x` is not atomic and refused).
    The data is flushed and fsynced before the replace, so a crash right after cannot leave an empty file."""
    if "w" not in mode or "+" in mode:
        raise ValueError(f"atomic_open writes a new file: mode {mode!r} is not 'w' or 'wb'")
    with atomic_path(path) as tmp, open(tmp, mode, encoding=None if "b" in mode else encoding) as f:
        yield f
        f.flush()
        os.fsync(f.fileno())


def write_bytes_atomic(path: str | os.PathLike[str], data: bytes) -> None:
    with atomic_open(path, "wb") as f:
        f.write(data)


def write_text_atomic(path: str | os.PathLike[str], text: str) -> None:
    with atomic_open(path, "w") as f:
        f.write(text)


def write_json_atomic(path: str | os.PathLike[str], data: object, *, indent: int | None = 1) -> None:
    """JSON written atomically; values JSON cannot encode (a datetime, a Path) are written as their `str`."""
    with atomic_open(path, "w") as f:
        json.dump(data, f, indent=indent, default=str, ensure_ascii=False)


def read_json(path: str | os.PathLike[str], default: Any = None) -> Any:
    """The parsed file, or `default` when it is missing or not JSON (half-written by code that did not write it
    atomically, or a file the app never made). A permission error still raises: that is not "no file"."""

    def load() -> Any:
        with open(path, encoding="utf8") as f:
            return json.load(f)

    try:
        return _windows_patient(load)
    except (FileNotFoundError, NotADirectoryError, ValueError):
        return default


# ---- identity ----------------------------------------------------------------------------------------------------------


class _Hasher(Protocol):
    def update(self, data: bytes, /) -> None: ...
    def digest(self) -> bytes: ...
    def hexdigest(self) -> str: ...


def _hash_file(path: str | os.PathLike[str]) -> _Hasher:
    h = hashlib.sha256()
    with open(path, "rb", buffering=0) as f:
        while block := f.read(CHUNK):
            h.update(block)
    return h


def sha256(path: str | os.PathLike[str]) -> bytes:
    """The file's SHA-256 as 32 bytes (what a `LargeBinary` column stores; filecat and listory's store use this)."""
    return _hash_file(path).digest()


def sha256_hex(path: str | os.PathLike[str]) -> str:
    """The file's SHA-256 as 64 hex characters (what a JSON row or a file name uses)."""
    return _hash_file(path).hexdigest()


def quick_fingerprint(path: str | os.PathLike[str], block: int = CHUNK) -> str:
    """A cheap identity: SHA-256 of the size and the first and last `block` bytes, as hex. Two different videos of the
    same size almost never share both ends, and a re-encoded or truncated file changes, so it tells a folder of large
    clips apart in milliseconds. Not a content hash: an edit in the middle of a big file is not seen."""
    size = os.path.getsize(path)
    h = hashlib.sha256(str(size).encode())
    with open(path, "rb") as f:
        h.update(f.read(block))
        if size > block:
            f.seek(max(size - block, block))
            h.update(f.read(block))
    return h.hexdigest()


def stat_key(path: str | os.PathLike[str], *extra: object) -> str:
    """A cache key that changes whenever the file does (its absolute path, size and modification time to the
    nanosecond) and with whatever else the cached thing depends on (`extra`: a size, a version). Hex SHA-256."""
    p = os.path.normcase(os.path.abspath(path))
    st = os.stat(p)
    return hashlib.sha256(json.dumps([p, st.st_size, st.st_mtime_ns, *extra], default=str).encode()).hexdigest()


# ---- paths -------------------------------------------------------------------------------------------------------------


def inside(root: str | os.PathLike[str], path: str | os.PathLike[str]) -> str:
    """`path` (absolute, or relative to `root`) resolved, links followed, and checked to be `root` or under it.
    ValueError otherwise, which `web.add_error_handlers` answers as 400: a route that takes a file name from the
    request passes it through this before opening anything, so `../../etc/passwd` and a link out are refused."""
    base = os.path.realpath(root)
    full = os.path.realpath(os.path.join(base, path))
    if os.path.normcase(full) != os.path.normcase(base) and not os.path.normcase(full).startswith(
        os.path.normcase(os.path.join(base, ""))
    ):
        raise ValueError(f"{os.fspath(path)!r} is outside {os.fspath(root)!r}")
    return full


# ---- a disk cache ------------------------------------------------------------------------------------------------------


class DiskCache:
    """Values kept on disk under a key, each in its own file named by the key's hash, with the time it was stored.

        COVERS = DiskCache(settings.cache_dir() + "/covers")
        COVERS.json(f"openlibrary:{isbn}", lambda: fetch(isbn), max_age_s=7 * 86400)   # None is cached too
        COVERS.get(key, max_age_s) / COVERS.put(key, raw) / COVERS.forget(key)        # bytes

    A cached `None` means "asked, nothing there" and is kept like any value, so a lookup that found nothing is not
    repeated until it is old. Writes are atomic, so several processes can share the folder; the last write wins.
    The clock is injectable for tests."""

    def __init__(self, folder: str | os.PathLike[str], *, clock: Callable[[], float] = time.time) -> None:
        self.folder = os.fspath(folder)
        self._clock = clock

    def path(self, key: str) -> str:
        h = hashlib.sha256(key.encode()).hexdigest()
        return os.path.join(self.folder, h[:2], h + ".cache")

    def get(self, key: str, max_age_s: float | None = None) -> bytes | None:
        """The stored bytes, or None when there are none or they are older than `max_age_s`."""

        def load() -> tuple[bytes, bytes]:
            with open(self.path(key), "rb") as f:
                return f.readline(), f.read()

        try:
            head, body = _windows_patient(load)
            stored = float(json.loads(head)["at"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if max_age_s is not None and self._clock() - stored > max_age_s:
            return None
        return body

    def put(self, key: str, value: bytes) -> None:
        head = json.dumps({"at": self._clock(), "key": key[:200]}).encode() + b"\n"
        write_bytes_atomic(self.path(key), head + value)

    def forget(self, key: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.path(key))

    def json(self, key: str, fetch: Callable[[], Any], max_age_s: float | None = None) -> Any:
        """`fetch()`'s result, from the cache when it is there and young enough, else fetched and stored. An exception
        from `fetch` is not cached (a failure is not an answer)."""
        raw = self.get(key, max_age_s)
        if raw is not None:
            with contextlib.suppress(ValueError):
                return json.loads(raw)
        value = fetch()
        self.put(key, json.dumps(value, default=str).encode())
        return value
