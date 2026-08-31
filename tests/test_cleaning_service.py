"""Unit tests for RowEvaluator + CleaningMetricsAccumulator: counts, reason
counts, detail truncation, and the guarantee that cleaning never modifies
sensor values or timestamps for a retained, non-redacted row.
"""

from __future__ import annotations

from app.cleaning.evaluator import RowEvaluator
from app.cleaning.metrics import CleaningMetricsAccumulator
from app.cleaning.rules.coverage import RequiredStreamsRule
from app.cleaning.rules.duplicates import DuplicateRowRule
from app.cleaning.rules.privacy import PrivacyRedactionRule


def _row(ts: str, imu_present: bool = True) -> dict:
    return {
        "timestamp": ts,
        "streams": {"imu": {"accel_x": 0.123456789, "accel_y": 9.999999} if imu_present else None},
    }


def test_input_retained_dropped_counts_correct() -> None:
    rows = [_row("2026-08-30T18:00:00Z"), _row("2026-08-30T18:00:01Z", imu_present=False), _row("2026-08-30T18:00:02Z")]
    evaluator = RowEvaluator([RequiredStreamsRule(required_streams=("imu",))])
    metrics = CleaningMetricsAccumulator(max_detail_entries=1000)

    for i, row in enumerate(rows, start=1):
        cleaned, drop_reasons, redactions = evaluator.evaluate(i, row)
        if drop_reasons:
            metrics.record_dropped(i, row["timestamp"], drop_reasons)
        else:
            metrics.record_kept(i, row["timestamp"], redactions)

    assert metrics.input_rows == 3
    assert metrics.retained_rows == 2
    assert metrics.dropped_rows == 1


def test_redacted_row_count_correct() -> None:
    rows = [_row("2026-08-30T18:00:00Z"), _row("2026-08-30T18:00:01Z")]
    evaluator = RowEvaluator([PrivacyRedactionRule(fields=("streams.imu.accel_x",))])
    metrics = CleaningMetricsAccumulator(max_detail_entries=1000)

    for i, row in enumerate(rows, start=1):
        cleaned, drop_reasons, redactions = evaluator.evaluate(i, row)
        metrics.record_kept(i, row["timestamp"], redactions)

    assert metrics.redacted_rows == 2
    assert metrics.retained_rows == 2


def test_reason_counts_correct() -> None:
    rows = [_row(f"2026-08-30T18:00:{i:02d}Z", imu_present=(i % 2 == 0)) for i in range(4)]
    evaluator = RowEvaluator([RequiredStreamsRule(required_streams=("imu",))])
    metrics = CleaningMetricsAccumulator(max_detail_entries=1000)

    for i, row in enumerate(rows, start=1):
        cleaned, drop_reasons, redactions = evaluator.evaluate(i, row)
        if drop_reasons:
            metrics.record_dropped(i, row["timestamp"], drop_reasons)
        else:
            metrics.record_kept(i, row["timestamp"], redactions)

    assert metrics.reason_counts == {"MISSING_REQUIRED_STREAM": 2}


def test_retention_ratio_correct() -> None:
    metrics = CleaningMetricsAccumulator(max_detail_entries=1000)
    for i in range(10):
        if i < 7:
            metrics.record_kept(i, "t", [])
        else:
            metrics.record_dropped(i, "t", [])
    assert metrics.retention_ratio == 0.7


def test_retention_ratio_zero_when_no_input() -> None:
    metrics = CleaningMetricsAccumulator(max_detail_entries=1000)
    assert metrics.retention_ratio == 0.0


def test_issue_detail_truncation_works() -> None:
    metrics = CleaningMetricsAccumulator(max_detail_entries=2)
    from app.cleaning.rules.base import DropReason

    for i in range(5):
        metrics.record_dropped(i, "t", [DropReason(code="MISSING_REQUIRED_STREAM", stream="imu")])

    assert len(metrics.dropped_examples) == 2
    assert metrics.details_truncated is True


def test_counts_continue_after_detail_truncation() -> None:
    metrics = CleaningMetricsAccumulator(max_detail_entries=2)
    from app.cleaning.rules.base import DropReason

    for i in range(5):
        metrics.record_dropped(i, "t", [DropReason(code="MISSING_REQUIRED_STREAM", stream="imu")])

    assert metrics.dropped_rows == 5  # count keeps going past the detail cap
    assert metrics.reason_counts["MISSING_REQUIRED_STREAM"] == 5


def test_redaction_detail_truncation_independent_of_dropped() -> None:
    metrics = CleaningMetricsAccumulator(max_detail_entries=1)
    from app.cleaning.rules.base import RedactionRecord

    for i in range(3):
        metrics.record_kept(i, "t", [RedactionRecord(code="FIELD_REDACTED", field="streams.gps.latitude")])

    assert len(metrics.redaction_examples) == 1
    assert metrics.redacted_rows == 3
    assert metrics.details_truncated is True


def test_sensor_numerical_values_never_modified_by_cleaning() -> None:
    row = _row("2026-08-30T18:00:00Z")
    evaluator = RowEvaluator([])  # no rules at all — nothing should change
    cleaned, drop_reasons, redactions = evaluator.evaluate(1, row)
    assert cleaned["streams"]["imu"]["accel_x"] == 0.123456789
    assert cleaned["streams"]["imu"]["accel_y"] == 9.999999


def test_timestamps_never_modified_by_cleaning() -> None:
    row = _row("2026-08-30T18:00:00.123456Z")
    evaluator = RowEvaluator([PrivacyRedactionRule(fields=("streams.imu.accel_x",))])
    cleaned, drop_reasons, redactions = evaluator.evaluate(1, row)
    assert cleaned["timestamp"] == "2026-08-30T18:00:00.123456Z"


def test_non_null_optional_payload_preserved_exactly_when_not_redacted() -> None:
    row = _row("2026-08-30T18:00:00Z")
    row["streams"]["gps"] = {"latitude": 34.0205, "longitude": -118.2856, "device_id": "gps_01"}
    evaluator = RowEvaluator([RequiredStreamsRule(required_streams=("imu",))])
    cleaned, _, _ = evaluator.evaluate(1, row)
    assert cleaned["streams"]["gps"] == {"latitude": 34.0205, "longitude": -118.2856, "device_id": "gps_01"}


def test_report_reason_counts_match_actual_decisions() -> None:
    rows = [
        _row("2026-08-30T18:00:00Z"),
        _row("2026-08-30T18:00:01Z", imu_present=False),
        _row("2026-08-30T18:00:02Z"),
        _row("2026-08-30T18:00:00Z"),  # exact duplicate of row 1
    ]
    rules = [RequiredStreamsRule(required_streams=("imu",)), DuplicateRowRule()]
    evaluator = RowEvaluator(rules)
    metrics = CleaningMetricsAccumulator(max_detail_entries=1000)

    for i, row in enumerate(rows, start=1):
        cleaned, drop_reasons, redactions = evaluator.evaluate(i, row)
        if drop_reasons:
            metrics.record_dropped(i, row["timestamp"], drop_reasons)
        else:
            metrics.record_kept(i, row["timestamp"], redactions)

    assert metrics.reason_counts == {"MISSING_REQUIRED_STREAM": 1, "DUPLICATE_ROW": 1}
    assert metrics.dropped_rows == 2
    assert metrics.retained_rows == 2
