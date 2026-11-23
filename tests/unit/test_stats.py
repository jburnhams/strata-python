"""stats: known answers for the intervals and tests, and Bradley-Terry recovering strengths it was made from."""

import datetime
import math
import random

import pytest

from strata_python import stats
from strata_python.contracts import Judgment


def simulate(strengths, n, side=1.0, seed=1):
    rng = random.Random(seed)  # noqa: S311 - a seeded simulation
    names = list(strengths)
    out = []
    for _ in range(n):
        a, b = rng.sample(names, 2)
        pa = side * strengths[a] / (side * strengths[a] + strengths[b])
        out.append((a, b, "a" if rng.random() < pa else "b"))
    return out


def test_bradley_terry_recovers_strengths():
    truth = {"x": 4.0, "y": 1.0, "z": 0.25}  # geometric mean 1
    fit = stats.bradley_terry(simulate(truth, 6000))
    for name, p in truth.items():
        assert fit.strengths[name] == pytest.approx(p, rel=0.15)
    assert math.prod(fit.strengths.values()) == pytest.approx(1.0)
    assert fit.side == 1.0
    assert fit.p_beats("x", "y") == pytest.approx(0.8, abs=0.04)
    r = fit.ratings()
    assert sum(r.values()) / 3 == pytest.approx(1500)
    assert r["x"] - r["y"] == pytest.approx(400 * math.log10(4), abs=40)


def test_side_advantage_is_fitted_and_taken_out():
    truth = {"x": 2.0, "y": 1.0, "z": 0.5}
    votes = simulate(truth, 8000, side=1.5, seed=2)
    plain = stats.bradley_terry(votes)
    fitted = stats.bradley_terry(votes, side=True)
    assert fitted.side == pytest.approx(1.5, rel=0.1)
    assert plain.side == 1.0
    for name, p in truth.items():
        assert fitted.strengths[name] == pytest.approx(p / math.prod(truth.values()) ** (1 / 3), rel=0.15)


def test_ties_exclusions_and_empty():
    votes = [("a", "b", "tie")] * 10 + [("a", "a", "a"), ("a", "c", "bogus")]
    fit = stats.bradley_terry(votes)
    assert fit.strengths == pytest.approx({"a": 1.0, "b": 1.0})
    assert stats.bradley_terry([]).strengths == {}
    assert stats.bradley_terry(votes, exclude={"b"}).strengths == {}
    one_sided = stats.bradley_terry([("w", "l", "a")] * 5)
    assert one_sided.strengths["w"] > 100 * one_sided.strengths["l"]  # large, finite
    assert all(math.isfinite(v) for v in one_sided.strengths.values())


def test_intervals_cover_the_estimate_and_repeat():
    votes = simulate({"x": 3.0, "y": 1.0, "z": 1 / 3}, 300, seed=3)
    ci = stats.intervals(votes, n=100)
    est = stats.bradley_terry(votes).ratings()
    for name, (lo, hi) in ci.items():
        assert lo <= est[name] <= hi
    assert stats.intervals(votes, n=100) == ci  # seeded
    assert stats.intervals([]) == {}


def test_head_to_head():
    votes = [("x", "y", "a"), ("y", "x", "a"), ("y", "x", "b"), ("x", "y", "tie"), ("x", "x", "a"), ("x", "y", "?")]
    assert stats.head_to_head(votes) == {("x", "y"): [2, 1, 1]}


def test_votes_from_judgments_leaves_out_undone():
    now = datetime.datetime(2026, 11, 23, tzinfo=datetime.UTC)
    rows = [
        Judgment(a="p", b="q", winner="a", rater="r", at=now),
        Judgment(a="p", b="q", winner="b", rater="r", at=now, undone_at=now),
    ]
    assert stats.votes_from(rows) == [("p", "q", "a")]


def test_wilson_known_values():
    share, lo, hi = stats.wilson(8, 10)
    assert share == 0.8
    assert (lo, hi) == pytest.approx((0.4902, 0.9433), abs=1e-4)
    assert stats.wilson(0, 10)[1] == 0.0
    assert stats.wilson(10, 10)[2] == pytest.approx(1.0)
    assert all(math.isnan(x) for x in stats.wilson(0, 0))


def test_mcnemar():
    assert stats.mcnemar([True, True, False], [True, False, False]) == (1, 0, 1.0)
    only_a, only_b, p = stats.mcnemar([True] * 10 + [False], [False] * 10 + [True])
    assert (only_a, only_b) == (10, 1)
    assert p == pytest.approx(2 * 12 / 2**11)
    assert stats.mcnemar([], []) == (0, 0, 1.0)
    with pytest.raises(ValueError, match=r"shorter|longer"):
        stats.mcnemar([True], [True, False])


def test_holm():
    assert stats.holm([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])
    out = stats.holm([0.5, math.nan, 0.01])
    assert out[0] == pytest.approx(0.5)
    assert math.isnan(out[1])
    assert out[2] == pytest.approx(0.02)
    assert stats.holm([0.9, 0.8]) == [1.0, 1.0]  # 2 x 0.8 capped at 1, and never below an earlier one


def test_side_converges_when_the_strengths_start_equal():
    """Each ordering of two names: nine wins for the side shown first, one for the other. Equal strengths, odds 9:1."""
    votes = [("x", "y", "a")] * 9 + [("x", "y", "b")] + [("y", "x", "a")] * 9 + [("y", "x", "b")]
    fit = stats.bradley_terry(votes, side=True, prior=0)
    assert fit.strengths == pytest.approx({"x": 1.0, "y": 1.0})
    assert fit.side == pytest.approx(9.0, rel=1e-3)
