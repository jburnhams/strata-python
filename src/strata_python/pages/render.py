"""Draw page descriptions as a PDF with real text, and read a PDF back as pixels and text (the `[pdf]` extra).

    pdf = render(page, fonts={"old-standard": "/srv/fonts/OldStandardTT-Regular.ttf"})
    img = rasterise(pdf, dpi=page.dpi)         # the same pixels as the scan, to compare ink
    text = read_text(pdf)                       # what a reader of the PDF gets, in reading order

What the renderer does with a description, so a reader knows what to give it:
- Each line is set on its baseline, from the left of its box (centred or right-aligned in the box for a block aligned
  so), span after span. Blocks are drawn in reading order, so the PDF's text reads back in that order.
- A span's size is its `size_pt`, else its line's, else `SIZE_OF_BOX` times the line box's height.
- `tracking_em`, `hscale` and `stroke_em` (a stroke round the glyphs) are applied as given, and a span's own `x`
  and `baseline_y` (a font matcher's per-word fit) place it; a line with placed spans is not fitted again. With `fit` (the default) the glyph spacing of the line is then
  nudged, by at most `FIT_LIMIT_EM` a glyph, so the line fills its box: a near face then lands where the print was
  without the glyphs themselves being stretched.
- `small-caps` is drawn as capitals with the lower-case letters at `SMALL_CAP` of the size, unless the span's face is a
  small-caps cut (named in `small_caps_faces`), which draws the text as it is; `caps` upper-cases the text.
- A span with no face, or a face missing from `fonts`, gets the standard PDF face for its style (Times), so a layout
  draws before any font is matched.
- Figures are drawn first, each crop stretched to its box.
ReportLab and pdfium have no type stubs, so they are reached through `Any` here; the types of this module's own
functions are complete."""

from __future__ import annotations

import hashlib
import importlib
import io
import os
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from strata_python.pages.model import Block, Line, Page, PageError, Span

if TYPE_CHECKING:
    from PIL import Image

SIZE_OF_BOX = (
    0.9  # a Tesseract line box runs from the tallest capital or ascender to the lowest descender: about 0.9-1.1 em
)
DESCENT_EM = (
    0.22  # with no baseline given, the baseline sits this far above the box's bottom (a book face's descenders)
)
SMALL_CAP = 0.78  # synthesised small capitals: the lower-case letters' size as a share of the capitals'
FIT_LIMIT_EM = 0.1  # the most a line's glyph spacing is changed to fit its box, in ems a glyph
STANDARD = {
    "roman": "Times-Roman",
    "italic": "Times-Italic",
    "bold": "Times-Bold",
    "bold-italic": "Times-BoldItalic",
    "small-caps": "Times-Roman",
    "caps": "Times-Roman",
    "blackletter": "Times-Roman",
}


def _rl(module: str) -> Any:
    try:
        return importlib.import_module(module)
    except ImportError as e:  # pragma: no cover  (the extra is installed wherever this module's tests run)
        raise ImportError("strata_python.pages.render needs the pdf extra: strata-python[pdf]") from e


class _Fonts:
    """The fonts a render may use: each face id's TTF registered with ReportLab once a process, under a name made from
    its path (ReportLab's registry is global, so two apps' faces with the same id must not collide)."""

    def __init__(self, fonts: Mapping[str, str], small_caps: Collection[str]) -> None:
        self.paths = dict(fonts)
        self.small_caps = set(small_caps)
        self.pdfmetrics: Any = _rl("reportlab.pdfbase.pdfmetrics")
        self.ttfonts: Any = _rl("reportlab.pdfbase.ttfonts")

    def name(self, span: Span, line: Line) -> str:
        face = span.face or line.face
        path = self.paths.get(face) if face else None
        if not path:
            return STANDARD[span.style]
        if not os.path.isfile(path):
            raise PageError(f"font {face!r}: no such file {path!r}")
        name = "sp-" + hashlib.sha1(os.path.abspath(path).encode()).hexdigest()[:12]  # noqa: S324  (a name, not security)
        if name not in self.pdfmetrics.getRegisteredFontNames():
            self.pdfmetrics.registerFont(self.ttfonts.TTFont(name, path))
        return name

    def is_small_caps_cut(self, span: Span, line: Line) -> bool:
        face = span.face or line.face
        return face is not None and face in self.small_caps and face in self.paths

    def width(self, text: str, font: str, size: float) -> float:
        return float(self.pdfmetrics.stringWidth(text, font, size))


@dataclass
class _Run:
    """Text drawn with one font, size, scale, tracking and weight; `x` and `baseline` (scan pixels) when its span was
    placed by a reader, else it follows the run before it."""

    text: str
    font: str
    size: float
    hscale: float
    track: float  # points a glyph
    weight: float  # stroke width in points (0: fill only)
    x: float | None = None
    baseline: float | None = None


def _runs(line: Line, fonts: _Fonts, default_size: float) -> list[_Run]:
    out: list[_Run] = []
    for span in line.spans:
        size = span.size_pt or line.size_pt or default_size
        font = fonts.name(span, line)
        track, weight = span.tracking_em * size, max(span.stroke_em, 0.0) * size
        text = span.text.upper() if span.style == "caps" else span.text
        if span.style != "small-caps" or fonts.is_small_caps_cut(span, line):
            out.append(_Run(text, font, size, span.hscale, track, weight, span.x, span.baseline_y))
            continue
        # capitals as they are, lower case as smaller capitals; split where the case changes so each piece is one run
        # (only the first piece is placed where the span was measured; the rest follow it)
        pieces: list[tuple[str, bool]] = []
        for ch in text:
            if pieces and pieces[-1][1] == ch.islower():
                pieces[-1] = (pieces[-1][0] + ch, pieces[-1][1])
            else:
                pieces.append((ch, ch.islower()))
        for i, (piece, small) in enumerate(pieces):
            first = i == 0
            out.append(
                _Run(
                    piece.upper(),
                    font,
                    size * SMALL_CAP if small else size,
                    span.hscale,
                    track,
                    weight,
                    span.x if first else None,
                    span.baseline_y,
                )
            )
    return out


