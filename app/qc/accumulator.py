"""Numerically stable streaming statistics.

WelfordAccumulator computes count/mean/variance/min/max in a single pass
without ever storing the underlying values or computing a numerically
fragile sum-of-squares — see Welford's online algorithm. Variance is
POPULATION variance (ddof=0), matching Step 7's documented convention.

PercentileBuffer retains up to `max_values` raw scalar values (in
first-encountered order — never randomly sampled) for exact percentile
computation. Beyond the cap, later values are dropped and `truncated` is
set; mean/std/min/max remain exact regardless, since those come from
WelfordAccumulator, not from this buffer.
"""

from __future__ import annotations

import math

_PERCENTILES = (0.01, 0.05, 0.25, 0.50, 0.75, 0.95, 0.99)


class WelfordAccumulator:
    def __init__(self) -> None:
        self.count = 0
        self.mean = 0.0
        self._m2 = 0.0
        self.min: float | None = None
        self.max: float | None = None

    def update(self, value: float) -> None:
        self.count += 1
        delta = value - self.mean
        self.mean += delta / self.count
        delta2 = value - self.mean
        self._m2 += delta * delta2
        self.min = value if self.min is None else min(self.min, value)
        self.max = value if self.max is None else max(self.max, value)

    @property
    def variance(self) -> float:
        return self._m2 / self.count if self.count else 0.0

    @property
    def std(self) -> float:
        return math.sqrt(self.variance)


class PercentileBuffer:
    """Deterministic percentile algorithm: linear interpolation between
    closest ranks (the same method as NumPy's default `interpolation="linear"`),
    documented here as the one canonical choice for this project."""

    def __init__(self, max_values: int) -> None:
        self._max_values = max_values
        self._values: list[float] = []
        self.truncated = False

    def add(self, value: float) -> None:
        if len(self._values) < self._max_values:
            self._values.append(value)
        else:
            self.truncated = True

    def percentiles(self) -> dict[str, float | None]:
        if not self._values:
            return {_label(p): None for p in _PERCENTILES}
        ordered = sorted(self._values)
        n = len(ordered)
        result: dict[str, float | None] = {}
        for p in _PERCENTILES:
            rank = p * (n - 1)
            lower = int(math.floor(rank))
            upper = int(math.ceil(rank))
            if lower == upper:
                value = ordered[lower]
            else:
                frac = rank - lower
                value = ordered[lower] + (ordered[upper] - ordered[lower]) * frac
            result[_label(p)] = value
        return result

    def median(self) -> float | None:
        return self.percentiles()["p50"]


def _label(p: float) -> str:
    return f"p{round(p * 100):02d}"
