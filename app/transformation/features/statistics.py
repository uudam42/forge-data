"""Deterministic, documented numeric statistics for window feature
generation.

Standard deviation is POPULATION standard deviation (ddof=0) — a fixed,
documented choice, never "sample" std (ddof=1), to avoid ambiguity between
callers.
"""

from __future__ import annotations

import math

from app.transformation.features.common import UnknownFeatureError

SUPPORTED_STATISTICS = ("mean", "std", "min", "max", "median", "first", "last", "delta")


def validate_statistic_names(names: list[str]) -> None:
    for name in names:
        if name not in SUPPORTED_STATISTICS:
            raise UnknownFeatureError(f"Unknown statistic '{name}'")


def compute_statistic(name: str, values: list[float]) -> float | None:
    if not values:
        return None
    if name == "mean":
        return sum(values) / len(values)
    if name == "std":
        mean = sum(values) / len(values)
        variance = sum((v - mean) ** 2 for v in values) / len(values)
        return math.sqrt(variance)
    if name == "min":
        return min(values)
    if name == "max":
        return max(values)
    if name == "median":
        ordered = sorted(values)
        n = len(ordered)
        mid = n // 2
        if n % 2 == 1:
            return ordered[mid]
        return (ordered[mid - 1] + ordered[mid]) / 2
    if name == "first":
        return values[0]
    if name == "last":
        return values[-1]
    if name == "delta":
        return values[-1] - values[0]
    raise UnknownFeatureError(f"Unknown statistic '{name}'")
