"""Unit tests for baseline drift comparison (app.qc.checks.drift)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.qc.checks.drift import BaselineNotFoundError, evaluate_drift, load_baseline
from app.qc.models import DriftConfig, ProfileRef, QCErrorCode, Severity
from app.storage.qc_store import LocalQCReportStore


def _profile() -> ProfileRef:
    return ProfileRef(name="default_dataset_qc", version="1.0.0")


def _baseline_manifest(profile_name="default_dataset_qc", profile_version="1.0.0") -> dict:
    return {"profile": {"name": profile_name, "version": profile_version}}


def _baseline_report(features: dict) -> dict:
    return {"features": features}


def test_standardized_mean_difference_correct() -> None:
    current = {"features.imu.statistics.x": {"mean": 3.0, "std": 1.0}}
    baseline_manifest = _baseline_manifest()
    baseline_report = _baseline_report({"features.imu.statistics.x": {"mean": 1.0, "std": 1.0}})
    summary, issues = evaluate_drift(
        current_features=current,
        baseline_manifest=baseline_manifest,
        baseline_report=baseline_report,
        baseline_qc_id="qc_baseline",
        current_profile=_profile(),
        drift_config=None,
    )
    result = summary.features["features.imu.statistics.x"]
    assert result.compared is True
    assert result.standardized_mean_difference == pytest.approx(2.0)


def test_baseline_std_zero_and_equal_means_is_zero_drift() -> None:
    current = {"features.imu.statistics.x": {"mean": 5.0, "std": 0.0}}
    baseline_report = _baseline_report({"features.imu.statistics.x": {"mean": 5.0, "std": 0.0}})
    summary, issues = evaluate_drift(
        current_features=current,
        baseline_manifest=_baseline_manifest(),
        baseline_report=baseline_report,
        baseline_qc_id="qc_baseline",
        current_profile=_profile(),
        drift_config=None,
    )
    result = summary.features["features.imu.statistics.x"]
    assert result.standardized_mean_difference == 0.0
    assert result.reason is None


def test_baseline_std_zero_handled_safely_when_means_differ() -> None:
    current = {"features.imu.statistics.x": {"mean": 6.0, "std": 0.0}}
    baseline_report = _baseline_report({"features.imu.statistics.x": {"mean": 5.0, "std": 0.0}})
    summary, issues = evaluate_drift(
        current_features=current,
        baseline_manifest=_baseline_manifest(),
        baseline_report=baseline_report,
        baseline_qc_id="qc_baseline",
        current_profile=_profile(),
        drift_config=None,
    )
    result = summary.features["features.imu.statistics.x"]
    assert result.standardized_mean_difference is None
    assert result.reason == "baseline_std_zero_mean_shifted"
    # Must never raise or produce a non-finite value.


def test_drift_warning_detected() -> None:
    current = {"features.imu.statistics.x": {"mean": 10.0, "std": 1.0}}
    baseline_report = _baseline_report({"features.imu.statistics.x": {"mean": 0.0, "std": 1.0}})
    drift_config = DriftConfig(enabled=True, max_abs_standardized_mean_difference=1.0, severity=Severity.WARNING)
    summary, issues = evaluate_drift(
        current_features=current,
        baseline_manifest=_baseline_manifest(),
        baseline_report=baseline_report,
        baseline_qc_id="qc_baseline",
        current_profile=_profile(),
        drift_config=drift_config,
    )
    drift_issues = [i for i in issues if i.code == QCErrorCode.FEATURE_DISTRIBUTION_DRIFT.value]
    assert len(drift_issues) == 1
    assert drift_issues[0].severity == Severity.WARNING


def test_drift_error_when_configured() -> None:
    current = {"features.imu.statistics.x": {"mean": 10.0, "std": 1.0}}
    baseline_report = _baseline_report({"features.imu.statistics.x": {"mean": 0.0, "std": 1.0}})
    drift_config = DriftConfig(enabled=True, max_abs_standardized_mean_difference=1.0, severity=Severity.ERROR)
    _, issues = evaluate_drift(
        current_features=current,
        baseline_manifest=_baseline_manifest(),
        baseline_report=baseline_report,
        baseline_qc_id="qc_baseline",
        current_profile=_profile(),
        drift_config=drift_config,
    )
    assert issues[0].severity == Severity.ERROR


def test_zero_shift_baseline_std_zero_flags_drift_when_enabled() -> None:
    current = {"features.imu.statistics.x": {"mean": 6.0, "std": 0.0}}
    baseline_report = _baseline_report({"features.imu.statistics.x": {"mean": 5.0, "std": 0.0}})
    drift_config = DriftConfig(enabled=True, max_abs_standardized_mean_difference=1.0)
    _, issues = evaluate_drift(
        current_features=current,
        baseline_manifest=_baseline_manifest(),
        baseline_report=baseline_report,
        baseline_qc_id="qc_baseline",
        current_profile=_profile(),
        drift_config=drift_config,
    )
    assert any(i.code == QCErrorCode.FEATURE_DISTRIBUTION_DRIFT.value for i in issues)


def test_no_drift_within_threshold() -> None:
    current = {"features.imu.statistics.x": {"mean": 0.5, "std": 1.0}}
    baseline_report = _baseline_report({"features.imu.statistics.x": {"mean": 0.0, "std": 1.0}})
    drift_config = DriftConfig(enabled=True, max_abs_standardized_mean_difference=1.0)
    _, issues = evaluate_drift(
        current_features=current,
        baseline_manifest=_baseline_manifest(),
        baseline_report=baseline_report,
        baseline_qc_id="qc_baseline",
        current_profile=_profile(),
        drift_config=drift_config,
    )
    assert issues == []


def test_missing_baseline_feature_skipped_and_documented() -> None:
    current = {"features.imu.statistics.x": {"mean": 1.0, "std": 1.0}}
    baseline_report = _baseline_report({})
    summary, issues = evaluate_drift(
        current_features=current,
        baseline_manifest=_baseline_manifest(),
        baseline_report=baseline_report,
        baseline_qc_id="qc_baseline",
        current_profile=_profile(),
        drift_config=None,
    )
    result = summary.features["features.imu.statistics.x"]
    assert result.compared is False
    assert result.reason == "missing_in_baseline"
    assert issues == []


def test_missing_current_feature_skipped() -> None:
    current = {"features.imu.statistics.x": {"mean": None, "std": None}}
    baseline_report = _baseline_report({"features.imu.statistics.x": {"mean": 1.0, "std": 1.0}})
    summary, _ = evaluate_drift(
        current_features=current,
        baseline_manifest=_baseline_manifest(),
        baseline_report=baseline_report,
        baseline_qc_id="qc_baseline",
        current_profile=_profile(),
        drift_config=None,
    )
    assert summary.features["features.imu.statistics.x"].compared is False
    assert summary.features["features.imu.statistics.x"].reason == "missing_in_current"


def test_baseline_compatibility_check() -> None:
    current = {}
    summary, issues = evaluate_drift(
        current_features=current,
        baseline_manifest=_baseline_manifest(profile_name="other_profile"),
        baseline_report=_baseline_report({}),
        baseline_qc_id="qc_baseline",
        current_profile=_profile(),
        drift_config=None,
    )
    assert summary.compatible is False
    assert summary.incompatibility_reason is not None
    assert any(i.code == QCErrorCode.BASELINE_INCOMPATIBLE.value for i in issues)


def test_compatible_baseline_reports_compatible_true() -> None:
    summary, issues = evaluate_drift(
        current_features={},
        baseline_manifest=_baseline_manifest(),
        baseline_report=_baseline_report({}),
        baseline_qc_id="qc_baseline",
        current_profile=_profile(),
        drift_config=None,
    )
    assert summary.compatible is True
    assert issues == []


def test_no_drift_config_still_reports_scores_without_issues() -> None:
    current = {"features.imu.statistics.x": {"mean": 100.0, "std": 1.0}}
    baseline_report = _baseline_report({"features.imu.statistics.x": {"mean": 0.0, "std": 1.0}})
    summary, issues = evaluate_drift(
        current_features=current,
        baseline_manifest=_baseline_manifest(),
        baseline_report=baseline_report,
        baseline_qc_id="qc_baseline",
        current_profile=_profile(),
        drift_config=None,
    )
    assert summary.features["features.imu.statistics.x"].standardized_mean_difference == 100.0
    assert issues == []  # scores computed, but no issue without an enabled drift config


# ---------------------------------------------------------------------------
# load_baseline / storage integration
# ---------------------------------------------------------------------------


def test_baseline_qc_lookup_works(tmp_path: Path) -> None:
    store = LocalQCReportStore(root=tmp_path / "qc")
    staging = store.staging_dir(transformation_id="xform_a", qc_id="qc_baseline")
    report = {"features": {"features.imu.statistics.x": {"mean": 1.0, "std": 1.0}}}
    (staging / "report.json").write_text(json.dumps(report))
    report_uri = f"file://{store.report_path(transformation_id='xform_a', qc_id='qc_baseline')}"
    manifest = {
        "qc_id": "qc_baseline",
        "profile": {"name": "default_dataset_qc", "version": "1.0.0"},
        "report_uri": report_uri,
    }
    (staging / "manifest.json").write_text(json.dumps(manifest))
    store.commit(transformation_id="xform_a", qc_id="qc_baseline", staging_dir=staging)

    loaded_manifest, loaded_report = load_baseline(store, "qc_baseline")
    assert loaded_manifest["qc_id"] == "qc_baseline"
    assert loaded_report["features"]["features.imu.statistics.x"]["mean"] == 1.0


def test_baseline_missing_raises_not_found(tmp_path: Path) -> None:
    store = LocalQCReportStore(root=tmp_path / "qc")
    with pytest.raises(BaselineNotFoundError):
        load_baseline(store, "does_not_exist")
