"""Pictures the right way up, and cached JPEG thumbnails of them (needs `strata-python[images]`, which is Pillow).

strata-360 (`photos.thumb`, a thumb route per kind), strata-stills (`browser.cached`), bright-horizons and strata-scan
each make a small JPEG of a picture on first request and keep it. Each copy turns it by its EXIF orientation, flattens
transparency and 16-bit greys, keys the cache on the file and the size, and writes through a temp file; each copy does
a different subset of that. Here:

    path = thumbnail(src, 320, cache_dir=settings.thumbs_dir())      # the cached JPEG's path, made if missing
    path = thumbnail(raw, 320, cache_dir=d, make=develop_preview)    # the app's own reader for what Pillow cannot open
    im = open_upright(src, draft=(640, 640))                         # EXIF-turned, decoded at a reduced size when it can
    rgb = flatten(im)                                                # alpha on white, 16-bit and float scaled to 8-bit
    jpeg_bytes(im, quality=85)

The cache key is `files.stat_key(src, px, quality, version)`: a changed file, size, quality or the app's `version`
(bump it when the way a thumbnail is made changes) is a new file, so there is never a stale one to clear. The long side
is `px` (clamped to 16..4096) and a picture is never enlarged. `slots` limits how many are made at once, since a page of
200 thumbnails would otherwise decode 200 large pictures together; a second request for one being made waits for it
rather than making it again."""

from __future__ import annotations

import io
import os
import threading
from collections.abc import Callable

from PIL import Image, ImageOps

from strata_python.files import stat_key, write_bytes_atomic

MIN_PX, MAX_PX = 16, 4096
_LIMITS: dict[int, threading.BoundedSemaphore] = {}
_LIMITS_LOCK = threading.Lock()
_MAKING: dict[str, threading.Lock] = {}


def open_upright(path: str | os.PathLike[str], *, draft: tuple[int, int] | None = None) -> Image.Image:
    """The picture turned by its EXIF orientation. `draft` lets a JPEG decode at a reduced scale (at least that big),
    which is far faster for a thumbnail of a 24-megapixel photo."""
    im = Image.open(path)
    if draft:
        im.draft("RGB", draft)
    return ImageOps.exif_transpose(im)


def flatten(im: Image.Image, background: tuple[int, int, int] = (255, 255, 255)) -> Image.Image:
    """An 8-bit RGB copy: transparency laid on `background`, 16-bit and float greys scaled to 0..255 (Pillow's plain
    `convert("RGB")` clips a 16-bit grey to white)."""
    if im.mode in ("I;16", "I;16B", "I;16L", "I"):
        im = im.point(lambda v: v / 256).convert("L")  # pyright: ignore[reportUnknownMemberType]
    elif im.mode == "F":
        im = im.point(lambda v: v * 255).convert("L")  # pyright: ignore[reportUnknownMemberType]
    if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        bg = Image.new("RGB", rgba.size, background)
        bg.paste(rgba, mask=rgba.getchannel("A"))
        return bg
    return im.convert("RGB")


def jpeg_bytes(im: Image.Image, quality: int = 85) -> bytes:
    buf = io.BytesIO()
    flatten(im).save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def clamp_px(px: int) -> int:
    return max(MIN_PX, min(int(px), MAX_PX))


def _slots(n: int) -> threading.BoundedSemaphore:
    with _LIMITS_LOCK:
        return _LIMITS.setdefault(n, threading.BoundedSemaphore(n))


def thumbnail(
    src: str | os.PathLike[str],
    px: int,
    *,
    cache_dir: str | os.PathLike[str],
    quality: int = 85,
    version: object = 1,
    make: Callable[[str, int], Image.Image] | None = None,
    slots: int = 4,
) -> str:
    """The path of a JPEG of `src` whose long side is at most `px`, made on first request and kept in `cache_dir`.
    `make(src, px)` returns the picture for files Pillow cannot read (a camera raw's embedded preview, a video frame);
    it should return one at least `px` on its long side, and it is still turned, shrunk and flattened here."""
    px = clamp_px(px)
    path = os.fspath(src)
    key = stat_key(path, px, quality, version)
    out = os.path.join(os.fspath(cache_dir), key[:2], key + ".jpg")
    if os.path.exists(out):
        return out
    with _LIMITS_LOCK:
        making = _MAKING.setdefault(out, threading.Lock())
    with making:
        if os.path.exists(out):  # made by another thread while this one waited
            return out
        with _slots(slots):
            im = make(path, px) if make else open_upright(path, draft=(px, px))
            im = ImageOps.exif_transpose(im) if make else im
            im.thumbnail((px, px), Image.Resampling.LANCZOS)  # never enlarges
            write_bytes_atomic(out, jpeg_bytes(im, quality))
    with _LIMITS_LOCK:
        _MAKING.pop(out, None)
    return out
