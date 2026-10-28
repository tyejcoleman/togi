"""Statistics used by the analysis, pure and dependency-free. Each has a known value in the tests.

    wilson(k, n)             Wilson score interval for a proportion (95% by default)
    mcnemar_exact(b, c)      two-sided exact McNemar p-value from the discordant counts (a two-sided exact binomial test at 0.5)
    paired_bootstrap(diffs)  percentile CI for the mean of paired differences, resampling CLUSTERS (items) with a seeded RNG
    pearson(x, y)            correlation (point-biserial when one side is 0/1), None when it is undefined
    isotonic(ys)             non-decreasing least-squares fit (pool adjacent violators)
    interp_inverse(...)      the x at which a monotone curve reaches a target y
    pareto_front(points)     non-dominated points for (minimise x, maximise y)
"""
from __future__ import annotations

import math
import random
from fractions import Fraction
from typing import Sequence

Z95 = 1.959963984540054


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    """(low, high) of the Wilson score interval. n = 0 gives (0, 1): nothing is known."""
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    lo = 0.0 if k == 0 else max(0.0, centre - half)       # the bound at the edge is exact, not 0.9999999999999999
    hi = 1.0 if k == n else min(1.0, centre + half)
    return lo, hi


def mcnemar_exact(b: int, c: int) -> float:
    """Exact two-sided McNemar: under H0 the b+c discordant pairs split 50/50. p = min(1, 2 * P(X <= min(b, c)))."""
    n = b + c
    if n == 0:
        return 1.0
    lo = min(b, c)
    tail = Fraction(sum(math.comb(n, i) for i in range(lo + 1)), 2 ** n)
    return float(min(Fraction(1), 2 * tail))


def paired_bootstrap(diffs: Sequence[float], *, resamples: int = 10000, seed: int = 20261027, alpha: float = 0.05) -> dict:
    """Bootstrap the mean of per-cluster paired differences (one value per item: A's mean success minus B's). Resamples
    clusters with replacement using `random.Random(seed)`, so the same seed gives the same interval. `p_two_sided` is the
    share of resampled means on the other side of zero than the observed mean, doubled."""
    n = len(diffs)
    if n == 0:
        return {"mean": None, "lo": None, "hi": None, "n": 0, "resamples": 0, "seed": seed, "p_two_sided": None}
    rng = random.Random(seed)
    mean = sum(diffs) / n
    means = []
    for _ in range(resamples):
        s = 0.0
        for _j in range(n):
            s += diffs[rng.randrange(n)]
        means.append(s / n)
    means.sort()
    lo = means[int((alpha / 2) * resamples)]
    hi = means[min(resamples - 1, int((1 - alpha / 2) * resamples))]
    other = sum(1 for m in means if (m <= 0 if mean > 0 else m >= 0))
    p = min(1.0, 2 * other / resamples) if mean != 0 else 1.0
    return {"mean": mean, "lo": lo, "hi": hi, "n": n, "resamples": resamples, "seed": seed, "p_two_sided": p}


def pearson(x: Sequence[float], y: Sequence[float]) -> float | None:
    n = len(x)
    if n < 3 or n != len(y):
        return None
    mx, my = sum(x) / n, sum(y) / n
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx <= 1e-12 or syy <= 1e-12:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / math.sqrt(sxx * syy)


def median(xs: Sequence[float]) -> float | None:
    s = sorted(xs)
    if not s:
        return None
    m = len(s) // 2
    return float(s[m]) if len(s) % 2 else (s[m - 1] + s[m]) / 2


def isotonic(ys: Sequence[float], weights: Sequence[float] | None = None) -> list[float]:
    """Pool-adjacent-violators: the non-decreasing sequence closest to `ys` in (weighted) least squares."""
    w = list(weights) if weights else [1.0] * len(ys)
    blocks: list[list[float]] = []      # [sum_wy, sum_w, count]
    for y, wi in zip(ys, w):
        blocks.append([y * wi, wi, 1])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            b = blocks.pop()
            blocks[-1] = [blocks[-1][0] + b[0], blocks[-1][1] + b[1], blocks[-1][2] + b[2]]
    out: list[float] = []
    for s, wt, cnt in blocks:
        out += [s / wt] * cnt
    return out


def interp_inverse(xs: Sequence[float], ys: Sequence[float], target: float) -> tuple[float | None, str]:
    """On a non-decreasing curve (xs ascending, in log space), the x where y first reaches `target`. Returns (x, note) with
    note 'in_range', 'below_range' (the curve is already above target at the smallest x: x is the smallest) or
    'above_range' (it never gets there: None)."""
    if not xs:
        return None, "empty"
    if target <= ys[0]:
        return xs[0], "below_range"
    for i in range(1, len(xs)):
        if ys[i] >= target:
            if ys[i] == ys[i - 1]:
                return xs[i - 1], "in_range"
            t = (target - ys[i - 1]) / (ys[i] - ys[i - 1])
            return math.exp(math.log(xs[i - 1]) + t * (math.log(xs[i]) - math.log(xs[i - 1]))), "in_range"
    return None, "above_range"


def pareto_front(points: Sequence[tuple[float, float, str]]) -> list[str]:
    """Ids of the points no other point beats on both axes: lower x (cost / latency) AND higher y (success)."""
    front = []
    for x, y, pid in points:
        dominated = any((x2 <= x and y2 >= y) and (x2 < x or y2 > y) for x2, y2, q in points if q != pid)
        if not dominated:
            front.append(pid)
    return front


def slope(xs: Sequence[float], ys: Sequence[float], ws: Sequence[float] | None = None) -> float | None:
    """Weighted least-squares slope of y on x; None if undefined."""
    if len(xs) < 2:
        return None
    w = list(ws) if ws else [1.0] * len(xs)
    sw = sum(w)
    mx = sum(a * b for a, b in zip(xs, w)) / sw
    my = sum(a * b for a, b in zip(ys, w)) / sw
    sxx = sum(wi * (a - mx) ** 2 for a, wi in zip(xs, w))
    if sxx <= 1e-12:
        return None
    return sum(wi * (a - mx) * (b - my) for a, b, wi in zip(xs, ys, w)) / sxx
