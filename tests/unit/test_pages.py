"""strata_python.pages: the page description round-trips and refuses what cannot be drawn; the renderer's PDF, drawn
back by pdfium (no mocks), puts ink where the description says and reads back as the description's text."""

import io
import json
import os
from pathlib import Path

import pytest
import reportlab
from PIL import Image, ImageOps

from strata_python.pages import Block, Figure, Line, Page, PageError, Span
from strata_python.pages.render import FIT_LIMIT_EM, page_count, rasterise, read_text, render

VERA = os.path.join(os.path.dirname(reportlab.__file__), "fonts", "Vera.ttf")  # a TrueType face ReportLab ships


def ink(img: Image.Image) -> tuple[int, int, int, int]:
    """The bounding box (left, top, right, bottom) of the dark pixels."""
    box = ImageOps.invert(img).point(lambda v: 255 if v > 128 else 0).getbbox()
    assert box is not None, "no ink"
    return box


def one_line(*spans: Span, box=(100, 100, 1000, 80), align="left", **line) -> Page:
    return Page(
        1200,
        600,
        300,
        blocks=[Block(box=(100, 100, 1000, 300), align=align, lines=[Line(box=box, spans=list(spans), **line)])],
    )


# ---- the description --------------------------------------------------------------------------------------------------


def test_a_page_round_trips_through_json():
    p = Page(
        2480,
        3508,
        300,
        blocks=[
            Block(
                box=(10, 20, 300, 40),
                role="heading",
                order=2,
                align="centre",
                lines=[
                    Line(
                        box=(10, 20, 300, 40),
                        baseline_y=52,
                        size_pt=14,
                        face="f",
                        spans=[
                            Span("A ", style="caps"),
                            Span("b", style="italic", face="g", size_pt=12, tracking_em=0.02, hscale=0.95),
                        ],
                    )
                ],
            ),
            Block(box=(10, 80, 300, 40), lines=[Line(box=(10, 80, 300, 40), spans=[Span("c")])]),
        ],
        figures=[Figure(box=(5, 5, 50, 50), image="/srv/app/f.png", kind="engraving"), Figure(box=(1, 2, 3, 4))],
    )
    d = json.loads(json.dumps(p.to_dict()))
    assert Page.from_dict(d) == p
    assert d["blocks"][1]["lines"][0]["spans"] == [{"text": "c"}]  # defaults stay out of the JSON
    assert "image" not in d["figures"][1]


def test_reading_order_puts_numbered_blocks_first_and_text_follows_it():
    first, second, loose = (
        Block(box=(0, 0, 1, 1), order=o, lines=[Line(box=(0, 0, 1, 1), spans=[Span(t)])])
        for o, t in ((1, "one"), (0, "zero"), (None, "loose"))
    )
    p = Page(10, 10, 72, blocks=[loose, first, second, Block(box=(0, 0, 1, 1))])
    assert [b.lines[0].text for b in p.reading_order()[:3]] == ["zero", "one", "loose"]
    assert p.text == "zero\n\none\n\nloose"


def test_a_line_with_only_text_becomes_one_roman_span():
    line = Line.from_dict({"box": [1, 2, 3, 4], "text": "Tesseract's line"})
    assert line.spans == [Span("Tesseract's line")]
    assert line.box == (1.0, 2.0, 3.0, 4.0)


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda: Span("x", style="wavy"), "not one of"),
        (lambda: Span("x", hscale=0), "hscale"),
        (lambda: Block(box=(0, 0, 1, 1), role="sidebar"), "not one of"),
        (lambda: Block(box=(0, 0, 1, 1), align="middle"), "not one of"),
        (lambda: Figure(box=(0, 0, 1, 1), kind="photo"), "not one of"),
        (lambda: Page(0, 10, 300), "positive"),
        (lambda: Page.from_dict({"width": 10, "height": 10}), "width, height and dpi"),
        (lambda: Line.from_dict({"box": [1, 2, 3]}), "x, y, w, h"),
        (lambda: Line.from_dict({"box": "1,2,3,4"}), "x, y, w, h"),
        (lambda: Line.from_dict({"box": [1, 2, "w", 4]}), "four numbers"),
        (lambda: Figure.from_dict({"box": [1, 2, -3, 4]}), "negative"),
    ],
)
def test_what_cannot_be_drawn_is_refused(make, message):
    with pytest.raises(PageError, match=message):
        make()


# ---- the renderer, read back by pdfium --------------------------------------------------------------------------------


