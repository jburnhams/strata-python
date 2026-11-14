"""Scanned pages rebuilt as PDFs: the page description (`model`, plain data) and its renderer (`render`, the `[pdf]`
extra: ReportLab to write, pdfium to read back). See `model` for the shape and `render` for what is drawn how.

    from strata_python.pages import Block, Figure, Line, Page, Span
    from strata_python.pages.render import rasterise, read_text, render"""

from strata_python.pages.model import (
    ALIGNS,
    FIGURE_KINDS,
    ROLES,
    STYLES,
    Block,
    Box,
    Figure,
    Line,
    Page,
    PageError,
    Span,
)

__all__ = ["ALIGNS", "FIGURE_KINDS", "ROLES", "STYLES", "Block", "Box", "Figure", "Line", "Page", "PageError", "Span"]
