"""The statistics the apps' rankings and evals share, so "95 % interval" and "strength" mean the same in every app.

strata-stills' arena and strata-models' evals each fit Bradley-Terry strengths to pairwise choices (both the MM
algorithm, 200 iterations; stills also fits the advantage of the picture shown on the left), and models has the
interval and test functions around it. This is one copy, in pure Python (no numpy), on the `contracts.Judgment` shape:
a vote is `(a, b, winner)` with `winner` in "a", "b", "tie".

    fit = bradley_terry(votes, side=True)     # Fit: .strengths {name: p} (geometric mean 1), .side (left-side odds)
    fit.ratings()                             # on the Elo scale, the mean name 1500
    intervals(votes, n=200)                   # {name: (low, high)} of the rating, a bootstrap over votes
    head_to_head(votes)                       # {(a, b): [a wins, b wins, ties]}, names sorted
    wilson(k, n)                              # (share, low, high)
    mcnemar(a_right, b_right)                 # exact paired test: (only_a, only_b, p)
    holm(pvalues)                             # Holm-adjusted, in the order given
    votes_from(judgments)                     # Judgment rows (undone ones left out) as votes

A tie counts half a win each way. With `side=True`, P(a wins) = h·p_a / (h·p_a + p_b), `a` being the one shown first
(left): when the sides are drawn at random, h only takes the person's lean towards one side out of the strengths. A
name with no usable vote is not in the result. Fits and bootstraps are deterministic (a seed), so a report repeats."""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

Winner = Literal["a", "b", "tie"]
Vote = tuple[str, str, str]
Z95 = 1.959963984540054
START = 1500.0
SCORE = {"a": 1.0, "b": 0.0, "tie": 0.5}


class _Judgment(Protocol):
    a: str
    b: str
    winner: str
    undone_at: object


def votes_from(judgments: Iterable[_Judgment]) -> list[Vote]:
    """`contracts.Judgment` rows (or anything with `a`, `b`, `winner`, `undone_at`) as votes, undone ones left out."""
    return [(j.a, j.b, j.winner) for j in judgments if j.undone_at is None]


@dataclass
class Fit:
    strengths: dict[str, float]  # geometric mean 1
    side: float = 1.0  # odds multiplier of the first-shown side; 1.0 = no lean

    def ratings(self, start: float = START) -> dict[str, float]:
        """Strengths on the Elo scale: 400·log10(p) + start, so 400 points is 10:1 odds and the mean name is `start`."""
        return {n: start + 400 * math.log10(p) for n, p in self.strengths.items()}

    def p_beats(self, a: str, b: str) -> float:
        """The chance `a` is chosen over `b`, with no side lean."""
        pa, pb = self.strengths[a], self.strengths[b]
        return pa / (pa + pb)


def _aggregate(votes: Iterable[Vote], exclude: frozenset[str]) -> dict[tuple[str, str], list[float]]:
    """(first, second) -> [games, score of first]: an iteration then costs the number of pairs, not of votes."""
    agg: dict[tuple[str, str], list[float]] = {}
    for a, b, winner in votes:
        s = SCORE.get(winner)
        if s is None or a == b or a in exclude or b in exclude:
            continue
        g = agg.setdefault((a, b), [0.0, 0.0])
        g[0] += 1
        g[1] += s
    return agg


def bradley_terry(
    votes: Iterable[Vote], *, side: bool = False, iters: int = 200, exclude: Iterable[str] = (), prior: float = 0.01
) -> Fit:
    """Bradley-Terry strengths by the MM algorithm. `prior` gives every name that much of a win, so a name that
    lost every vote gets a small finite strength rather than zero (stills' arena uses 0.01). `exclude` drops the votes
    about some names (a retired variant)."""
    agg = _aggregate(votes, frozenset(exclude))
    names = sorted({n for pair in agg for n in pair})
    if not names:
        return Fit({})
    p, h = dict.fromkeys(names, 1.0), 1.0
    first_score = sum(g[1] for g in agg.values())
    for _ in range(iters):
        wins, denom = dict.fromkeys(names, prior), dict.fromkeys(names, 1e-9)
        for (a, b), (n, s) in agg.items():
            d = h * p[a] + p[b]
            wins[a] += s
            wins[b] += n - s
            denom[a] += n * h / d
            denom[b] += n / d
        q = {n: wins[n] / denom[n] for n in names}
        g = math.exp(sum(math.log(x) for x in q.values()) / len(q))
        new = {n: x / g for n, x in q.items()}
        if side:
            h = (first_score + prior) / (
                sum(n * new[a] / (h * new[a] + new[b]) for (a, b), (n, _) in agg.items()) + prior
            )
        done = all(abs(new[n] - p[n]) <= 1e-9 * max(1.0, p[n]) for n in names)
        p = new
        if done:
            break
    return Fit(p, h)


