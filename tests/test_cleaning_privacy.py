"""Unit tests for explicit field redaction."""

from __future__ import annotations

import copy

from app.cleaning.rules.base import RuleContext
from app.cleaning.rules.common import apply_redactions, is_valid_field_path, path_exists
from app.cleaning.rules.privacy import PrivacyRedactionRule


def _row() -> dict:
    return {
        "timestamp": "2026-08-30T18:00:00Z",
        "streams": {
            "gps": {"latitude": 34.0205, "longitude": -118.2856, "altitude": 30.48, "device_id": "gps_01"},
            "imu": {"accel_x": 0.1},
        },
    }


def test_privacy_redaction_works() -> None:
    rule = PrivacyRedactionRule(fields=("streams.gps.latitude",))
    outcome = rule.evaluate(_row(), context=RuleContext(row_index=1))
    assert len(outcome.redactions) == 1
    assert outcome.redactions[0].code == "FIELD_REDACTED"
    assert outcome.redactions[0].field == "streams.gps.latitude"


def test_nested_latitude_redaction_works() -> None:
    row = _row()
    cleaned = apply_redactions(row, ["streams.gps.latitude"])
    assert cleaned["streams"]["gps"]["latitude"] is None
    assert cleaned["streams"]["gps"]["longitude"] == -118.2856  # untouched


def test_nested_longitude_redaction_works() -> None:
    row = _row()
    cleaned = apply_redactions(row, ["streams.gps.longitude"])
    assert cleaned["streams"]["gps"]["longitude"] is None
    assert cleaned["streams"]["gps"]["latitude"] == 34.0205  # untouched


def test_both_lat_and_lon_redacted_together() -> None:
    row = _row()
    cleaned = apply_redactions(row, ["streams.gps.latitude", "streams.gps.longitude"])
    assert cleaned["streams"]["gps"]["latitude"] is None
    assert cleaned["streams"]["gps"]["longitude"] is None
    assert cleaned["streams"]["gps"]["altitude"] == 30.48


def test_missing_optional_redaction_path_is_ignored_not_an_error() -> None:
    row = _row()
    row["streams"]["gps"] = None  # gps didn't match this row at all
    rule = PrivacyRedactionRule(fields=("streams.gps.latitude",))
    outcome = rule.evaluate(row, context=RuleContext(row_index=1))
    assert outcome.redactions == []  # silently skipped, not an error


def test_path_exists_helper() -> None:
    row = _row()
    assert path_exists(row, "streams.gps.latitude") is True
    assert path_exists(row, "streams.gps.nonexistent") is False
    assert path_exists(row, "streams.camera.image_path") is False


def test_invalid_redaction_configuration_rejected() -> None:
    assert is_valid_field_path("") is False
    assert is_valid_field_path(".") is False
    assert is_valid_field_path("streams.") is False
    assert is_valid_field_path(".gps") is False
    assert is_valid_field_path("streams..latitude") is False


def test_valid_redaction_paths_accepted() -> None:
    assert is_valid_field_path("streams.gps.latitude") is True
    assert is_valid_field_path("streams.camera.image_path") is True
    assert is_valid_field_path("timestamp") is True


def test_dropped_rows_are_not_additionally_redacted() -> None:
    """Simulated via the evaluator: a row that fails a required-stream check
    must never reach the privacy rule at all."""
    from app.cleaning.evaluator import RowEvaluator
    from app.cleaning.rules.coverage import RequiredStreamsRule

    row = _row()
    row["streams"]["imu"] = None  # missing required stream

    rules = [RequiredStreamsRule(required_streams=("imu",)), PrivacyRedactionRule(fields=("streams.gps.latitude",))]
    evaluator = RowEvaluator(rules)

    cleaned_row, drop_reasons, redactions = evaluator.evaluate(1, row)

    assert cleaned_row is None
    assert drop_reasons and drop_reasons[0].code == "MISSING_REQUIRED_STREAM"
    assert redactions == []  # privacy rule never ran


def test_duplicate_detection_happens_before_redaction() -> None:
    """Two rows identical except for the field that redaction WOULD remove
    must NOT be treated as duplicates — duplicate detection sees the
    original (unredacted) content, since it runs first."""
    from app.cleaning.evaluator import RowEvaluator
    from app.cleaning.rules.duplicates import DuplicateRowRule

    row_a = _row()
    row_b = _row()
    row_b["streams"]["gps"]["latitude"] = 99.9999  # only differs in a field that gets redacted

    rules = [DuplicateRowRule(), PrivacyRedactionRule(fields=("streams.gps.latitude",))]
    evaluator = RowEvaluator(rules)

    cleaned_a, drop_a, _ = evaluator.evaluate(1, row_a)
    cleaned_b, drop_b, _ = evaluator.evaluate(2, row_b)

    # Neither is a duplicate of the other, because their PRE-redaction
    # latitude differs — duplicate detection ran before redaction.
    assert drop_a == []
    assert drop_b == []
    # But after redaction is applied, both outputs have latitude=None.
    assert cleaned_a["streams"]["gps"]["latitude"] is None
    assert cleaned_b["streams"]["gps"]["latitude"] is None


def test_redaction_does_not_mutate_input_python_structures() -> None:
    row = _row()
    original_snapshot = copy.deepcopy(row)

    apply_redactions(row, ["streams.gps.latitude", "streams.gps.longitude"])

    assert row == original_snapshot  # the input dict itself is untouched


def test_apply_redactions_returns_same_object_when_no_fields() -> None:
    row = _row()
    result = apply_redactions(row, [])
    assert result is row  # no-op short circuit, no needless copy
