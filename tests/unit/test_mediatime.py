"""strata_python.mediatime: rational time, frame rates, half-open spans and the output-to-source map, with round trips over many random
edits (a fixed seed, so a failure repeats)."""

import json
import random
from decimal import Decimal
from fractions import Fraction as F

import pytest

from strata_python.mediatime import Piece, Rate, Span, TimeMap, rational, text


def test_rational_is_exact_and_refuses_what_is_not_a_number():
    assert rational("1001/30000") == F(1001, 30000)
    assert rational("12.04") == F(301, 25)
    assert rational(Decimal("0.1")) == F(1, 10)
    assert rational(0.1) == F(1, 10)  # the float's shortest repr, not its binary expansion
    assert rational(3) == 3
    assert rational(F(1, 3)) == F(1, 3)
    for bad in ("soon", "1/0", float("nan"), float("inf"), True, None):
        with pytest.raises(ValueError, match="not a"):
            rational(bad)  # pyright: ignore[reportArgumentType]
    assert text(F(1001, 30000)) == "1001/30000"
    assert text(F(3)) == "3"
    assert rational(text(F(7, 3))) == F(7, 3)


def test_frames_of_pal_and_ntsc():
    pal, ntsc = Rate.parse("25/1"), Rate.parse("30000/1001")
    assert pal.frame_s == F(1, 25)
    assert str(ntsc) == "30000/1001"
    assert Rate.parse(29.97).fps == F(2997, 100)
    assert pal.frame_at(F(2)) == 50  # a time on a frame's start is that frame
    assert pal.frame_at(F(2) - F(1, 1000)) == 49
    assert ntsc.time_of(30000) == 1001
    assert ntsc.frame_at(ntsc.time_of(123456)) == 123456
    assert pal.snap(F(1, 50)) == 0  # halfway goes to the earlier frame
    assert pal.snap(F(1, 50) + F(1, 10**6)) == F(1, 25)
    with pytest.raises(ValueError, match="above 0"):
        Rate.parse(0)
    # the float way drifts: adding 0.04 a thousand times is not 40.0, and int() of it lands a frame early; the rational way does not
    t, exact = 0.0, F(0)
    for _ in range(1000):
        t += 0.04
        exact += pal.frame_s
    assert int(t * 25) == 999
    assert pal.frame_at(exact) == 1000


def test_spans_are_half_open():
    a, b = Span.of(2, 5), Span.of(5, 7)
    assert a.duration == 3
    assert 2 in a
    assert 5 not in a
    assert F(9, 2) in a
    assert "3" not in a
    assert not a.overlaps(b)
    assert a.overlaps(Span.of(4, 9))
    assert not a.overlaps(Span.of(3, 3))  # an empty span holds no time, even inside another
    assert (a & b) is None
    assert (a & Span.of(4, 9)) == Span.of(4, 5)
    assert a.shift(F(1)) == Span.of(3, 6)
    assert a.to_json() == ["2", "5"]
    assert Span.from_json(a.to_json()) == a
    # frame 0 started before the span: not kept
    assert list(Span.of("1/50", 1).frames(Rate.parse(25))) == list(range(1, 25))
    with pytest.raises(ValueError, match="end before it starts"):
        Span.of(5, 2)
    with pytest.raises(ValueError, match=r"\[start, end\]"):
        Span.from_json(["1"])


def test_a_cut_list_maps_both_ways():
    m = TimeMap.sequence([("tape1", Span.of(10, 12)), ("tape2", Span.of(0, 1)), ("tape1", Span.of(11, 13))])
    assert m.duration == 5
    assert [p.at for p in m.pieces] == [0, 2, 3]
    assert m.to_source(F(0)) == ("tape1", 10)
    assert m.to_source(F(2)) == ("tape2", 0)
    assert m.to_source(F(5)) is None
    assert m.to_output("tape1", F(23, 2)) == [F(3, 2), F(7, 2)]  # 11.5 s of tape1 is used twice
    assert m.to_output("tape2", F(5)) == []
    assert TimeMap().duration == 0
    assert TimeMap().to_source(F(0)) is None
    assert TimeMap.from_json(json.loads(json.dumps(m.to_json()))) == m


def test_speed_gaps_and_refusals():
    fast = Piece("t", Span.of(0, 4), F(10), F(2))
    m = TimeMap((fast, Piece("t", Span.of(20, 21), F(0))))
    assert fast.out == Span.of(10, 12)
    assert m.pieces[0].at == 0  # sorted by output time
    assert m.to_source(F(11)) == ("t", 2)
    assert m.to_source(F(5)) is None  # a gap
    assert m.to_output("t", F(3)) == [F(23, 2)]
    assert fast.to_json()["speed"] == "2"
    assert "speed" not in m.pieces[0].to_json()
    assert TimeMap.from_json(m.to_json()) == m
    with pytest.raises(ValueError, match="overlap"):
        TimeMap((Piece("a", Span.of(0, 2), F(0)), Piece("b", Span.of(0, 2), F(1))))
    with pytest.raises(ValueError, match="speed"):
        Piece("a", Span.of(0, 1), F(0), F(0))
    with pytest.raises(ValueError, match="plays nothing"):
        Piece("a", Span.of(1, 1), F(0))


def test_round_trips_on_random_edits_are_exact():
    rng = random.Random(20261123)  # noqa: S311  (a repeatable sample of edits, not a secret)
    rates = [Rate.parse(r) for r in ("25", "50", "30000/1001", "24000/1001", "60")]
    for _ in range(300):
        rate = rng.choice(rates)
        ranges: list[tuple[str, Span]] = []
        for _ in range(rng.randint(1, 8)):
            a = rate.time_of(rng.randint(0, 100_000))
            ranges.append((rng.choice("xyz"), Span(a, a + rate.time_of(rng.randint(1, 500)))))
        m = TimeMap.sequence(ranges, start=rate.time_of(rng.randint(0, 50)))
        for _ in range(20):
            t = rate.time_of(rng.randint(rate.frame_at(m.pieces[0].at), rate.frame_at(m.duration) - 1))
            src = m.to_source(t)
            assert src is not None
            # output -> source -> output lands on the same output time, exactly; a frame start maps to a frame start
            assert t in m.to_output(*src)
            assert src[1] == rate.time_of(rate.frame_at(src[1]))
        assert m.duration == m.pieces[0].at + sum((s.duration for _, s in ranges), F(0))