def intervals(
    votes: Sequence[Vote],
    *,
    n: int = 200,
    level: float = 0.90,
    side: bool = False,
    exclude: Iterable[str] = (),
    seed: int = 20261123,
) -> dict[str, tuple[float, float]]:
    """{name: (low, high)} of each Elo-scale rating over `n` resamples of the votes: how far to trust a gap. The
    default 90 % matches the arena's 5th and 95th percentiles."""
    if not votes:
        return {}
    rng = random.Random(seed)  # noqa: S311 - resampling, not secrets; seeded so a report repeats
    skip = frozenset(exclude)
    runs = [
        bradley_terry([votes[rng.randrange(len(votes))] for _ in votes], side=side, iters=60, exclude=skip).ratings()
        for _ in range(n)
    ]
    lo_q = (1 - level) / 2
    out: dict[str, tuple[float, float]] = {}
    for name in bradley_terry(votes, side=side, exclude=skip).strengths:
        xs = sorted(r[name] for r in runs if name in r)
        if xs:
            out[name] = (_quantile(xs, lo_q), _quantile(xs, 1 - lo_q))
    return out


def _quantile(sorted_xs: Sequence[float], q: float) -> float:
    """Linear interpolation between order statistics (numpy's default), so this agrees with the numpy versions."""
    pos = q * (len(sorted_xs) - 1)
    i = math.floor(pos)
    j = min(i + 1, len(sorted_xs) - 1)
    return sorted_xs[i] + (sorted_xs[j] - sorted_xs[i]) * (pos - i)


def head_to_head(votes: Iterable[Vote]) -> dict[tuple[str, str], list[int]]:
    """{(x, y): [x wins, y wins, ties]} over unordered pairs, the names sorted."""
    out: dict[tuple[str, str], list[int]] = {}
    for a, b, winner in votes:
        if winner not in SCORE or a == b:
            continue
        x, y = sorted((a, b))
        row = out.setdefault((x, y), [0, 0, 0])
        if winner == "tie":
            row[2] += 1
        else:
            row[0 if (winner == "a") == (a == x) else 1] += 1
    return out


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float, float]:
    """(share, low, high) for `k` successes in `n` trials: the Wilson score interval, right near 0 and 1 where the
    normal approximation is not. NaNs for n = 0."""
    if n == 0:
        return (math.nan, math.nan, math.nan)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return p, max(0.0, c - h), min(1.0, c + h)


def mcnemar(a_right: Iterable[bool], b_right: Iterable[bool]) -> tuple[int, int, float]:
    """Exact McNemar test for two candidates' right/wrong on the same items: (only a right, only b right, two-sided p
    from the binomial on the discordant pairs)."""
    only_a = only_b = 0
    for x, y in zip(a_right, b_right, strict=True):
        only_a += bool(x and not y)
        only_b += bool(y and not x)
    n = only_a + only_b
    if n == 0:
        return 0, 0, 1.0
    tail = sum(math.comb(n, i) for i in range(min(only_a, only_b) + 1)) / 2**n
    return only_a, only_b, min(1.0, 2 * tail)


def holm(pvalues: Sequence[float]) -> list[float]:
    """Holm's step-down adjustment of several comparisons' p values, returned in the order given (NaN stays NaN)."""
    order = sorted((i for i, p in enumerate(pvalues) if not math.isnan(p)), key=lambda i: pvalues[i])
    out = list(pvalues)
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (len(order) - rank) * pvalues[i]))
        out[i] = running
    return out
