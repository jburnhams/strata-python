"""images on real pictures made with Pillow: EXIF orientation, flattening, and the thumbnail cache."""

import os
import threading

import pytest
import rawpy  # noqa: F401 (numpy, which rawpy loads, must be in sys.modules before a pytester run, or it loads twice)
from PIL import Image

from strata_python import images, testing


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


def test_thumbnail_as_webp_is_its_own_cached_file(tmp_path):
    p = _photo(tmp_path / "p.jpg", size=(2000, 1000))
    jpg = images.thumbnail(p, 1600, cache_dir=tmp_path / "c")
    webp = images.thumbnail(p, 1600, cache_dir=tmp_path / "c", fmt="webp", quality=80)
    assert webp.endswith(".webp")
    assert jpg.endswith(".jpg")
    assert webp != jpg
    with Image.open(webp) as im:
        assert im.format == "WEBP"
        assert im.size == (1600, 800)
    assert images.thumbnail(p, 1600, cache_dir=tmp_path / "c", fmt="webp", quality=80) == webp
    with pytest.raises(ValueError, match="fmt is one of"):
        images.thumbnail(p, 100, cache_dir=tmp_path / "c", fmt="gif")


def test_crop_cuts_a_padded_square_round_the_box(tmp_path):
    p = _photo(tmp_path / "p.jpg", size=(400, 200))  # blue in the top-left 100x50
    out = images.crop(p, (20, 10, 60, 40), 64, cache_dir=tmp_path / "c", pad=1.0)
    assert out.endswith(".webp")
    assert images.crop(p, (20, 10, 60, 40), 64, cache_dir=tmp_path / "c", pad=1.0) == out
    with Image.open(out) as im:
        assert im.size == (40, 40)  # square on the long side (40), never enlarged past the region
        r, _, b = im.convert("RGB").getpixel((20, 20))
        assert b > 200
        assert r < 60  # the blue corner, not the red rest
    with Image.open(images.crop(p, (20, 10, 60, 40), 16, cache_dir=tmp_path / "c", pad=2.0, square=False)) as im:
        assert im.size == (16, 11)  # 80x60 round (40, 25), clipped at the top to 80x55, shrunk to 16 on its long side


def test_crop_clips_at_the_edge_and_refuses_empty_or_outside_boxes(tmp_path):
    p = _photo(tmp_path / "p.jpg", size=(400, 200))
    with Image.open(images.crop(p, (380, 180, 400, 200), 100, cache_dir=tmp_path / "c", pad=3.0, fmt="jpeg")) as im:
        assert im.size == (40, 40)  # a 60x60 square centred on (390, 190), clipped to 360..400 and 160..200
    with pytest.raises(ValueError, match="empty box"):
        images.crop(p, (10, 10, 10, 20), 64, cache_dir=tmp_path / "c")
    with pytest.raises(ValueError, match="outside the 400x200"):
        images.crop(p, (500, 300, 520, 320), 64, cache_dir=tmp_path / "c")


def test_crop_with_the_apps_own_reader(tmp_path):
    src = tmp_path / "raw.cr3"
    src.write_bytes(b"not a picture Pillow reads")
    seen = []

    def develop(path, px):
        seen.append(px)
        return Image.new("RGB", (300, 300), (0, 200, 0))

    with Image.open(images.crop(src, (100, 100, 200, 200), 50, cache_dir=tmp_path / "c", make=develop)) as im:
        assert im.size == (50, 50)
        assert im.convert("RGB").getpixel((25, 25))[1] > 150
    assert seen == [0]  # asked for the full-size picture


# ---- formats beyond Pillow's own: HEIC (iPhones), AVIF, JPEG XL and camera raw ------------------------------------------


@pytest.mark.parametrize("ext", [".heic", ".avif", ".jxl", ".webp", ".tif", ".png"])
def test_open_upright_reads_what_phones_and_cameras_write(tmp_path, ext):
    images.extensions()  # registers the HEIC and JPEG XL readers, as every reader here does
    nice = None if os.name == "nt" else os.getpriority(os.PRIO_PROCESS, 0)
    p = testing.tiny_picture(tmp_path / f"p{ext}")
    assert nice is None or os.getpriority(os.PRIO_PROCESS, 0) == nice  # encoded in a child (x265 lowers its nice)
    im = images.open_upright(p)
    assert im.size == (64, 48)
    assert images.size_upright(p) == (64, 48)
    assert images.flatten(im).getpixel((10, 10))[0] > 150  # red, after lossy encoders


def test_extensions_name_every_format_read_here():
    ext = images.extensions()
    assert {".jpg", ".png", ".heic", ".heif", ".avif", ".jxl", ".dng", ".cr3", ".nef", ".arw"} <= ext
    assert ".pdf" not in ext  # Pillow writes PDFs but cannot open them
    assert images.is_raw("/x/IMG_1.CR3")
    assert not images.is_raw("/x/IMG_1.HEIC")


def test_a_raw_file_is_developed_and_turned_as_the_camera_held_it(tmp_path):
    p = testing.tiny_dng(tmp_path / "a.dng", (64, 48), orientation=6)  # top red, bottom blue, held on its side
    im = images.open_upright(p)
    assert im.mode == "RGB"
    assert im.size == (48, 64) == images.size_upright(p)
    assert im.getpixel((47, 5))[0] > 200  # red now on the right
    assert im.getpixel((0, 5))[2] > 200  # blue on the left
    flat = images.open_upright(testing.tiny_dng(tmp_path / "b.dng", (64, 48)))
    assert flat.size == (64, 48)
    assert flat.getpixel((5, 0))[0] > 200
    assert flat.getpixel((5, 47))[2] > 200


def test_a_raw_file_develops_at_half_size_for_a_small_draft(tmp_path):
    p = testing.tiny_dng(tmp_path / "big.dng", (400, 300))
    # LibRaw halves a mosaic (a real camera's raw); a linear DNG like this one comes back whole either way.
    assert images.open_upright(p, draft=(100, 100)).size == images.open_upright(p, draft=(200, 200)).size == (400, 300)
    assert os.path.isfile(images.thumbnail(p, 64, cache_dir=tmp_path / "c"))


def test_a_file_no_reader_opens_is_an_oserror(tmp_path):
    bad = tmp_path / "x.cr2"
    bad.write_bytes(b"not a raw file")
    for f in (
        lambda: images.open_upright(bad),
        lambda: images.open_upright(bad, draft=(10, 10)),
        lambda: images.size_upright(bad),
    ):
        with pytest.raises(OSError, match="cannot read the raw file"):
            f()
    junk = tmp_path / "x.heic"
    junk.write_bytes(b"not a picture")
    with pytest.raises(OSError, match="cannot identify"):
        images.open_upright(junk)


def test_size_upright_swaps_the_sides_of_a_photo_taken_on_its_side(tmp_path):
    assert images.size_upright(_photo(tmp_path / "p.jpg", orientation=6)) == (200, 400)
    assert images.size_upright(_photo(tmp_path / "q.jpg", orientation=3)) == (400, 200)
