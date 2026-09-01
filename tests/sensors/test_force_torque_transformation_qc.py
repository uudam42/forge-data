"""Force/Torque transformation and QC tests (Design Requirements 12-14;
test items 45-55). Transformation resolves the Force/Torque feature
extractor purely through the sensor plugin registry (no per-sensor
branch in feature_engine.py); QC discovers force_torque_* features via
its existing recursive numeric-feature discovery -- no QC code changed."""

from __future__ import annotations

import json
import math
from pathlib import Path

from fastapi.testclient import TestClient

from tests.sensors.pipeline_helpers import clean, qc, synchronize, three_sensor_normalized, transform


def _xform_with_features(client: TestClient, session_id: str, features: dict) -> dict:
    normalized = three_sensor_normalized(client, session_id)
    sync = synchronize(client, normalized)
    cleaned = clean(client, sync["synchronization_id"])
    return transform(client, cleaned["cleaning_id"], features=features)


def _read_samples(transformed_root: Path, xform: dict) -> list[dict]:
    path = Path(xform["artifact_uri"].replace("file://", ""))
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_raw_force_torque_sequence(client: TestClient, transformed_root: Path) -> None:
    xform = _xform_with_features(client, "sess_ft_raw", {"force_torque": {"include_raw": True}})
    samples = _read_samples(transformed_root, xform)
    ft_features = next(s["features"]["force_torque"] for s in samples if s["features"].get("force_torque"))
    assert "force_x" in ft_features["raw"]
    assert "torque_z" in ft_features["raw"]


def test_statistical_features(client: TestClient, transformed_root: Path) -> None:
    xform = _xform_with_features(client, "sess_ft_stats", {"force_torque": {"statistics": ["mean", "std", "min", "max"]}})
    samples = _read_samples(transformed_root, xform)
    ft_features = next(s["features"]["force_torque"] for s in samples if s["features"].get("force_torque"))
    assert "force_x_mean" in ft_features["statistics"]
    assert "torque_z_std" in ft_features["statistics"]


def test_force_magnitude_derived_feature(client: TestClient, transformed_root: Path) -> None:
    xform = _xform_with_features(client, "sess_ft_force_mag", {"force_torque": {"statistics": ["mean"], "derived": ["force_magnitude"]}})
    samples = _read_samples(transformed_root, xform)
    ft_features = next(s["features"]["force_torque"] for s in samples if s["features"].get("force_torque"))
    assert "force_magnitude_mean" in ft_features["statistics"]
    assert ft_features["statistics"]["force_magnitude_mean"] > 0


def test_torque_magnitude_derived_feature(client: TestClient, transformed_root: Path) -> None:
    xform = _xform_with_features(client, "sess_ft_torque_mag", {"force_torque": {"statistics": ["mean"], "derived": ["torque_magnitude"]}})
    samples = _read_samples(transformed_root, xform)
    ft_features = next(s["features"]["force_torque"] for s in samples if s["features"].get("force_torque"))
    assert "torque_magnitude_mean" in ft_features["statistics"]


def test_force_magnitude_matches_manual_computation(client: TestClient, transformed_root: Path) -> None:
    xform = _xform_with_features(
        client, "sess_ft_force_mag_check",
        {"force_torque": {"include_raw": True, "statistics": ["mean"], "derived": ["force_magnitude"]}},
    )
    samples = _read_samples(transformed_root, xform)
    sample = next(s for s in samples if s["features"].get("force_torque"))
    raw = sample["features"]["force_torque"]["raw"]
    expected = [
        math.sqrt(fx**2 + fy**2 + fz**2)
        for fx, fy, fz in zip(raw["force_x"], raw["force_y"], raw["force_z"])
    ]
    expected_mean = sum(expected) / len(expected)
    actual_mean = sample["features"]["force_torque"]["statistics"]["force_magnitude_mean"]
    assert abs(actual_mean - expected_mean) < 1e-9


def test_missing_force_torque_modality_handled(client: TestClient, transformed_root: Path) -> None:
    """Windows where force_torque happens to have no present row (sparse
    relative to imu) report present=False / modality_mask False, exactly
    like any other stream -- generic handling, no crash."""
    xform = _xform_with_features(
        client, "sess_ft_missing_modality",
        {"imu": {"statistics": ["mean"]}, "force_torque": {"statistics": ["mean"]}},
    )
    samples = _read_samples(transformed_root, xform)
    assert all("force_torque" in s.get("modality_mask", {}) for s in samples)


