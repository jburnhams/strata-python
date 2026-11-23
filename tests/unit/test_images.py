"""images on real pictures made with Pillow: EXIF orientation, flattening, and the thumbnail cache."""

import os
import threading

import pytest
from PIL import Image

from strata_python import images


def _photo(path, size=(400, 200), orientation=None, colour=(200, 30, 30)):
    im = Image.new("RGB", size, colour)
    im.paste((0, 0, 255), (0, 0, size[0] // 4, size[1] // 4))  # a blue top-left corner to see the turn
    exif = Image.Exif()
    if orientation:
        exif[0x0112] = orientation
    im.save(path, "JPEG", exif=exif.tobytes(), quality=95)
    return path


def test_open_upright_turns_by_exif(tmp_path):
    p = _photo(tmp_path / "p.jpg", orientation=6)  # stored landscape, shown rotated 90° clockwise
    im = images.open_upright(p)
    assert im.size == (200, 400)
    assert images.open_upright(_photo(tmp_path / "q.jpg")).size == (400, 200)


def test_open_upright_with_draft_decodes_smaller(tmp_path):
    p = _photo(tmp_path / "big.jpg", size=(1600, 1200))
    assert images.open_upright(p, draft=(200, 200)).size[0] < 1600


def test_flatten_modes():
    rgba = Image.new("RGBA", (2, 1), (0, 0, 0, 0))
    rgba.putpixel((1, 0), (10, 20, 30, 255))
    flat = images.flatten(rgba)
    assert flat.mode == "RGB"
    assert flat.getpixel((0, 0)) == (255, 255, 255)  # transparent: the background
    assert flat.getpixel((1, 0)) == (10, 20, 30)
    assert images.flatten(rgba, background=(0, 0, 0)).getpixel((0, 0)) == (0, 0, 0)
    grey16 = Image.new("I;16", (1, 1), 65535)
    assert images.flatten(grey16).getpixel((0, 0)) == (255, 255, 255)
    grey16.putpixel((0, 0), 32768)
    assert images.flatten(grey16).getpixel((0, 0)) == (128, 128, 128)  # scaled, not clipped to white
    f = Image.new("F", (1, 1), 0.5)
    assert images.flatten(f).getpixel((0, 0))[0] in (127, 128)
    p = Image.new("P", (1, 1), 0)
    p.info["transparency"] = 0
    assert images.flatten(p).getpixel((0, 0)) == (255, 255, 255)
    assert images.flatten(Image.new("L", (1, 1), 7)).getpixel((0, 0)) == (7, 7, 7)


def test_thumbnail_is_made_once_upright_and_never_enlarged(tmp_path):
    src = _photo(tmp_path / "p.jpg", size=(400, 200), orientation=6)
    cache = tmp_path / "thumbs"
    out = images.thumbnail(src, 100, cache_dir=cache)
    assert out.startswith(str(cache))
    with Image.open(out) as im:
        assert im.size == (50, 100)  # turned upright, long side 100
        assert im.format == "JPEG"
    mtime = os.stat(out).st_mtime_ns
    assert images.thumbnail(src, 100, cache_dir=cache) == out
    assert os.stat(out).st_mtime_ns == mtime  # served from the cache, not made again
    big = images.thumbnail(src, 1000, cache_dir=cache)
    with Image.open(big) as im:
        assert im.size == (200, 400)  # never enlarged
    assert images.thumbnail(src, 100, cache_dir=cache, version=2) != out
    assert images.thumbnail(src, 100, cache_dir=cache, quality=60) != out


def test_thumbnail_follows_a_changed_file(tmp_path):
    src = _photo(tmp_path / "p.jpg")
    first = images.thumbnail(src, 64, cache_dir=tmp_path / "t")
    _photo(src, size=(300, 300), colour=(0, 200, 0))
    os.utime(src, ns=(10**18, 10**18))
    second = images.thumbnail(src, 64, cache_dir=tmp_path / "t")
    assert second != first
    with Image.open(second) as im:
        assert im.size == (64, 64)


def test_thumbnail_size_is_clamped(tmp_path):
    src = _photo(tmp_path / "p.jpg", size=(100, 50))
    with Image.open(images.thumbnail(src, 1, cache_dir=tmp_path / "t")) as im:
        assert max(im.size) == images.MIN_PX
    assert images.clamp_px(10**6) == images.MAX_PX


def test_thumbnail_with_the_apps_own_reader(tmp_path):
    raw = tmp_path / "shot.raw"
    raw.write_bytes(b"not a picture Pillow knows")
    calls = []

    def develop(path, px):
        calls.append((path, px))
        return Image.new("RGBA", (px * 2, px), (0, 128, 0, 255))

    out = images.thumbnail(raw, 80, cache_dir=tmp_path / "t", make=develop)
    with Image.open(out) as im:
        assert im.size == (80, 40)
        assert im.mode == "RGB"
    assert calls == [(str(raw), 80)]
    with pytest.raises(Exception, match="cannot identify image file"):
        images.thumbnail(raw, 80, cache_dir=tmp_path / "t2")  # without the reader Pillow's own error says why


def test_concurrent_requests_make_one_thumbnail(tmp_path):
    src = _photo(tmp_path / "p.jpg", size=(1200, 800))
    made = []

    def make(path, px):
        made.append(1)
        return images.open_upright(path)

    outs: list[str] = []
    threads = [
        threading.Thread(target=lambda: outs.append(images.thumbnail(src, 200, cache_dir=tmp_path / "t", make=make)))
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(outs)) == 1
    assert len(made) == 1
    assert os.listdir(os.path.dirname(outs[0])) == [os.path.basename(outs[0])]  # no temp files left


def test_jpeg_bytes():
    data = images.jpeg_bytes(Image.new("LA", (4, 4)), quality=50)
    assert data[:2] == b"\xff\xd8"
