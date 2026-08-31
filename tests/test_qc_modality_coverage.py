"""Unit tests for dataset-level modality coverage aggregation
(app.qc.metrics) and the threshold check (app.qc.checks.modality_coverage)."""

from __future__ import annotations

from app.qc.checks.modality_coverage import ModalityCoverageCheck
from app.qc.metrics import DatasetMetricsCollector
from app.qc.models import ModalityCoverageThreshold, QCConfig, QCErrorCode, Severity

CHECK = ModalityCoverageCheck()


def _sample(sample_id: str, *, imu_present: bool, gps_present: bool, gps_coverage: float | None = None) -> dict:
    return {
        "sample_id": sample_id,
        "window": {"index": 0, "start_timestamp": "2026-08-30T18:00:00Z", "end_timestamp": "2026-08-30T18:00:01Z", "row_count": 10},
        "features": {},
        "modality_mask": {"imu": imu_present, "gps": gps_present},
        "modality_coverage": {"imu": 1.0 if imu_present else 0.0, "gps": gps_coverage if gps_coverage is not None else (1.0 if gps_present else 0.0)},
        "metadata": {"source_row_start": 0, "source_row_end": 9},
    }


def test_imu_coverage_correct() -> None:
    collector = DatasetMetricsCollector(max_values_per_feature=1000)
    for i in range(10):
        collector.observe_sample(i, _sample(f"s{i}", imu_present=True, gps_present=False))
    assert collector.metrics.modality["imu"].present_count == 10


def test_gps_coverage_correct() -> None:
    collector = DatasetMetricsCollector(max_values_per_feature=1000)
    for i in range(10):
        collector.observe_sample(i, _sample(f"s{i}", imu_present=True, gps_present=(i % 2 == 0)))
    assert collector.metrics.modality["gps"].present_count == 5


def test_low_modality_coverage_warning() -> None:
    collector = DatasetMetricsCollector(max_values_per_feature=1000)
    for i in range(10):
        collector.observe_sample(i, _sample(f"s{i}", imu_present=True, gps_present=(i < 6)))
    config = QCConfig(modality_coverage={"gps": ModalityCoverageThreshold(minimum_ratio=0.80, severity=Severity.WARNING)})
    issues = CHECK.evaluate(collector.metrics, config)
    assert len(issues) == 1
    assert issues[0].code == QCErrorCode.LOW_MODALITY_COVERAGE.value
    assert issues[0].severity == Severity.WARNING
    assert issues[0].path == "gps"
    assert issues[0].observed == 0.6


def test_low_modality_coverage_error() -> None:
    collector = DatasetMetricsCollector(max_values_per_feature=1000)
    for i in range(10):
        collector.observe_sample(i, _sample(f"s{i}", imu_present=(i < 9), gps_present=True))
    config = QCConfig(modality_coverage={"imu": ModalityCoverageThreshold(minimum_ratio=0.95, severity=Severity.ERROR)})
    issues = CHECK.evaluate(collector.metrics, config)
    assert len(issues) == 1
    assert issues[0].severity == Severity.ERROR


def test_coverage_meeting_threshold_no_issue() -> None:
    collector = DatasetMetricsCollector(max_values_per_feature=1000)
    for i in range(10):
        collector.observe_sample(i, _sample(f"s{i}", imu_present=True, gps_present=True))
    config = QCConfig(modality_coverage={"imu": ModalityCoverageThreshold(minimum_ratio=1.0)})
    assert CHECK.evaluate(collector.metrics, config) == []


def test_no_threshold_configured_no_issue() -> None:
    collector = DatasetMetricsCollector(max_values_per_feature=1000)
    collector.observe_sample(0, _sample("s0", imu_present=True, gps_present=False))
    assert CHECK.evaluate(collector.metrics, QCConfig()) == []


def test_dataset_level_window_coverage_aggregate() -> None:
    collector = DatasetMetricsCollector(max_values_per_feature=1000)
    coverages = [0.2, 0.5, 0.0]
    for i, cov in enumerate(coverages):
        collector.observe_sample(i, _sample(f"s{i}", imu_present=True, gps_present=(cov > 0), gps_coverage=cov))
    stats = collector.metrics.modality["gps"].coverage_stats()
    assert stats["mean"] == sum(coverages) / 3
    assert stats["min"] == 0.0
    assert stats["max"] == 0.5
    assert stats["median"] == 0.2


def test_modality_present_via_coverage_fallback_when_no_mask() -> None:
    collector = DatasetMetricsCollector(max_values_per_feature=1000)
    sample = {
        "sample_id": "s0",
        "window": {"index": 0, "start_timestamp": "2026-08-30T18:00:00Z", "end_timestamp": "2026-08-30T18:00:01Z", "row_count": 10},
        "features": {},
        "modality_coverage": {"gps": 0.4},
        "metadata": {},
    }
    collector.observe_sample(0, sample)
    assert collector.metrics.modality["gps"].present_count == 1


def test_empty_dataset_no_modality_issues() -> None:
    collector = DatasetMetricsCollector(max_values_per_feature=1000)
    config = QCConfig(modality_coverage={"imu": ModalityCoverageThreshold(minimum_ratio=0.9)})
    assert CHECK.evaluate(collector.metrics, config) == []
