"""Unit tests for the post-assignment leakage verification pass
(app.packaging.leakage)."""

from __future__ import annotations

import pytest

from app.packaging.leakage import LeakageInvariantViolation, SampleCountMismatch, run_leakage_checks


def test_valid_assignment_passes() -> None:
    assignments = [("s0", "g0", "train"), ("s1", "g0", "train"), ("s2", "g1", "validation")]
    result = run_leakage_checks(assignments=assignments, source_sample_count=3)
    assert result.passed is True
    assert result.duplicate_sample_ids == 0
    assert result.cross_split_groups == 0


def test_every_sample_appears_exactly_once() -> None:
    assignments = [("s0", "g0", "train"), ("s1", "g1", "test")]
    result = run_leakage_checks(assignments=assignments, source_sample_count=2)
    assert result.passed is True


def test_sample_count_mismatch_raises() -> None:
    assignments = [("s0", "g0", "train")]
    with pytest.raises(SampleCountMismatch):
        run_leakage_checks(assignments=assignments, source_sample_count=5)


def test_duplicate_sample_id_across_splits_raises() -> None:
    assignments = [("s0", "g0", "train"), ("s0", "g0", "validation")]
    with pytest.raises(LeakageInvariantViolation):
        run_leakage_checks(assignments=assignments, source_sample_count=2)


def test_cross_split_group_invariant_violation_caught() -> None:
    # Same group_id assigned to two different splits -- must never happen
    # via correct engine behavior, but the check must catch it if it does.
    assignments = [("s0", "g0", "train"), ("s1", "g0", "validation")]
    with pytest.raises(LeakageInvariantViolation):
        run_leakage_checks(assignments=assignments, source_sample_count=2)


def test_cross_split_overlaps_detected_independently_of_group_id() -> None:
    # Two overlapping ranges land in different splits even though (by a
    # hypothetical bug) they were tagged with different group_ids -- the
    # independent range-based check must still catch this.
    assignments = [("s0", "g0", "train"), ("s1", "g1", "validation")]
    overlap_ranges = [(0, 19, "train"), (10, 29, "validation")]
    with pytest.raises(LeakageInvariantViolation):
        run_leakage_checks(
            assignments=assignments, source_sample_count=2, overlap_ranges_and_splits=overlap_ranges
        )


def test_non_overlapping_ranges_in_different_splits_is_fine() -> None:
    assignments = [("s0", "g0", "train"), ("s1", "g1", "validation")]
    overlap_ranges = [(0, 9, "train"), (20, 29, "validation")]
    result = run_leakage_checks(
        assignments=assignments, source_sample_count=2, overlap_ranges_and_splits=overlap_ranges
    )
    assert result.passed is True
    assert result.cross_split_overlaps == 0


def test_no_overlap_ranges_supplied_defaults_to_zero() -> None:
    assignments = [("s0", "g0", "train")]
    result = run_leakage_checks(assignments=assignments, source_sample_count=1, overlap_ranges_and_splits=None)
    assert result.cross_split_overlaps == 0


def test_single_sample_single_group_passes() -> None:
    result = run_leakage_checks(assignments=[("s0", "g0", "train")], source_sample_count=1)
    assert result.passed is True
