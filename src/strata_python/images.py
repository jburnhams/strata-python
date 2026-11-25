"""Pictures the right way up, and cached JPEG thumbnails of them (needs `strata-python[images]`, which is Pillow).

strata-360 (`photos.thumb`, a thumb route per kind), strata-stills (`browser.cached`), bright-horizons and strata-scan
each make a small JPEG of a picture on first request and keep it. Each copy turns it by its EXIF orientation, flattens
transparency and 16-bit greys, keys the cache on the file and the size, and writes through a temp file; each copy does
a different subset of that. Here:

    path = thumbnail(src, 320, cache_dir=settings.thumbs_dir())      # the cached JPEG's path, made if missing
    path = thumbnail(raw, 320, cache_dir=d, make=develop_preview)    # the app's own reader for what Pillow cannot open
    path = thumbnail(src, 1600, cache_dir=d, fmt="webp", quality=80)   # a medium copy to keep: WebP, a third smaller
    path = crop(src, (x0, y0, x1, y1), 160, cache_dir=d, pad=1.6)      # one face (any box), square, cached as WebP
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


def webp_bytes(im: Image.Image, quality: int = 80) -> bytes:
    """A lossy WebP: about two thirds the bytes of a JPEG that looks the same, which matters for a copy of every
    picture an app indexed (strata-models' face store keeps a medium copy of each, to show a face in its picture)."""
    buf = io.BytesIO()
    flatten(im).save(buf, "WEBP", quality=quality, method=4)
    return buf.getvalue()


FORMATS: dict[str, tuple[str, Callable[[Image.Image, int], bytes]]] = {
    "jpeg": (".jpg", jpeg_bytes),
    "webp": (".webp", webp_bytes),
}


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
    fmt: str = "jpeg",
) -> str:
    """The path of a JPEG (or, with `fmt="webp"`, a WebP) of `src` whose long side is at most `px`, made on first
    request and kept in `cache_dir`. `make(src, px)` returns the picture for files Pillow cannot read (a camera raw's
    embedded preview, a video frame); it should return one at least `px` on its long side, and it is still turned,
    shrunk and flattened here."""
    px = clamp_px(px)
    path = os.fspath(src)
    ext, encode = _format(fmt)
    key = stat_key(path, px, quality, version, *([] if fmt == "jpeg" else [fmt]))  # a JPEG keeps its old key
    out = os.path.join(os.fspath(cache_dir), key[:2], key + ext)

    def build() -> bytes:
        im = make(path, px) if make else open_upright(path, draft=(px, px))
        im = ImageOps.exif_transpose(im) if make else im
        im.thumbnail((px, px), Image.Resampling.LANCZOS)  # never enlarges
        return encode(im, quality)

    return _cached(out, build, slots)


def crop(
    src: str | os.PathLike[str],
    box: tuple[float, float, float, float],
    px: int,
    *,
    cache_dir: str | os.PathLike[str],
    pad: float = 1.0,
    square: bool = True,
    quality: int = 85,
    version: object = 1,
    make: Callable[[str, int], Image.Image] | None = None,
    slots: int = 4,
    fmt: str = "webp",
) -> str:
    """The path of a cached picture of one region of `src`: `box` is `(x0, y0, x1, y1)` in the upright picture's
    pixels (a face, an OCR line), grown `pad` times about its centre (1.6 shows a face with its hair and chin),
    made square around that centre when `square` (so a grid of crops lines up), clipped to the picture, and shrunk
    to `px` on its long side. Review screens of faces in bright-horizons, strata-360 and strata-models each cut
    these by hand. `make(src, 0)` reads what Pillow cannot at full size (see `thumbnail`); a box given in another copy's pixels
    (a medium copy) must be scaled to the picture `make` returns first."""
    x0, y0, x1, y1 = (float(v) for v in box)
    if not (x1 > x0 and y1 > y0):
        raise ValueError(f"an empty box: {box}")
    px = clamp_px(px)
    path = os.fspath(src)
    ext, encode = _format(fmt)
    key = stat_key(
        path, "crop", round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1), pad, square, px, quality, version, fmt
    )
    out = os.path.join(os.fspath(cache_dir), key[:2], key + ext)

    def build() -> bytes:
        im = ImageOps.exif_transpose(make(path, 0)) if make else open_upright(path)
        w, h = (x1 - x0) * pad, (y1 - y0) * pad
        if square:
            w = h = max(w, h)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        region = (
            max(0, round(cx - w / 2)),
            max(0, round(cy - h / 2)),
            min(im.width, round(cx + w / 2)),
            min(im.height, round(cy + h / 2)),
        )
        if region[2] <= region[0] or region[3] <= region[1]:
            raise ValueError(f"box {box} is outside the {im.width}x{im.height} picture")
        part = im.crop(region)
        part.thumbnail((px, px), Image.Resampling.LANCZOS)
        return encode(part, quality)

    return _cached(out, build, slots)


def _format(fmt: str) -> tuple[str, Callable[[Image.Image, int], bytes]]:
    if fmt not in FORMATS:
        raise ValueError(f"fmt is one of {sorted(FORMATS)}, not {fmt!r}")
    return FORMATS[fmt]


def _cached(out: str, build: Callable[[], bytes], slots: int) -> str:
    """Make `out` once: a second request for it while it is being made waits for that one instead of making it again."""
    if os.path.exists(out):
        return out
    with _LIMITS_LOCK:
        making = _MAKING.setdefault(out, threading.Lock())
    with making:
        if os.path.exists(out):  # made by another thread while this one waited
            return out
        with _slots(slots):
            write_bytes_atomic(out, build())
    with _LIMITS_LOCK:
        _MAKING.pop(out, None)
    return out
