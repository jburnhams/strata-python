"""Exact time for media: rational seconds, frame rates, half-open spans and the map from an edit's output time back to its sources.

An editor that keeps time as float seconds drifts: 1001/30000 s is not a float, a frame found by `int(t * fps)` lands one early when `t` came
back from a sum, and a cut moved twice is no longer on a frame. strata-reels (the video editor) and strata-360 both needed the same answer,
which is the one OpenTimelineIO and every NLE use: time is a rational number of seconds, a rate is a rational number of frames a second,
and a stretch of time is half-open, [start, end), so two shots that meet share no frame. Here:

    t = rational("1001/30000")                 # Fraction; also from an int, a Decimal, a str ("12.04") or a float (by its shortest repr)
    pal = Rate.parse("25/1"); ntsc = Rate.parse("30000/1001")   # ffprobe's r_frame_rate / avg_frame_rate text
    ntsc.frame_at(t); ntsc.time_of(120); ntsc.snap(t)            # the frame showing at t; when frame 120 starts; the nearest frame start
    s = Span(rational(2), rational(5)); s.duration; s & Span(...)  # [2, 5): 3 s; the overlap, or None
    m = TimeMap.sequence([("tape1", Span(...)), ("tape1", Span(...))])   # source ranges played end to end
    m.to_source(rational(4)) -> ("tape1", Fraction(...)); m.to_output("tape1", t) -> [output times]
    text(t) -> "1001/30000"; m.to_json(); TimeMap.from_json(...)          # exact in JSON: rationals as strings

A float never goes in silently: `rational(0.1)` is 1/10 (the float's shortest repr), not 3602879701896397/36028797018963968, and a value that
is not a number raises ValueError. Standard library only."""

from __future__ import annotations

import bisect
import itertools
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from typing import Any

Number = int | float | str | Decimal | Fraction


def rational(value: Number) -> Fraction:
    """Exact seconds (or any quantity) from an int, Fraction, Decimal, "p/q" or decimal text, or a float by its shortest repr."""
    if isinstance(value, bool):
        raise ValueError(f"not a number: {value!r}")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"not a finite number: {value!r}")
        return Fraction(
            repr(value)
        )  # 0.1 -> 1/10: the number the person or file wrote, not the float's binary expansion
    try:
        return Fraction(value)
    except (ValueError, ZeroDivisionError, TypeError) as e:
        raise ValueError(f"not a number: {value!r}") from e


def text(value: Fraction) -> str:
    """A rational as JSON-safe text: "3" or "1001/30000"; `rational` reads it back exactly."""
    return str(value)


@dataclass(frozen=True, order=True)
class Rate:
    """Frames a second, exact: 25, 50, 30000/1001. Frame n starts at n / rate and shows until frame n + 1 starts."""

    fps: Fraction

    def __post_init__(self) -> None:
        if self.fps <= 0:
            raise ValueError(f"a frame rate must be above 0: {self.fps}")

    @classmethod
    def parse(cls, value: Number) -> Rate:
        """From ffprobe's "30000/1001" (or "25", 25, 29.97 as written)."""
        return cls(rational(value))

    @property
    def frame_s(self) -> Fraction:
        """How long one frame lasts."""
        return 1 / self.fps

    def frame_at(self, t: Fraction) -> int:
        """The frame showing at `t` (a time on a frame's start is that frame): floor(t * fps)."""
        return math.floor(t * self.fps)

    def time_of(self, frame: int) -> Fraction:
        """When `frame` starts."""
        return frame / self.fps

    def snap(self, t: Fraction) -> Fraction:
        """The frame start nearest `t`; exactly halfway goes to the earlier frame, so snapping never moves a cut past the frame it was in."""
        n = t * self.fps
        lower = math.floor(n)
        return self.time_of(lower if n - lower <= Fraction(1, 2) else lower + 1)

    def __str__(self) -> str:
        return text(self.fps)