def test_the_pdf_has_the_scan_size_and_the_text_and_is_the_same_bytes_each_time():
    p = one_line(Span("Punch and Judy, "), Span("with ", style="italic"), Span("Instructions", style="bold"))
    pdf = render(p, title="a page")
    assert pdf == render(p, title="a page")  # invariant: no date or random id, so a cached rebuild compares by hash
    img = rasterise(pdf, dpi=300)
    assert img.size == (1200, 600)
    assert img.mode == "L"
    assert read_text(pdf) == "Punch and Judy, with Instructions"
    left, top, right, bottom = ink(img)
    assert 100 <= left <= 106
    assert right <= 1102
    assert top >= 100
    assert bottom <= 182  # inside the line box


def test_fit_fills_the_box_by_spacing_within_its_limit():
    short = Span("Punch and Judy")
    natural = ink(rasterise(render(one_line(short), fit=False)))
    fitted = ink(rasterise(render(one_line(short))))
    assert fitted[2] > natural[2] + 50  # spread out towards the box's right edge (1100)...
    glyphs = len(short.text)
    size_px = 80 * 0.9  # SIZE_OF_BOX of the line box, in pixels at 300 dpi
    assert fitted[2] - natural[2] <= FIT_LIMIT_EM * size_px * glyphs + 2  # ...but never by more than the limit a glyph
    long = Span("x" * 30)  # about 1060 px at this size: a little too long for the 1000 px box
    assert (
        ink(rasterise(render(one_line(long))))[2] < ink(rasterise(render(one_line(long), fit=False)))[2]
    )  # too long: tighter


def test_centre_and_right_alignment_place_a_short_line_in_its_box():
    left = ink(rasterise(render(one_line(Span("Judy"), size_pt=10), fit=False)))
    centre = ink(rasterise(render(one_line(Span("Judy"), size_pt=10, align="centre"), fit=False)))
    right = ink(rasterise(render(one_line(Span("Judy"), size_pt=10, align="right"), fit=False)))
    assert left[0] < 110
    assert abs((centre[0] + centre[2]) / 2 - 600) < 6
    assert 1090 <= right[2] <= 1101


def test_size_baseline_tracking_and_hscale_are_applied():
    base = ink(rasterise(render(one_line(Span("Hd", size_pt=10)), fit=False)))
    big = ink(rasterise(render(one_line(Span("Hd", size_pt=20)), fit=False)))
    assert big[3] - big[1] > 1.8 * (base[3] - base[1])
    low = ink(rasterise(render(one_line(Span("H", size_pt=10), baseline_y=400), fit=False)))
    assert 395 <= low[3] <= 401  # a capital stands on the baseline given
    wide = ink(rasterise(render(one_line(Span("HHHH", size_pt=10, tracking_em=0.5)), fit=False)))
    plain = ink(rasterise(render(one_line(Span("HHHH", size_pt=10)), fit=False)))
    squeezed = ink(rasterise(render(one_line(Span("HHHH", size_pt=10, hscale=0.5)), fit=False)))
    assert wide[2] - wide[0] > (plain[2] - plain[0]) + 3 * 0.5 * 10 * 300 / 72 - 4
    assert squeezed[2] - squeezed[0] < 0.6 * (plain[2] - plain[0])


def test_small_capitals_are_smaller_capitals_and_caps_are_upper_case():
    sc = render(one_line(Span("Abc", style="small-caps", size_pt=20)), fit=False)
    assert read_text(sc) == "ABC"
    img = rasterise(sc)
    a, b = img.crop((100, 0, 150, 600)), img.crop((150, 0, 400, 600))  # the capital A, then the small capital B
    assert ink(b)[3] - ink(b)[1] < 0.85 * (ink(a)[3] - ink(a)[1])
    assert read_text(render(one_line(Span("punch", style="caps")))) == "PUNCH"


def test_a_face_from_a_file_is_embedded_and_a_small_caps_cut_draws_text_as_it_is(tmp_path: Path):
    times = render(one_line(Span("Judy", size_pt=12)), fit=False)
    vera = render(one_line(Span("Judy", size_pt=12, face="vera")), {"vera": VERA}, fit=False)
    assert b"Vera" not in times
    assert len(vera) > len(times)  # the TrueType face is in the file
    assert ink(rasterise(vera)) != ink(rasterise(times))
    assert (
        read_text(
            render(one_line(Span("Judy", style="small-caps", face="vera")), {"vera": VERA}, small_caps_faces={"vera"})
        )
        == "Judy"
    )
    line_face = render(one_line(Span("Judy"), face="vera"), {"vera": VERA})
    assert len(line_face) > len(times)  # a line's face is its spans' default
    assert read_text(render(one_line(Span("Judy", face="unknown")))) == "Judy"  # a face with no file: the standard one
    with pytest.raises(PageError, match="no such file"):
        render(one_line(Span("x", face="gone")), {"gone": str(tmp_path / "gone.ttf")})


