"""Unit tests for scalar feature discovery (app.qc.selectors) and the
feature completeness check (app.qc.checks.feature_completeness)."""

from __future__ import annotations

from app.qc.checks.feature_completeness import FeatureCompletenessCheck
from app.qc.metrics import DatasetMetricsCollector
from app.qc.models import FeatureCompletenessConfig, FeatureThresholdOverride, QCConfig, QCErrorCode
from app.qc.selectors import discover_scalar_feature_paths

CHECK = FeatureCompletenessCheck()


def _sample(index: int, features: dict) -> dict:
    return {
        "sample_id": f"s{index}",
        "window": {"index": index, "start_timestamp": f"2026-08-30T18:00:{index:02d}Z", "end_timestamp": f"2026-08-30T18:00:{index+1:02d}Z", "row_count": 10},
        "features": features,
        "metadata": {},
    }


# ---------------------------------------------------------------------------
# Feature discovery
# ---------------------------------------------------------------------------


def test_scalar_numeric_feature_discovered() -> None:
    discovered = discover_scalar_feature_paths({"imu": {"statistics": {"accel_x_mean": 0.5}}})
    assert discovered == {"features.imu.statistics.accel_x_mean": 0.5}


def test_raw_arrays_ignored() -> None:
    discovered = discover_scalar_feature_paths({"imu": {"raw": {"accel_x": [0.1, 0.2, 0.3]}}})
    assert discovered == {}


def test_strings_ignored() -> None:
    discovered = discover_scalar_feature_paths({"imu": {"label": "moving"}})
    assert discovered == {}


def test_bools_ignored_as_numeric() -> None:
    discovered = discover_scalar_feature_paths({"imu": {"is_valid": True, "is_bad": False}})
    assert discovered == {}


def test_negative_numeric_values_discovered() -> None:
    discovered = discover_scalar_feature_paths({"imu": {"statistics": {"accel_x_min": -3.5}}})
    assert discovered["features.imu.statistics.accel_x_min"] == -3.5


def test_zero_is_a_present_value_not_missing() -> None:
    discovered = discover_scalar_feature_paths({"gps": {"statistics": {"speed_mean": 0.0}}})
    assert discovered["features.gps.statistics.speed_mean"] == 0.0


def test_explicit_null_discovered_distinctly() -> None:
    discovered = discover_scalar_feature_paths({"gps": {"statistics": {"speed_mean": None}}})
    assert "features.gps.statistics.speed_mean" in discovered
    assert discovered["features.gps.statistics.speed_mean"] is None


def test_high_precision_values_not_rounded() -> None:
    discovered = discover_scalar_feature_paths({"imu": {"statistics": {"x": 0.123456789012345}}})
    assert discovered["features.imu.statistics.x"] == 0.123456789012345


# ---------------------------------------------------------------------------
# Feature union across samples / completeness ratio
# ---------------------------------------------------------------------------


def test_feature_union_across_all_samples() -> None:
    """A feature appearing only from sample 5 onward must count as missing
    for samples 0-4 — completeness is never defined from the first sample
    alone."""
    collector = DatasetMetricsCollector(max_values_per_feature=1000)
    for i in range(10):
        features = {"imu": {"statistics": {"accel_x_mean": float(i)}}}
        if i >= 5:
            features["gps"] = {"statistics": {"speed_mean": 1.0}}
        collector.observe_sample(i, _sample(i, features))

    gps_accumulator = collector.metrics.feature_accumulators["features.gps.statistics.speed_mean"]
    assert gps_accumulator.missing_count == 5
    assert gps_accumulator.present_count == 5


def test_completeness_ratio_correct() -> None:
    collector = DatasetMetricsCollector(max_values_per_feature=1000)
    for i in range(10):
        features = {"gps": {"statistics": {"speed_mean": 1.0}}} if i < 8 else {}
        collector.observe_sample(i, _sample(i, features))
    accumulator = collector.metrics.feature_accumulators["features.gps.statistics.speed_mean"]
    completeness_ratio = accumulator.present_count / collector.metrics.sample_count
    assert completeness_ratio == 0.8


def test_missing_feature_counted() -> None:
    collector = DatasetMetricsCollector(max_values_per_feature=1000)
    collector.observe_sample(0, _sample(0, {"imu": {"statistics": {"accel_x_mean": 1.0}}}))
    collector.observe_sample(1, _sample(1, {}))
    accumulator = collector.metrics.feature_accumulators["features.imu.statistics.accel_x_mean"]
    assert accumulator.missing_count == 1
    assert accumulator.present_count == 1


def test_null_feature_counted_separately_from_missing() -> None:
    collector = DatasetMetricsCollector(max_values_per_feature=1000)
    collector.observe_sample(0, _sample(0, {"gps": {"statistics": {"speed_mean": 1.0}}}))
    collector.observe_sample(1, _sample(1, {"gps": {"statistics": {"speed_mean": None}}}))
    accumulator = collector.metrics.feature_accumulators["features.gps.statistics.speed_mean"]
    assert accumulator.present_count == 1
    assert accumulator.null_count == 1
    assert accumulator.missing_count == 0


def test_numeric_zero_counted_as_present() -> None:
    collector = DatasetMetricsCollector(max_values_per_feature=1000)
    collector.observe_sample(0, _sample(0, {"gps": {"statistics": {"speed_mean": 0.0}}}))
    accumulator = collector.metrics.feature_accumulators["features.gps.statistics.speed_mean"]
    assert accumulator.present_count == 1
    assert accumulator.null_count == 0


def test_low_completeness_detected() -> None:
    collector = DatasetMetricsCollector(max_values_per_feature=1000)
    for i in range(10):
        features = {"gps": {"statistics": {"speed_mean": 1.0}}} if i < 5 else {}
        collector.observe_sample(i, _sample(i, features))
    config = QCConfig(feature_completeness=FeatureCompletenessConfig(maximum_missing_ratio=0.1))
    issues = CHECK.evaluate(collector.metrics, config)
    assert len(issues) == 1
    assert issues[0].code == QCErrorCode.LOW_FEATURE_COMPLETENESS.value
    assert issues[0].path == "features.gps.statistics.speed_mean"
    assert issues[0].observed == 0.5


def test_per_feature_override_threshold() -> None:
    collector = DatasetMetricsCollector(max_values_per_feature=1000)
    for i in range(10):
        features = {"gps": {"statistics": {"speed_mean": 1.0}}} if i < 5 else {}
        collector.observe_sample(i, _sample(i, features))
    config = QCConfig(
        feature_completeness=FeatureCompletenessConfig(
            maximum_missing_ratio=0.9,
            per_feature={"features.gps.statistics.speed_mean": FeatureThresholdOverride(maximum_missing_ratio=0.1)},
        )
    )
    issues = CHECK.evaluate(collector.metrics, config)
    assert len(issues) == 1


def test_no_feature_completeness_config_means_no_checks() -> None:
    collector = DatasetMetricsCollector(max_values_per_feature=1000)
    collector.observe_sample(0, _sample(0, {}))
    assert CHECK.evaluate(collector.metrics, QCConfig()) == []
