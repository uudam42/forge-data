"""Unit tests for the dataset size check (app.qc.checks.dataset_size)."""

from __future__ import annotations

from app.qc.checks.dataset_size import DatasetSizeCheck
from app.qc.metrics import DatasetMetrics
from app.qc.models import QCConfig, QCErrorCode, Severity

CHECK = DatasetSizeCheck()


def test_empty_dataset_always_flagged_regardless_of_config() -> None:
    metrics = DatasetMetrics(sample_count=0)
    issues = CHECK.evaluate(metrics, QCConfig())
    assert len(issues) == 1
    assert issues[0].code == QCErrorCode.EMPTY_DATASET.value
    assert issues[0].severity == Severity.ERROR


def test_empty_dataset_flagged_even_with_no_minimum_samples_configured() -> None:
    metrics = DatasetMetrics(sample_count=0)
    issues = CHECK.evaluate(metrics, QCConfig(minimum_samples=None))
    assert any(i.code == QCErrorCode.EMPTY_DATASET.value for i in issues)


def test_dataset_too_small_when_below_minimum() -> None:
    metrics = DatasetMetrics(sample_count=47)
    issues = CHECK.evaluate(metrics, QCConfig(minimum_samples=100))
    assert len(issues) == 1
    assert issues[0].code == QCErrorCode.DATASET_TOO_SMALL.value
    assert issues[0].observed == 47
    assert issues[0].threshold == 100


def test_dataset_size_default_severity_is_error() -> None:
    metrics = DatasetMetrics(sample_count=5)
    issues = CHECK.evaluate(metrics, QCConfig(minimum_samples=100))
    assert issues[0].severity == Severity.ERROR


def test_dataset_size_severity_configurable() -> None:
    metrics = DatasetMetrics(sample_count=5)
    issues = CHECK.evaluate(metrics, QCConfig(minimum_samples=100, dataset_size_severity=Severity.WARNING))
    assert issues[0].severity == Severity.WARNING


def test_no_minimum_samples_configured_never_flags_small_dataset() -> None:
    metrics = DatasetMetrics(sample_count=3)
    issues = CHECK.evaluate(metrics, QCConfig(minimum_samples=None))
    assert issues == []


def test_dataset_meeting_minimum_passes() -> None:
    metrics = DatasetMetrics(sample_count=100)
    issues = CHECK.evaluate(metrics, QCConfig(minimum_samples=100))
    assert issues == []


def test_no_configured_threshold_does_not_invent_a_failure() -> None:
    metrics = DatasetMetrics(sample_count=1)
    issues = CHECK.evaluate(metrics, QCConfig())
    assert issues == []