def test_figures_are_drawn_in_their_boxes(tmp_path: Path):
    crop = tmp_path / "engraving.png"
    Image.new("L", (40, 20), 0).save(crop)
    p = Page(
        1200, 600, 300, figures=[Figure(box=(200, 300, 400, 200), image=str(crop)), Figure(box=(700, 100, 100, 100))]
    )
    left, top, right, bottom = ink(rasterise(render(p)))
    assert abs(left - 200) <= 1
    assert abs(top - 300) <= 1
    assert abs(right - 600) <= 1
    assert abs(bottom - 500) <= 1
    with pytest.raises(PageError, match="no such picture"):
        render(Page(100, 100, 72, figures=[Figure(box=(0, 0, 1, 1), image=str(tmp_path / "gone.png"))]))


def test_several_pages_and_empty_lines():
    empty = Page(
        600,
        300,
        150,
        blocks=[
            Block(box=(0, 0, 10, 10), lines=[Line(box=(0, 0, 10, 10)), Line(box=(0, 0, 10, 10), spans=[Span("")])])
        ],
    )
    pdf = render([one_line(Span("one")), empty, one_line(Span("three"))])
    assert page_count(pdf) == 3
    assert read_text(pdf) == "one\f\fthree"
    assert read_text(pdf, page=2) == "three"
    assert rasterise(pdf, page=1, dpi=150).size == (600, 300)
    with pytest.raises(PageError, match="no page 3"):
        rasterise(pdf, page=3)


def test_words_placed_by_a_font_matcher_are_set_where_they_were_measured():
    words = [Span("Punch ", x=120, baseline_y=170, size_pt=10), Span("Judy", x=700, baseline_y=170, size_pt=10)]
    pdf = render(one_line(*words))
    img = rasterise(pdf)
    assert ink(img.crop((0, 0, 600, 600)))[0] in range(119, 124)
    assert ink(img.crop((600, 0, 1200, 600)))[0] + 600 in range(
        699, 704
    )  # not right after "Punch ": where it was measured
    assert read_text(pdf).replace(" ", "") == "PunchJudy"
    heavy = ink(rasterise(render(one_line(Span("l", size_pt=20, stroke_em=0.05)), fit=False)))
    thin = ink(rasterise(render(one_line(Span("l", size_pt=20)), fit=False)))
    assert heavy[2] - heavy[0] > thin[2] - thin[0]


def test_a_heavy_word_leaves_the_next_one_as_it_was():
    plain = ink(rasterise(render(one_line(Span("l", x=600, size_pt=20)))))
    after_heavy = rasterise(render(one_line(Span("l", size_pt=20, stroke_em=0.08), Span("l", x=600, size_pt=20))))
    second = ink(after_heavy.crop((500, 0, 1200, 600)))
    assert second[2] - second[0] == plain[2] - plain[0]


def test_tracking_is_the_advance_on_the_page_even_when_scaled():
    # "HH" squeezed to half width with 0.5 em tracking, then "I": the I starts one tracking gap after the second H
    pdf = render(one_line(Span("HH", size_pt=10, hscale=0.5, tracking_em=0.5), Span("I", size_pt=10)), fit=False)
    img = rasterise(pdf)
    hh = ink(rasterise(render(one_line(Span("HH", size_pt=10, hscale=0.5, tracking_em=0.5)), fit=False)))
    i_left = ink(img.crop((hh[2] + 1, 0, 1200, 600)))[0] + hh[2] + 1
    gap_px = 0.5 * 10 * 300 / 72
    assert abs((i_left - hh[2]) - gap_px) <= 4


@pytest.mark.parametrize(("width", "height"), [(2660, 4140), (2660, 4056), (2481, 3509)])
def test_a_page_at_its_own_dpi_comes_back_the_scan_size(width, height):
    """Issue #27: points that are not whole at 500 dpi (383.04 pt) made pdfium round the raster up a pixel."""
    pdf = render(Page(width, height, 500))
    assert rasterise(pdf, dpi=500).size == (width, height)
    if width % 2 == 0 and height % 2 == 0:  # an odd size is half a pixel at half the dpi, which either way is right
        assert rasterise(pdf, dpi=250).size == (width // 2, height // 2)


def test_a_rotated_page_comes_back_turned_and_at_its_rounded_size():
    """A PDF from elsewhere may carry /Rotate; the rounded size follows the turn, as pdfium's own does."""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(render(Page(2660, 4140, 500)))
    doc[0].set_rotation(90)
    out = io.BytesIO()
    doc.save(out)
    doc.close()
    assert rasterise(out.getvalue(), dpi=500).size == (4140, 2660)
