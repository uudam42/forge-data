"""Unit tests for fixed-rate timeline generation and its interval policy."""

from __future__ import annotations

import pytest

from app.synchronization.timeline import (
    InvalidSyncConfigurationError,
    fixed_rate_period_us,
    fixed_rate_timeline,
    intersection_interval,
)


def test_fixed_rate_timeline_correct() -> None:
    targets = list(fixed_rate_timeline(start_epoch_us=0, end_epoch_us=5_000_000, period_us=1_000_000))
    assert targets == [0, 1_000_000, 2_000_000, 3_000_000, 4_000_000, 5_000_000]


def test_fixed_rate_uses_stream_range_intersection() -> None:
    # stream A: 0s -> 10s, stream B: 2s -> 8s => intersection 2s -> 8s
    start, end = intersection_interval([(0, 10_000_000), (2_000_000, 8_000_000)])
    assert start == 2_000_000
    assert end == 8_000_000


def test_intersection_with_no_overlap_yields_start_after_end() -> None:
    start, end = intersection_interval([(0, 1_000_000), (5_000_000, 6_000_000)])
    assert start > end


def test_fixed_rate_timeline_empty_when_no_overlap() -> None:
    targets = list(fixed_rate_timeline(start_epoch_us=5_000_000, end_epoch_us=1_000_000, period_us=1_000_000))
    assert targets == []


def test_fixed_rate_timeline_never_extrapolates_beyond_end() -> None:
    targets = list(fixed_rate_timeline(start_epoch_us=0, end_epoch_us=2_500_000, period_us=1_000_000))
    assert targets == [0, 1_000_000, 2_000_000]
    assert all(t <= 2_500_000 for t in targets)


def test_fixed_rate_period_us_correct_for_10hz() -> None:
    assert fixed_rate_period_us(10.0, max_frequency_hz=1000.0) == 100_000


def test_invalid_frequency_zero_rejected() -> None:
    with pytest.raises(InvalidSyncConfigurationError):
        fixed_rate_period_us(0.0, max_frequency_hz=1000.0)


def test_invalid_frequency_negative_rejected() -> None:
    with pytest.raises(InvalidSyncConfigurationError):
        fixed_rate_period_us(-5.0, max_frequency_hz=1000.0)


def test_excessive_frequency_rejected_according_to_config() -> None:
    with pytest.raises(InvalidSyncConfigurationError):
        fixed_rate_period_us(5000.0, max_frequency_hz=1000.0)


def test_frequency_at_exactly_the_max_is_allowed() -> None:
    assert fixed_rate_period_us(1000.0, max_frequency_hz=1000.0) == 1000
