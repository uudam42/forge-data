"""Unit tests for the constant/low-variance feature check
(app.qc.checks.variance)."""

from __future__ import annotations

from app.qc.checks.variance import VarianceCheck
from app.qc.metrics import DatasetMetricsCollector
from app.qc.models import QCConfig, QCErrorCode, Severity, VarianceConfig

CHECK = VarianceCheck()


def _sample(index: int, value: float) -> dict:
    return {
        "sample_id": f"s{index}",
        "window": {"index": index, "start_timestamp": f"2026-08-30T18:00:{index:02d}Z", "end_timestamp": f"2026-08-30T18:00:{index+1:02d}Z", "row_count": 10},
        "features": {"imu": {"statistics": {"gyro_z_std": value}}},
        "metadata": {},
    }


def test_constant_feature_detected() -> None:
    collector = DatasetMetricsCollector(max_values_per_feature=1000)
    for i in range(20):
        collector.observe_sample(i, _sample(i, 0.0))
    config = QCConfig(variance=VarianceConfig(enabled=True, minimum_variance=1e-12))
    issues = CHECK.evaluate(collector.metrics, config)
    assert len(issues) == 1
    assert issues[0].code == QCErrorCode.LOW_FEATURE_VARIANCE.value
    assert issues[0].observed == 0.0


def test_low_variance_detected() -> None:
    collector = DatasetMetricsCollector(max_values_per_feature=1000)
    values = [1.0, 1.0 + 1e-9, 1.0, 1.0 - 1e-9]
    for i, v in enumerate(values):
        collector.observe_sample(i, _sample(i, v))
    config = QCConfig(variance=VarianceConfig(enabled=True, minimum_variance=1e-12))
    issues = CHECK.evaluate(collector.metrics, config)
    assert len(issues) == 1


def test_normal_variance_no_issue() -> None:
    collector = DatasetMetricsCollector(max_values_per_feature=1000)
    for i, v in enumerate((1.0, 2.0, 3.0, 4.0, 5.0)):
        collector.observe_sample(i, _sample(i, v))
    config = QCConfig(variance=VarianceConfig(enabled=True, minimum_variance=1e-12))
    assert CHECK.evaluate(collector.metrics, config) == []


def test_variance_check_disabled_by_default_config() -> None:
    collector = DatasetMetricsCollector(max_values_per_feature=1000)
    for i in range(5):
        collector.observe_sample(i, _sample(i, 0.0))
    assert CHECK.evaluate(collector.metrics, QCConfig()) == []


def test_variance_check_explicitly_disabled() -> None:
    collector = DatasetMetricsCollector(max_values_per_feature=1000)
    for i in range(5):
        collector.observe_sample(i, _sample(i, 0.0))
    config = QCConfig(variance=VarianceConfig(enabled=False))
    assert CHECK.evaluate(collector.metrics, config) == []


def test_severity_configurable() -> None:
    collector = DatasetMetricsCollector(max_values_per_feature=1000)
    for i in range(5):
        collector.observe_sample(i, _sample(i, 0.0))
    config = QCConfig(variance=VarianceConfig(enabled=True, minimum_variance=1e-12, severity=Severity.ERROR))
    issues = CHECK.evaluate(collector.metrics, config)
    assert issues[0].severity == Severity.ERROR


def test_single_observation_skipped_not_flagged() -> None:
    collector = DatasetMetricsCollector(max_values_per_feature=1000)
    collector.observe_sample(0, _sample(0, 5.0))
    config = QCConfig(variance=VarianceConfig(enabled=True, minimum_variance=1e-12))
    assert CHECK.evaluate(collector.metrics, config) == []
