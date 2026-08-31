"""Unit tests for exact duplicate-row detection."""

from __future__ import annotations

from app.cleaning.rules.base import RuleContext
from app.cleaning.rules.duplicates import DuplicateRowRule, canonical_row_key


def _row(timestamp: str, **streams) -> dict:
    return {"timestamp": timestamp, "streams": streams, "alignment": {"imu": {"matched": True}}}


def test_exact_duplicate_row_detected() -> None:
    rule = DuplicateRowRule()
    row = _row("2026-08-30T18:00:00Z", imu={"accel_x": 0.1})

    outcome1 = rule.evaluate(row, context=RuleContext(row_index=1))
    outcome2 = rule.evaluate(row, context=RuleContext(row_index=2))

    assert outcome1.should_drop is False
    assert outcome2.should_drop is True
    assert outcome2.drop_reasons[0].code == "DUPLICATE_ROW"


def test_first_duplicate_retained_later_dropped() -> None:
    rule = DuplicateRowRule()
    row_a = _row("2026-08-30T18:00:00Z", imu={"accel_x": 0.1})
    row_b = dict(row_a)  # a distinct dict, same content

    first = rule.evaluate(row_a, context=RuleContext(row_index=1))
    second = rule.evaluate(row_b, context=RuleContext(row_index=2))

    assert first.should_drop is False
    assert second.should_drop is True
    assert second.drop_reasons[0].duplicate_of_row_index == 1


def test_equal_values_different_timestamps_are_not_duplicates() -> None:
    rule = DuplicateRowRule()
    row_a = _row("2026-08-30T18:00:00Z", imu={"accel_x": 0.1})
    row_b = _row("2026-08-30T18:00:01Z", imu={"accel_x": 0.1})  # same sensor values, different time

    outcome_a = rule.evaluate(row_a, context=RuleContext(row_index=1))
    outcome_b = rule.evaluate(row_b, context=RuleContext(row_index=2))

    assert outcome_a.should_drop is False
    assert outcome_b.should_drop is False


def test_different_streams_content_same_timestamp_not_duplicates() -> None:
    rule = DuplicateRowRule()
    row_a = _row("2026-08-30T18:00:00Z", imu={"accel_x": 0.1})
    row_b = _row("2026-08-30T18:00:00Z", imu={"accel_x": 0.2})

    outcome_a = rule.evaluate(row_a, context=RuleContext(row_index=1))
    outcome_b = rule.evaluate(row_b, context=RuleContext(row_index=2))

    assert outcome_a.should_drop is False
    assert outcome_b.should_drop is False


def test_duplicate_hash_deterministic() -> None:
    row = _row("2026-08-30T18:00:00Z", imu={"accel_x": 0.1, "accel_y": 0.2})
    key1 = canonical_row_key(row)
    key2 = canonical_row_key(dict(row))
    assert key1 == key2
    assert len(key1) == 64  # sha256 hex digest
    int(key1, 16)  # valid hex


def test_duplicate_hash_ignores_alignment_block() -> None:
    """Two rows with identical timestamp+streams but different alignment
    diagnostics are still the same observation — alignment is provenance,
    not content identity."""
    row_a = {
        "timestamp": "2026-08-30T18:00:00Z",
        "streams": {"imu": {"accel_x": 0.1}},
        "alignment": {"imu": {"matched": True, "method": "reference", "delta_ms": 0.0}},
    }
    row_b = {
        "timestamp": "2026-08-30T18:00:00Z",
        "streams": {"imu": {"accel_x": 0.1}},
        "alignment": {"imu": {"matched": True, "method": "nearest", "delta_ms": 12.3}},
    }
    assert canonical_row_key(row_a) == canonical_row_key(row_b)


def test_duplicate_hash_does_not_use_python_builtin_hash() -> None:
    """hash() is not stable across processes/runs by default (PYTHONHASHSEED
    randomization for strings) — canonical_row_key must not rely on it."""
    row = _row("2026-08-30T18:00:00Z", imu={"accel_x": 0.1})
    key = canonical_row_key(row)
    assert key != str(hash(str(row)))
    assert isinstance(key, str) and all(c in "0123456789abcdef" for c in key)


def test_duplicate_detection_stable_across_runs() -> None:
    rows = [
        _row("2026-08-30T18:00:00Z", imu={"accel_x": 0.1}),
        _row("2026-08-30T18:00:01Z", imu={"accel_x": 0.2}),
        _row("2026-08-30T18:00:00Z", imu={"accel_x": 0.1}),  # duplicate of row 1
    ]

    def run() -> list[bool]:
        rule = DuplicateRowRule()
        return [
            rule.evaluate(row, context=RuleContext(row_index=i)).should_drop
            for i, row in enumerate(rows, start=1)
        ]

    assert run() == run() == [False, False, True]