def _run_width(fonts: _Fonts, run: _Run, extra: float = 0.0) -> float:
    return fonts.width(run.text, run.font, run.size) * run.hscale + (run.track + extra) * len(run.text)


def _draw_line(c: Any, line: Line, block: Block, fonts: _Fonts, k: float, page_h_pt: float, fit: bool) -> None:
    x_px, y_px, w_px, h_px = line.box
    runs = [r for r in _runs(line, fonts, max(h_px * k * SIZE_OF_BOX, 1.0)) if r.text]
    glyphs = sum(len(r.text) for r in runs)
    if not glyphs:
        return
    placed = any(r.x is not None for r in runs)
    natural = sum(_run_width(fonts, r) for r in runs)
    box_w = w_px * k
    extra = 0.0
    if fit and not placed and box_w > 0 and glyphs > 1:
        mean_size = sum(r.size * len(r.text) for r in runs) / glyphs
        limit = FIT_LIMIT_EM * mean_size
        extra = max(-limit, min(limit, (box_w - natural) / glyphs))
    width = natural + extra * glyphs
    x = x_px * k
    if not placed and block.align == "centre":
        x += (box_w - width) / 2
    elif not placed and block.align == "right":
        x += box_w - width
    line_base = line.baseline_y if line.baseline_y is not None else y_px + h_px - DESCENT_EM * runs[0].size / k
    for r in runs:  # one text object a run: ReportLab's cursor does not count tracking or scale, so x is ours
        if r.x is not None:
            x = r.x * k
        t = c.beginText(x, page_h_pt - (r.baseline if r.baseline is not None else line_base) * k)
        t.setFont(r.font, r.size)
        t.setHorizScale(r.hscale * 100)
        t.setCharSpace(r.track + extra)
        if r.weight > 0:  # fill and stroke the glyphs: heavier print than the face's own weight
            c.setLineWidth(r.weight)
            t.setTextRenderMode(2)
        t.textOut(r.text)
        c.drawText(t)
        x += _run_width(fonts, r, extra)


def _draw_page(c: Any, page: Page, fonts: _Fonts, fit: bool) -> None:
    k = 72.0 / page.dpi  # scan pixels to points
    w_pt, h_pt = page.width * k, page.height * k
    c.setPageSize((w_pt, h_pt))
    for f in page.figures:
        if f.image is None:
            continue
        if not os.path.isfile(f.image):
            raise PageError(f"figure at {list(f.box)}: no such picture {f.image!r}")
        x, y, w, h = f.box
        c.drawImage(f.image, x * k, h_pt - (y + h) * k, w * k, h * k)
    for block in page.reading_order():
        for line in block.lines:
            _draw_line(c, line, block, fonts, k, h_pt, fit)
    c.showPage()


def render(
    pages: Page | Sequence[Page],
    fonts: Mapping[str, str] | None = None,
    *,
    small_caps_faces: Collection[str] = (),
    fit: bool = True,
    title: str | None = None,
) -> bytes:
    """The PDF of one page or several (one PDF page each, sized from the scan's pixels and dpi). `fonts` maps face ids
    to TTF or OTF files with TrueType outlines; the faces used are embedded (subset). The output is the same bytes for
    the same input (no creation date or random id), so a cached rebuild compares by hash."""
    canvas: Any = _rl("reportlab.pdfgen.canvas")
    buf = io.BytesIO()
    c = canvas.Canvas(buf, invariant=1, pageCompression=1)
    if title:
        c.setTitle(title)
    f = _Fonts(fonts or {}, small_caps_faces)
    for page in [pages] if isinstance(pages, Page) else pages:
        _draw_page(c, page, f, fit)
    c.save()
    return buf.getvalue()


def _document(pdf: bytes) -> Any:
    return _rl("pypdfium2").PdfDocument(pdf)


def page_count(pdf: bytes) -> int:
    doc = _document(pdf)
    try:
        return len(doc)
    finally:
        doc.close()


def rasterise(pdf: bytes, page: int = 0, dpi: float = 300.0) -> Image.Image:
    """One page of a PDF as a greyscale picture at `dpi` (the scan's dpi gives the scan's size in pixels), white paper."""
    doc = _document(pdf)
    try:
        if not 0 <= page < len(doc):
            raise PageError(f"no page {page} in a PDF of {len(doc)}")
        p = doc[page]
        img = p.render(scale=dpi / 72.0).to_pil().convert("L")
        p.close()
        return img
    finally:
        doc.close()


def read_text(pdf: bytes, page: int | None = None) -> str:
    """The text a reader gets from the PDF (one page, or every page with a form feed between), in drawing order."""
    doc = _document(pdf)
    try:
        out: list[str] = []
        for i in [page] if page is not None else range(len(doc)):
            p = doc[i]
            tp = p.get_textpage()
            out.append(str(tp.get_text_range()).replace("\r\n", "\n").replace("\r", "\n"))
            tp.close()
            p.close()
        return "\f".join(out)
    finally:
        doc.close()
