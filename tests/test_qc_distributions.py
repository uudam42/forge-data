"""Unit tests for streaming statistics (app.qc.accumulator) and the
distribution check — non-finite reporting + configured range violations
(app.qc.checks.distributions)."""

from __future__ import annotations

import math

import pytest

from app.qc.accumulator import PercentileBuffer, WelfordAccumulator
from app.qc.checks.distributions import DistributionCheck
from app.qc.metrics import DatasetMetricsCollector
from app.qc.models import FeatureRangeConfig, QCConfig, QCErrorCode, Severity

CHECK = DistributionCheck()


def _sample(index: int, features: dict) -> dict:
    return {
        "sample_id": f"s{index}",
        "window": {"index": index, "start_timestamp": f"2026-08-30T18:00:{index:02d}Z", "end_timestamp": f"2026-08-30T18:00:{index+1:02d}Z", "row_count": 10},
        "features": features,
        "metadata": {},
    }


# ---------------------------------------------------------------------------
# WelfordAccumulator
# ---------------------------------------------------------------------------


def test_streaming_mean_correct() -> None:
    acc = WelfordAccumulator()
    for v in (1.0, 2.0, 3.0, 4.0):
        acc.update(v)
    assert acc.mean == 2.5


def test_streaming_population_std_correct() -> None:
    acc = WelfordAccumulator()
    for v in (2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0):
        acc.update(v)
    assert acc.std == pytest.approx(2.0)


def test_min_correct() -> None:
    acc = WelfordAccumulator()
    for v in (5.0, 1.0, 3.0):
        acc.update(v)
    assert acc.min == 1.0


def test_max_correct() -> None:
    acc = WelfordAccumulator()
    for v in (5.0, 1.0, 3.0):
        acc.update(v)
    assert acc.max == 5.0


def test_welford_matches_naive_variance_for_larger_sample() -> None:
    values = [float(i) for i in range(1, 101)]
    acc = WelfordAccumulator()
    for v in values:
        acc.update(v)
    naive_mean = sum(values) / len(values)
    naive_variance = sum((v - naive_mean) ** 2 for v in values) / len(values)
    assert acc.variance == pytest.approx(naive_variance)


# ---------------------------------------------------------------------------
# PercentileBuffer
# ---------------------------------------------------------------------------


def test_median_correct() -> None:
    buf = PercentileBuffer(max_values=1000)
    for v in (1.0, 2.0, 3.0, 4.0):
        buf.add(v)
    assert buf.median() == 2.5


def test_p05_p95_correct() -> None:
    buf = PercentileBuffer(max_values=1000)
    for v in range(1, 101):
        buf.add(float(v))
    percentiles = buf.percentiles()
    assert percentiles["p05"] == pytest.approx(5.95, abs=0.5)
    assert percentiles["p95"] == pytest.approx(95.05, abs=0.5)


def test_percentile_limit_truncation() -> None:
    buf = PercentileBuffer(max_values=5)
    for v in range(100):
        buf.add(float(v))
    assert buf.truncated is True
    assert len(buf._values) == 5


def test_percentile_no_truncation_under_limit() -> None:
    buf = PercentileBuffer(max_values=100)
    for v in range(5):
        buf.add(float(v))
    assert buf.truncated is False


def test_empty_percentile_buffer_returns_none() -> None:
    buf = PercentileBuffer(max_values=100)
    percentiles = buf.percentiles()
    assert all(v is None for v in percentiles.values())


# ---------------------------------------------------------------------------
# Non-finite value handling
# ---------------------------------------------------------------------------


def test_non_finite_feature_rejected_from_statistics_but_reported() -> None:
    collector = DatasetMetricsCollector(max_values_per_feature=1000)
    collector.observe_sample(0, _sample(0, {"imu": {"statistics": {"x": 1.0}}}))
    collector.observe_sample(1, _sample(1, {"imu": {"statistics": {"x": float("nan")}}}))
    collector.observe_sample(2, _sample(2, {"imu": {"statistics": {"x": 3.0}}}))

    accumulator = collector.metrics.feature_accumulators["features.imu.statistics.x"]
    assert accumulator.non_finite_count == 1
    assert accumulator.present_count == 2
    assert accumulator.welford.mean == 2.0  # NaN excluded, not poisoning the mean

    issues = CHECK.evaluate(collector.metrics, QCConfig())
    non_finite_issues = [i for i in issues if i.code == QCErrorCode.NON_FINITE_FEATURE_VALUE.value]
    assert len(non_finite_issues) == 1
    assert non_finite_issues[0].severity == Severity.ERROR
    assert non_finite_issues[0].observed == 1


def test_infinite_value_also_flagged() -> None:
    collector = DatasetMetricsCollector(max_values_per_feature=1000)
    collector.observe_sample(0, _sample(0, {"imu": {"statistics": {"x": math.inf}}}))
    accumulator = collector.metrics.feature_accumulators["features.imu.statistics.x"]
    assert accumulator.non_finite_count == 1
    assert accumulator.present_count == 0


# ---------------------------------------------------------------------------
# Configured feature range violations
# ---------------------------------------------------------------------------


def test_configured_feature_range_within_bounds_no_issue() -> None:
    collector = DatasetMetricsCollector(max_values_per_feature=1000)
    for i, v in enumerate((1.0, 2.0, 3.0)):
        collector.observe_sample(i, _sample(i, {"imu": {"statistics": {"x": v}}}))
    config = QCConfig(feature_ranges={"features.imu.statistics.x": FeatureRangeConfig(min=-100.0, max=100.0)})
    assert CHECK.evaluate(collector.metrics, config) == []


def test_range_violation_issue_correct() -> None:
    collector = DatasetMetricsCollector(max_values_per_feature=1000)
    for i, v in enumerate((1.0, 2.0, 500.0)):
        collector.observe_sample(i, _sample(i, {"imu": {"statistics": {"x": v}}}))
    config = QCConfig(
        feature_ranges={"features.imu.statistics.x": FeatureRangeConfig(min=-100.0, max=100.0, severity=Severity.WARNING)}
    )
    issues = CHECK.evaluate(collector.metrics, config)
    range_issues = [i for i in issues if i.code == QCErrorCode.FEATURE_RANGE_VIOLATION.value]
    assert len(range_issues) == 1
    assert range_issues[0].observed == 500.0
    assert range_issues[0].threshold == 100.0
    assert range_issues[0].severity == Severity.WARNING


def test_range_violation_below_minimum() -> None:
    collector = DatasetMetricsCollector(max_values_per_feature=1000)
    collector.observe_sample(0, _sample(0, {"imu": {"statistics": {"x": -500.0}}}))
    config = QCConfig(feature_ranges={"features.imu.statistics.x": FeatureRangeConfig(min=-100.0, max=100.0)})
    issues = CHECK.evaluate(collector.metrics, config)
    assert issues[0].observed == -500.0
    assert issues[0].threshold == -100.0


def test_range_config_for_absent_feature_no_crash() -> None:
    collector = DatasetMetricsCollector(max_values_per_feature=1000)
    collector.observe_sample(0, _sample(0, {}))
    config = QCConfig(feature_ranges={"features.imu.statistics.x": FeatureRangeConfig(min=0.0, max=1.0)})
    assert CHECK.evaluate(collector.metrics, config) == []
