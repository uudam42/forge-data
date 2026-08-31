"""Unit/service-level tests for the QC registry, profile validation,
duplicate-sample-id / temporal / window-size metrics, session distribution,
and mutation-safety guarantees not already covered by the HTTP-level test
files."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.qc.checks.identifiers import DuplicateSampleIdCheck
from app.qc.checks.temporal import TemporalOrderCheck
from app.qc.metrics import DatasetMetricsCollector
from app.qc.models import DriftConfig, FeatureRangeConfig, ModalityCoverageThreshold, QCConfig, QCErrorCode
from app.qc.profiles.base import InvalidQCConfigurationError
from app.qc.profiles.default import DEFAULT_DATASET_QC
from app.qc.registry import QCProfileNotFoundError, QCProfileRegistry

QC_URL = "/api/v1/qc"

IMU_CSV = "timestamp,accel_x,accel_y,accel_z,gyro_x,gyro_y,gyro_z\n" + "".join(
    f"2026-08-30T18:00:{i:02d}Z,0.{i},0.2,9.8,0.01,0.02,0.03\n" for i in range(20)
)
GPS_CSV = "timestamp,latitude,longitude,altitude,speed\n" + "".join(
    f"2026-08-30T18:00:{i:02d}Z,34.02{i:02d},-118.28{i:02d},100.0,9.{i}\n" for i in range(0, 20, 4)
)


def _sample(index: int, sample_id: str, start: str, end: str, row_count: int = 10) -> dict:
    return {
        "sample_id": sample_id,
        "window": {"index": index, "start_timestamp": start, "end_timestamp": end, "row_count": row_count},
        "features": {},
        "metadata": {},
    }


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_finds_builtin_profile() -> None:
    registry = QCProfileRegistry()
    assert registry.get("default_dataset_qc", "1.0.0") is DEFAULT_DATASET_QC


def test_registry_raises_for_unknown_profile() -> None:
    registry = QCProfileRegistry()
    with pytest.raises(QCProfileNotFoundError):
        registry.get("does_not_exist", "1.0.0")


def test_registry_raises_for_wrong_version() -> None:
    registry = QCProfileRegistry()
    with pytest.raises(QCProfileNotFoundError):
        registry.get("default_dataset_qc", "9.9.9")


def test_registry_list_profiles() -> None:
    registry = QCProfileRegistry()
    assert ("default_dataset_qc", "1.0.0") in registry.list_profiles()


# ---------------------------------------------------------------------------
# Profile validation
# ---------------------------------------------------------------------------


def test_validate_config_rejects_negative_minimum_samples() -> None:
    with pytest.raises(InvalidQCConfigurationError):
        DEFAULT_DATASET_QC.validate_config(QCConfig(minimum_samples=-1))


def test_validate_config_rejects_modality_ratio_out_of_range() -> None:
    with pytest.raises(InvalidQCConfigurationError):
        DEFAULT_DATASET_QC.validate_config(
            QCConfig(modality_coverage={"imu": ModalityCoverageThreshold(minimum_ratio=1.5)})
        )


def test_validate_config_rejects_negative_minimum_variance() -> None:
    from app.qc.models import VarianceConfig

    with pytest.raises(InvalidQCConfigurationError):
        DEFAULT_DATASET_QC.validate_config(QCConfig(variance=VarianceConfig(minimum_variance=-1.0)))


def test_validate_config_rejects_inverted_feature_range() -> None:
    with pytest.raises(InvalidQCConfigurationError):
        DEFAULT_DATASET_QC.validate_config(
            QCConfig(feature_ranges={"features.imu.statistics.x": FeatureRangeConfig(min=10.0, max=0.0)})
        )


def test_validate_config_rejects_max_group_fraction_out_of_range() -> None:
    with pytest.raises(InvalidQCConfigurationError):
        DEFAULT_DATASET_QC.validate_config(QCConfig(max_group_fraction=1.5))


def test_validate_config_rejects_drift_enabled_without_baseline() -> None:
    with pytest.raises(InvalidQCConfigurationError):
        DEFAULT_DATASET_QC.validate_config(QCConfig(drift=DriftConfig(enabled=True), baseline_qc_id=None))


def test_validate_config_accepts_well_formed_config() -> None:
    DEFAULT_DATASET_QC.validate_config(
        QCConfig(
            minimum_samples=5,
            modality_coverage={"imu": ModalityCoverageThreshold(minimum_ratio=0.9)},
            drift=DriftConfig(enabled=True),
            baseline_qc_id="qc_baseline",
        )
    )  # no raise


def test_no_configured_threshold_does_not_invent_subjective_failure() -> None:
    DEFAULT_DATASET_QC.validate_config(QCConfig())  # no raise; empty config is valid


# ---------------------------------------------------------------------------
# Duplicate sample IDs
# ---------------------------------------------------------------------------


def test_duplicate_sample_id_detected() -> None:
    collector = DatasetMetricsCollector(max_values_per_feature=1000)
    collector.observe_sample(0, _sample(0, "dup", "2026-08-30T18:00:00Z", "2026-08-30T18:00:01Z"))
    collector.observe_sample(1, _sample(1, "dup", "2026-08-30T18:00:01Z", "2026-08-30T18:00:02Z"))
    issues = DuplicateSampleIdCheck().evaluate(collector.metrics, QCConfig())
    assert len(issues) == 1
    assert issues[0].code == QCErrorCode.DUPLICATE_SAMPLE_ID.value
    assert issues[0].observed == 1
    assert issues[0].threshold == 0


def test_unique_sample_ids_pass() -> None:
    collector = DatasetMetricsCollector(max_values_per_feature=1000)
    for i in range(5):
        collector.observe_sample(i, _sample(i, f"s{i}", f"2026-08-30T18:00:{i:02d}Z", f"2026-08-30T18:00:{i+1:02d}Z"))
    assert DuplicateSampleIdCheck().evaluate(collector.metrics, QCConfig()) == []


# ---------------------------------------------------------------------------
# Temporal / window-size metrics
# ---------------------------------------------------------------------------


def test_window_row_count_distribution_correct() -> None:
    collector = DatasetMetricsCollector(max_values_per_feature=1000)
    for i, rc in enumerate((10, 10, 8, 10)):
        collector.observe_sample(i, _sample(i, f"s{i}", f"2026-08-30T18:00:{i:02d}Z", f"2026-08-30T18:00:{i+1:02d}Z", row_count=rc))
    m = collector.metrics
    assert m.window_row_counts.min == 8
    assert m.window_row_counts.max == 10
    assert m.window_row_counts.mean == pytest.approx(9.5)


def test_earliest_and_latest_timestamp_and_duration_correct() -> None:
    collector = DatasetMetricsCollector(max_values_per_feature=1000)
    collector.observe_sample(0, _sample(0, "s0", "2026-08-30T18:00:00Z", "2026-08-30T18:00:10Z"))
    collector.observe_sample(1, _sample(1, "s1", "2026-08-30T18:00:05Z", "2026-08-30T18:00:15Z"))
    m = collector.metrics
    assert m.earliest_timestamp == "2026-08-30T18:00:00Z"
    assert m.latest_timestamp == "2026-08-30T18:00:15Z"
    assert m.duration_seconds == pytest.approx(15.0)


def test_non_monotonic_sample_time_detected() -> None:
    collector = DatasetMetricsCollector(max_values_per_feature=1000)
    collector.observe_sample(0, _sample(0, "s0", "2026-08-30T18:00:10Z", "2026-08-30T18:00:11Z"))
    collector.observe_sample(1, _sample(1, "s1", "2026-08-30T18:00:05Z", "2026-08-30T18:00:06Z"))
    issues = TemporalOrderCheck().evaluate(collector.metrics, QCConfig())
    assert len(issues) == 1
    assert issues[0].code == QCErrorCode.NON_MONOTONIC_SAMPLE_TIME.value


def test_monotonic_order_no_issue() -> None:
    collector = DatasetMetricsCollector(max_values_per_feature=1000)
    collector.observe_sample(0, _sample(0, "s0", "2026-08-30T18:00:00Z", "2026-08-30T18:00:01Z"))
    collector.observe_sample(1, _sample(1, "s1", "2026-08-30T18:00:01Z", "2026-08-30T18:00:02Z"))
    assert TemporalOrderCheck().evaluate(collector.metrics, QCConfig()) == []


# ---------------------------------------------------------------------------
# Mutation safety
# ---------------------------------------------------------------------------


def test_qc_does_not_mutate_in_memory_parsed_samples() -> None:
    collector = DatasetMetricsCollector(max_values_per_feature=1000)
    sample = {
        "sample_id": "s0",
        "window": {"index": 0, "start_timestamp": "2026-08-30T18:00:00Z", "end_timestamp": "2026-08-30T18:00:01Z", "row_count": 10},
        "features": {"imu": {"statistics": {"x": 1.0}}, "gps": {"raw": {"speed": [1.0, 2.0]}}},
        "modality_mask": {"imu": True, "gps": False},
        "modality_coverage": {"imu": 1.0, "gps": 0.0},
        "metadata": {"source_row_start": 0, "source_row_end": 9},
    }
    snapshot = json.loads(json.dumps(sample))
    collector.observe_sample(0, sample)
    assert sample == snapshot


def test_bool_false_not_treated_as_numeric_zero() -> None:
    from app.qc.selectors import discover_scalar_feature_paths

    discovered = discover_scalar_feature_paths({"imu": {"is_moving": False}})
    assert discovered == {}


# ---------------------------------------------------------------------------
# Session distribution (single-session limitation)
# ---------------------------------------------------------------------------


def _upload(client: TestClient, filename: str, content: str, **fields) -> dict:
    response = client.post(
        "/api/v1/ingestion/upload", files={"file": (filename, content.encode(), None)}, data=fields
    )
    assert response.status_code == 201, response.text
    return response.json()


def _pipeline(client: TestClient, filename, content, schema_name, profile_name, source_units, **fields) -> dict:
    ingestion = _upload(client, filename, content, **fields)
    for path, body in (
        (f"/api/v1/validation/{ingestion['ingestion_id']}", {"schema_name": schema_name, "schema_version": "1.0.0"}),
        (f"/api/v1/integrity/{ingestion['ingestion_id']}", {"schema_name": schema_name, "schema_version": "1.0.0"}),
    ):
        r = client.post(path, json=body)
        assert r.status_code == 200, r.text
    r = client.post(
        f"/api/v1/normalization/{ingestion['ingestion_id']}",
        json={
            "schema_name": schema_name,
            "schema_version": "1.0.0",
            "profile_name": profile_name,
            "profile_version": "1.0.0",
            "source_units": source_units,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _transformed(client: TestClient, session_id: str) -> dict:
    imu = _pipeline(client, "imu.csv", IMU_CSV, "imu", "imu_canonical", {"acceleration": "m/s^2", "angular_velocity": "rad/s"}, session_id=session_id)
    gps = _pipeline(client, "gps.csv", GPS_CSV, "gps", "gps_canonical", {"altitude": "m", "speed": "m/s"}, session_id=session_id)
    sync = client.post(
        "/api/v1/synchronization",
        json={
            "streams": [
                {"name": "imu", "normalization_id": imu["normalization_id"]},
                {"name": "gps", "normalization_id": gps["normalization_id"]},
            ],
            "reference": {"mode": "stream", "stream": "imu"},
            "alignment": {"default_method": "nearest", "max_time_delta_ms": 400},
        },
    ).json()
    cleaned = client.post(
        f"/api/v1/cleaning/{sync['synchronization_id']}",
        json={"policy_name": "default_multimodal", "policy_version": "1.0.0", "config": {"required_streams": ["imu"]}},
    ).json()
    return client.post(
        f"/api/v1/transformation/{cleaned['cleaning_id']}",
        json={
            "profile_name": "multimodal_window_v1",
            "profile_version": "1.0.0",
            "config": {
                "window": {"mode": "count", "size": 10, "stride": 5, "drop_incomplete": True},
                "features": {"imu": {"statistics": ["mean", "std"]}, "gps": {"statistics": ["mean"]}},
            },
        },
    ).json()


def test_single_session_distribution_reported_honestly(client: TestClient) -> None:
    xform = _transformed(client, session_id="sess_single_qc")
    response = client.post(
        f"{QC_URL}/{xform['transformation_id']}",
        json={"profile_name": "default_dataset_qc", "profile_version": "1.0.0", "config": {"minimum_samples": 1}},
    )
    body = response.json()
    report = json.loads(Path(body["report_uri"].replace("file://", "")).read_text())
    assert report["session_distribution"] == {"sess_single_qc": report["summary"]["samples_checked"]}
    # A single group can never itself be "imbalanced" — the check must never
    # manufacture a GROUP_IMBALANCE finding here.
    assert not any(i["code"] == "GROUP_IMBALANCE" for i in report["issues"])


def test_group_imbalance_never_fires_for_single_group() -> None:
    from app.qc.checks.group_distribution import evaluate_group_imbalance

    issues = evaluate_group_imbalance({"only_session": 100}, QCConfig(max_group_fraction=0.5))
    assert issues == []


def test_group_imbalance_fires_for_genuine_multi_group_data() -> None:
    from app.qc.checks.group_distribution import evaluate_group_imbalance

    issues = evaluate_group_imbalance({"a": 950, "b": 50}, QCConfig(max_group_fraction=0.90))
    assert len(issues) == 1
    assert issues[0].path == "a"


# ---------------------------------------------------------------------------
# Multiple issues coexisting / no raw arrays in report
# ---------------------------------------------------------------------------


def test_failed_processing_leaves_no_committed_partial_report(client: TestClient, qc_root: Path) -> None:
    import hashlib

    xform = _transformed(client, session_id="sess_failed_qc")
    artifact_path = Path(xform["artifact_uri"].replace("file://", ""))
    corrupted = b"not-valid-json\n"
    artifact_path.write_bytes(corrupted)
    manifest_path = artifact_path.parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["transformed_sha256"] = hashlib.sha256(corrupted).hexdigest()
    manifest["transformed_size_bytes"] = len(corrupted)
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(json.JSONDecodeError):
        client.post(
            f"{QC_URL}/{xform['transformation_id']}",
            json={"profile_name": "default_dataset_qc", "profile_version": "1.0.0", "config": {}},
        )

    transformation_qc_dir = qc_root / xform["transformation_id"]
    assert not transformation_qc_dir.exists() or list(transformation_qc_dir.iterdir()) == []


def test_multiple_issues_can_coexist_and_report_excludes_raw_arrays(client: TestClient) -> None:
    xform = _transformed(client, session_id="sess_multi_issue_qc")
    request = {
        "profile_name": "default_dataset_qc",
        "profile_version": "1.0.0",
        "config": {
            "minimum_samples": 1000,  # guarantees DATASET_TOO_SMALL (error)
            "variance": {"enabled": True, "minimum_variance": 1e12, "severity": "warning"},  # guarantees warnings
        },
    }
    response = client.post(f"{QC_URL}/{xform['transformation_id']}", json=request)
    body = response.json()
    assert body["summary"]["error_count"] >= 1
    assert body["summary"]["warning_count"] >= 1
    assert body["status"] == "failed"

    report = json.loads(Path(body["report_uri"].replace("file://", "")).read_text())
    assert "raw" not in json.dumps(report["features"])  # no raw arrays ever surfaced in the QC report