@dataclass(frozen=True, order=True)
class Span:
    """A stretch of time, half-open: [start, end). Two spans that meet (one's end is the other's start) do not overlap."""

    start: Fraction
    end: Fraction

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"a span cannot end before it starts: [{self.start}, {self.end})")

    @classmethod
    def of(cls, start: Number, end: Number) -> Span:
        return cls(rational(start), rational(end))

    @property
    def duration(self) -> Fraction:
        return self.end - self.start

    def __contains__(self, t: object) -> bool:
        return isinstance(t, (int, Fraction)) and self.start <= t < self.end

    def overlaps(self, other: Span) -> bool:
        return self.start < other.end and other.start < self.end

    def __and__(self, other: Span) -> Span | None:
        """The overlap of two spans, or None when they share no time."""
        start, end = max(self.start, other.start), min(self.end, other.end)
        return Span(start, end) if start < end else None

    def shift(self, by: Fraction) -> Span:
        return Span(self.start + by, self.end + by)

    def frames(self, rate: Rate) -> range:
        """The frames whose start lies in the span: the frames a cut [start, end) keeps."""
        first = math.ceil(self.start * rate.fps)
        last = math.ceil(self.end * rate.fps)
        return range(first, last)

    def to_json(self) -> list[str]:
        return [text(self.start), text(self.end)]

    @classmethod
    def from_json(cls, value: Sequence[Number]) -> Span:
        if len(value) != 2:
            raise ValueError(f"a span is [start, end]: {value!r}")
        return cls.of(value[0], value[1])


@dataclass(frozen=True)
class Piece:
    """One stretch of a source placed in the output: `source` from `src.start` to `src.end` plays from output time `at`, at `speed` (2 = twice as
    fast, so it lasts half as long in the output). A freeze frame is not a speed of 0: give it its own piece of one frame and a slow speed."""

    source: str
    src: Span
    at: Fraction
    speed: Fraction = Fraction(1)

    def __post_init__(self) -> None:
        if self.speed <= 0:
            raise ValueError(f"speed must be above 0: {self.speed}")
        if self.src.duration == 0:
            raise ValueError(f"a piece of {self.source} plays nothing: {self.src}")

    @property
    def out(self) -> Span:
        """Where the piece sits in the output."""
        return Span(self.at, self.at + self.src.duration / self.speed)

    def to_source(self, t: Fraction) -> Fraction:
        return self.src.start + (t - self.at) * self.speed

    def to_output(self, s: Fraction) -> Fraction:
        return self.at + (s - self.src.start) / self.speed

    def to_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {"source": self.source, "src": self.src.to_json(), "at": text(self.at)}
        if self.speed != 1:
            d["speed"] = text(self.speed)
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Piece:
        return cls(str(d["source"]), Span.from_json(d["src"]), rational(d["at"]), rational(d.get("speed", 1)))


@dataclass(frozen=True)
class TimeMap:
    """Output time to source time for one track of an edit: pieces in output order, never overlapping in the output (gaps are allowed: nothing
    plays there). `to_source` answers what is showing; `to_output` answers every place a source moment is used (it may be used twice)."""

    pieces: tuple[Piece, ...] = ()
    _starts: tuple[Fraction, ...] = field(default=(), init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        ordered = tuple(sorted(self.pieces, key=lambda p: p.at))
        for a, b in itertools.pairwise(ordered):
            if a.out.overlaps(b.out):
                raise ValueError(f"pieces overlap in the output: {a.out} and {b.out}")
        object.__setattr__(self, "pieces", ordered)
        object.__setattr__(self, "_starts", tuple(p.at for p in ordered))

    @classmethod
    def sequence(cls, ranges: Iterable[tuple[str, Span]], start: Fraction = Fraction(0)) -> TimeMap:
        """Source ranges played one after the other with no gap, from output time `start`: a cut list."""
        pieces: list[Piece] = []
        at = start
        for source, src in ranges:
            pieces.append(Piece(source, src, at))
            at += src.duration
        return cls(tuple(pieces))

    @property
    def duration(self) -> Fraction:
        """The end of the last piece (gaps included)."""
        return self.pieces[-1].out.end if self.pieces else Fraction(0)

    def piece_at(self, t: Fraction) -> Piece | None:
        i = bisect.bisect_right(self._starts, t) - 1
        if i < 0:
            return None
        p = self.pieces[i]
        return p if t in p.out else None

    def to_source(self, t: Fraction) -> tuple[str, Fraction] | None:
        """(source, source time) showing at output time `t`, or None in a gap or past the end."""
        p = self.piece_at(t)
        return None if p is None else (p.source, p.to_source(t))

    def to_output(self, source: str, s: Fraction) -> list[Fraction]:
        """Every output time at which `source` time `s` plays, earliest first (none when the edit leaves it out)."""
        return [p.to_output(s) for p in self.pieces if p.source == source and s in p.src]

    def to_json(self) -> list[dict[str, Any]]:
        return [p.to_json() for p in self.pieces]

    @classmethod
    def from_json(cls, value: Iterable[dict[str, Any]]) -> TimeMap:
        return cls(tuple(Piece.from_json(d) for d in value))
