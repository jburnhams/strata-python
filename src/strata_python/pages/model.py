"""The page description: what a scanned page holds, in the scan's own pixels, for drawing it again as a PDF.

A reader (a layout model, OCR, a vision model, a font matcher) fills it in; `strata_python.pages.render` draws it. Every
app that rebuilds pages uses this one shape and one renderer, so an eval that scores a rebuilt page scores exactly what
the app prints.

    Page(width=2480, height=3508, dpi=300, blocks=[
        Block(box=(300, 400, 1880, 120), role="heading", lines=[
            Line(box=(600, 410, 1280, 100), spans=[Span("PUNCH AND JUDY", style="caps", size_pt=24)])])],
        figures=[Figure(box=(400, 700, 1680, 1400), image="/srv/app/crops/p12-f1.png")])

Boxes are `(x, y, w, h)` in scan pixels from the top left, the same as Tesseract's. A line's spans are drawn one after
the other as written (spaces are part of their text), from the line box's left edge, on the line's baseline.
Plain data only (no third-party imports), so an app can build and send descriptions without the `[pdf]` extra."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import MISSING, dataclass, field, fields
from typing import Any, cast

Box = tuple[float, float, float, float]

STYLES = ("roman", "italic", "bold", "bold-italic", "small-caps", "caps", "blackletter")
ROLES = ("body", "heading", "caption", "footnote", "running-head", "folio", "ornament", "rule", "other")
ALIGNS = ("left", "centre", "right", "justify")
FIGURE_KINDS = ("figure", "engraving", "ornament", "initial", "rule")


class PageError(ValueError):
    """A description that cannot be drawn: an unknown style, role or alignment, a box that is not four numbers."""


def _box(v: object, where: str) -> Box:
    if not isinstance(v, Sequence) or isinstance(v, str) or len(cast("Sequence[object]", v)) != 4:
        raise PageError(f"{where}: a box is [x, y, w, h], not {v!r}")
    try:
        x, y, w, h = (float(cast("Any", n)) for n in cast("Sequence[object]", v))
    except (TypeError, ValueError) as e:
        raise PageError(f"{where}: a box is four numbers, not {v!r}") from e
    if w < 0 or h < 0:
        raise PageError(f"{where}: a box has no negative size: {v!r}")
    return (x, y, w, h)


def _one_of(v: object, allowed: tuple[str, ...], where: str) -> str:
    if v not in allowed:
        raise PageError(f"{where}: {v!r} is not one of {', '.join(allowed)}")
    return cast("str", v)


def _opt_float(v: object) -> float | None:
    return None if v is None else float(cast("Any", v))


def _to_dict(obj: Any) -> dict[str, Any]:
    """A dataclass as JSON: fields at their default left out (the JSON stays small and readable), boxes as lists,
    nested parts through their own to_dict."""
    out: dict[str, Any] = {}
    for f in fields(obj):
        v = getattr(obj, f.name)
        if (f.default is not MISSING and v == f.default) or (
            f.default_factory is not MISSING and v == f.default_factory()
        ):
            continue
        if isinstance(v, tuple):
            v = list(cast("tuple[float, ...]", v))
        elif isinstance(v, list):
            v = [x.to_dict() for x in cast("list[Any]", v)]
        out[f.name] = v
    return out


@dataclass
class Span:
    """A run of text in one style. `face` names a font in the renderer's `fonts` mapping (None: the standard face for the
    style); `size_pt` is the size to set (None: the line's, else one worked out from the line box); `tracking_em` is extra
    advance after each glyph in ems of the size; `hscale` is horizontal scale (1.0 = none); `stroke_em` is the width
    of a stroke drawn round the glyphs, in ems of the size, for print heavier than the face: the stroke is centred on
    the outline, so each edge moves out by half of it (0 or less: no stroke).

    Geometry measured per word (a font matcher fits each word): `x` is where the span's ink starts and `baseline_y`
    its baseline, both in scan pixels. A span with an `x` is set there rather than after the span before it, and a
    line with any such span is not fitted to its box, since its words are already placed."""

    text: str
    style: str = "roman"
    face: str | None = None
    size_pt: float | None = None
    tracking_em: float = 0.0
    hscale: float = 1.0
    stroke_em: float = 0.0
    x: float | None = None
    baseline_y: float | None = None

    def __post_init__(self) -> None:
        _one_of(self.style, STYLES, f"span {self.text[:20]!r}")
        if self.hscale <= 0:
            raise PageError(f"span {self.text[:20]!r}: hscale must be above 0, not {self.hscale}")

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Span:
        return cls(
            text=str(d.get("text", "")),
            style=str(d.get("style") or "roman"),
            face=d.get("face"),
            size_pt=_opt_float(d.get("size_pt")),
            tracking_em=float(d.get("tracking_em") or 0.0),
            hscale=float(d.get("hscale") or 1.0),
            stroke_em=float(d.get("stroke_em") or 0.0),
            x=_opt_float(d.get("x")),
            baseline_y=_opt_float(d.get("baseline_y")),
        )

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass
class Line:
    """One printed line. `baseline_y` is the baseline in scan pixels (None: near the bottom of the box, above the
    descenders); `size_pt` and `face` are the defaults for its spans."""

    box: Box
    spans: list[Span] = field(default_factory=lambda: list[Span]())
    baseline_y: float | None = None
    size_pt: float | None = None
    face: str | None = None

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.spans)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Line:
        spans = d.get("spans")
        if spans is None:  # a reader that gives only the text (OCR): one roman span
            spans = [{"text": d.get("text", "")}]
        return cls(
            box=_box(d.get("box"), f"line {str(d.get('text', ''))[:20]!r}"),
            spans=[Span.from_dict(s) for s in cast("list[Mapping[str, Any]]", spans)],
            baseline_y=_opt_float(d.get("baseline_y")),
            size_pt=_opt_float(d.get("size_pt")),
            face=d.get("face"),
        )

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass
class Block:
    """A block of lines with one role. `order` is its place in reading order (None: after the numbered ones, in list
    order); `align` places a line inside its block when it is narrower than its own box allows."""

    box: Box
    lines: list[Line] = field(default_factory=lambda: list[Line]())
    role: str = "body"
    order: int | None = None
    align: str = "left"

    def __post_init__(self) -> None:
        _one_of(self.role, ROLES, "block role")
        _one_of(self.align, ALIGNS, "block align")

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Block:
        return cls(
            box=_box(d.get("box"), "block"),
            lines=[Line.from_dict(x) for x in cast("list[Mapping[str, Any]]", d.get("lines") or [])],
            role=str(d.get("role") or "body"),
            order=None if d.get("order") is None else int(d["order"]),
            align=str(d.get("align") or "left"),
        )

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass
class Figure:
    """A picture on the page. `image` is the path of its cleaned crop, drawn stretched to the box (None: the box is left
    empty, so a layout can be drawn before the crops exist)."""

    box: Box
    image: str | None = None
    kind: str = "figure"

    def __post_init__(self) -> None:
        _one_of(self.kind, FIGURE_KINDS, "figure kind")

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Figure:
        return cls(box=_box(d.get("box"), "figure"), image=d.get("image"), kind=str(d.get("kind") or "figure"))

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass
class Page:
    """A page: its size in scan pixels at `dpi`, which sets the PDF's size in points (72 a inch)."""

    width: float
    height: float
    dpi: float
    blocks: list[Block] = field(default_factory=lambda: list[Block]())
    figures: list[Figure] = field(default_factory=lambda: list[Figure]())

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0 or self.dpi <= 0:
            raise PageError(
                f"a page needs a positive width, height and dpi, not {self.width}x{self.height} at {self.dpi}"
            )

    def reading_order(self) -> list[Block]:
        """The blocks in reading order: numbered ones first by number, then the rest as listed."""
        numbered = sorted((b for b in self.blocks if b.order is not None), key=lambda b: cast("int", b.order))
        return numbered + [b for b in self.blocks if b.order is None]

    @property
    def text(self) -> str:
        """The page's text in reading order, a line a line, blocks apart by a blank line."""
        return "\n\n".join("\n".join(x.text for x in b.lines) for b in self.reading_order() if b.lines)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Page:
        try:
            width, height, dpi = float(d["width"]), float(d["height"]), float(d["dpi"])
        except (KeyError, TypeError, ValueError) as e:
            raise PageError(f"a page needs width, height and dpi: {e}") from e
        return cls(
            width=width,
            height=height,
            dpi=dpi,
            blocks=[Block.from_dict(b) for b in cast("list[Mapping[str, Any]]", d.get("blocks") or [])],
            figures=[Figure.from_dict(f) for f in cast("list[Mapping[str, Any]]", d.get("figures") or [])],
        )

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)
