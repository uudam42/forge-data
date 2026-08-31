"""Unit tests for the generic statistics functions
(app.transformation.features.statistics)."""

from __future__ import annotations

import math

import pytest

from app.transformation.features.common import UnknownFeatureError
from app.transformation.features.statistics import compute_statistic, validate_statistic_names


def test_mean() -> None:
    assert compute_statistic("mean", [1.0, 2.0, 3.0]) == 2.0


def test_std_is_population_not_sample() -> None:
    # population std of [2,4,4,4,5,5,7,9] is 2.0; sample std (ddof=1) would be ~2.138
    values = [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]
    assert compute_statistic("std", values) == pytest.approx(2.0)


def test_std_of_constant_values_is_zero() -> None:
    assert compute_statistic("std", [5.0, 5.0, 5.0]) == 0.0


def test_min_max() -> None:
    assert compute_statistic("min", [3.0, 1.0, 2.0]) == 1.0
    assert compute_statistic("max", [3.0, 1.0, 2.0]) == 3.0


def test_median_odd_and_even_length() -> None:
    assert compute_statistic("median", [1.0, 3.0, 2.0]) == 2.0
    assert compute_statistic("median", [1.0, 2.0, 3.0, 4.0]) == 2.5


def test_first_last_delta() -> None:
    values = [1.0, 5.0, 9.0]
    assert compute_statistic("first", values) == 1.0
    assert compute_statistic("last", values) == 9.0
    assert compute_statistic("delta", values) == 8.0


def test_empty_values_returns_none_for_every_statistic() -> None:
    for name in ("mean", "std", "min", "max", "median", "first", "last", "delta"):
        assert compute_statistic(name, []) is None


def test_unknown_statistic_raises() -> None:
    with pytest.raises(UnknownFeatureError):
        compute_statistic("bogus", [1.0, 2.0])


def test_validate_statistic_names_accepts_known_names() -> None:
    validate_statistic_names(["mean", "std", "min", "max"])  # no raise


def test_validate_statistic_names_rejects_unknown_name() -> None:
    with pytest.raises(UnknownFeatureError):
        validate_statistic_names(["mean", "typo_stat"])


def test_statistics_are_finite_for_normal_input() -> None:
    for name in ("mean", "std", "min", "max", "median", "first", "last", "delta"):
        result = compute_statistic(name, [1.0, 2.0, 3.0])
        assert math.isfinite(result)