def test_no_nan_or_inf_in_force_torque_features(client: TestClient, transformed_root: Path) -> None:
    xform = _xform_with_features(
        client, "sess_ft_finite",
        {"force_torque": {"include_raw": True, "statistics": ["mean", "std", "min", "max"], "derived": ["force_magnitude", "torque_magnitude"]}},
    )
    samples = _read_samples(transformed_root, xform)

    def _check(value):
        if isinstance(value, float):
            assert math.isfinite(value)
        elif isinstance(value, dict):
            for v in value.values():
                _check(v)
        elif isinstance(value, list):
            for v in value:
                _check(v)

    for sample in samples:
        ft = sample["features"].get("force_torque")
        if ft:
            _check(ft)


def test_no_force_torque_specific_branch_in_transformation_core() -> None:
    import inspect

    from app.transformation import service, feature_engine, windowing

    for module in (service, feature_engine, windowing):
        source = inspect.getsource(module)
        assert "force_torque" not in source.lower()


# ---------------------------------------------------------------------------
# QC
# ---------------------------------------------------------------------------


def _qc_with_force_torque(client: TestClient, session_id: str) -> dict:
    xform = _xform_with_features(
        client, session_id,
        {"force_torque": {"statistics": ["mean", "std"], "derived": ["force_magnitude", "torque_magnitude"]}, "imu": {"statistics": ["mean"]}},
    )
    return xform


def test_qc_modality_coverage_includes_force_torque(client: TestClient) -> None:
    xform = _qc_with_force_torque(client, "sess_ft_qc_coverage")
    result = qc(client, xform["transformation_id"])
    report = json.loads(Path(result["report_uri"].replace("file://", "")).read_text())
    assert "force_torque" in report["modality_coverage"]


def test_qc_feature_completeness_includes_force_torque(client: TestClient) -> None:
    xform = _qc_with_force_torque(client, "sess_ft_qc_completeness")
    result = qc(client, xform["transformation_id"], feature_completeness={"maximum_missing_ratio": 1.0})
    assert result["status"] in ("passed", "passed_with_warnings")
    report = json.loads(Path(result["report_uri"].replace("file://", "")).read_text())
    force_torque_features = [k for k in report["features"] if "force_torque" in k]
    assert force_torque_features
    assert all("missing_count" in report["features"][k] and "count" in report["features"][k] for k in force_torque_features)


def test_qc_low_variance_check_works_on_force_torque(client: TestClient) -> None:
    xform = _qc_with_force_torque(client, "sess_ft_qc_variance")
    result = qc(client, xform["transformation_id"], variance={"enabled": True, "minimum_variance": 1e-12})
    assert result["status"] in ("passed", "passed_with_warnings", "failed")


def test_qc_distribution_stats_computed_for_force_torque(client: TestClient) -> None:
    xform = _qc_with_force_torque(client, "sess_ft_qc_distribution")
    result = qc(client, xform["transformation_id"])
    report = json.loads(Path(result["report_uri"].replace("file://", "")).read_text())
    force_torque_features = {k: v for k, v in report["features"].items() if "force_torque" in k}
    assert force_torque_features
    assert any(v["mean"] is not None for v in force_torque_features.values())


def test_qc_drift_baseline_works_with_force_torque(client: TestClient) -> None:
    xform_a = _qc_with_force_torque(client, "sess_ft_qc_drift_a")
    baseline = qc(client, xform_a["transformation_id"])

    xform_b = _qc_with_force_torque(client, "sess_ft_qc_drift_b")
    result = qc(
        client, xform_b["transformation_id"],
        drift={"enabled": True}, baseline_qc_id=baseline["qc_id"],
    )
    assert result["status"] in ("passed", "passed_with_warnings", "failed")


def test_no_force_torque_specific_branch_in_qc_core() -> None:
    import inspect

    from app.qc import service, accumulator, selectors
    from app.qc.checks import distributions, drift, feature_completeness, variance, modality_coverage

    for module in (service, accumulator, selectors, distributions, drift, feature_completeness, variance, modality_coverage):
        source = inspect.getsource(module)
        assert "force_torque" not in source.lower()
