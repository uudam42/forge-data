"""Unit tests for group assignment (app.packaging.grouping)."""

from __future__ import annotations

import pytest

from app.packaging.grouping import (
    MissingGroupMetadataError,
    SampleRecord,
    assign_session_groups,
    assign_source_overlap_groups,
)

TSHA = "abc123"


def _rec(index: int, start: int | None, end: int | None) -> SampleRecord:
    return SampleRecord(index=index, sample_id=f"sample_{index}", source_row_start=start, source_row_end=end)


def test_direct_overlap_grouped_together() -> None:
    records = [_rec(0, 0, 19), _rec(1, 10, 29)]
    grouped = assign_source_overlap_groups(records, transformed_sha256=TSHA)
    group_ids = {g for _, g in grouped}
    assert len(group_ids) == 1


def test_transitive_overlap_grouped_together() -> None:
    # A=[0,19], B=[10,29], C=[25,44]: A overlaps B, B overlaps C, but A does
    # NOT directly overlap C — all three must still land in ONE group.
    records = [_rec(0, 0, 19), _rec(1, 10, 29), _rec(2, 25, 44)]
    grouped = assign_source_overlap_groups(records, transformed_sha256=TSHA)
    group_ids = {g for _, g in grouped}
    assert len(group_ids) == 1


def test_adjacent_non_overlap_windows_separated() -> None:
    # A ends at row 19, B starts at row 20 -> no overlap (inclusive bounds).
    records = [_rec(0, 0, 19), _rec(1, 20, 39)]
    grouped = assign_source_overlap_groups(records, transformed_sha256=TSHA)
    ids = [g for _, g in grouped]
    assert ids[0] != ids[1]


def test_shared_boundary_row_counts_as_overlap() -> None:
    # Both include row 19 -> DO overlap.
    records = [_rec(0, 0, 19), _rec(1, 19, 38)]
    grouped = assign_source_overlap_groups(records, transformed_sha256=TSHA)
    ids = [g for _, g in grouped]
    assert ids[0] == ids[1]


def test_multiple_separate_groups_in_one_stream() -> None:
    # Two independent non-overlapping clusters.
    records = [_rec(0, 0, 9), _rec(1, 20, 29), _rec(2, 21, 30)]
    grouped = assign_source_overlap_groups(records, transformed_sha256=TSHA)
    ids = [g for _, g in grouped]
    assert ids[0] != ids[1]
    assert ids[1] == ids[2]  # record 1 and 2 overlap (21<=29)


def test_missing_source_row_metadata_raises() -> None:
    records = [_rec(0, None, None)]
    with pytest.raises(MissingGroupMetadataError):
        assign_source_overlap_groups(records, transformed_sha256=TSHA)


def test_partial_missing_metadata_raises() -> None:
    records = [_rec(0, 0, 9), _rec(1, None, 19)]
    with pytest.raises(MissingGroupMetadataError):
        assign_source_overlap_groups(records, transformed_sha256=TSHA)


def test_group_ids_deterministic_for_same_content() -> None:
    records = [_rec(0, 0, 19), _rec(1, 10, 29)]
    grouped1 = assign_source_overlap_groups(records, transformed_sha256=TSHA)
    grouped2 = assign_source_overlap_groups(records, transformed_sha256=TSHA)
    assert [g for _, g in grouped1] == [g for _, g in grouped2]


def test_group_id_changes_with_different_transformed_sha256() -> None:
    records = [_rec(0, 0, 19)]
    grouped1 = assign_source_overlap_groups(records, transformed_sha256="aaa")
    grouped2 = assign_source_overlap_groups(records, transformed_sha256="bbb")
    assert grouped1[0][1] != grouped2[0][1]


def test_order_preserved() -> None:
    records = [_rec(0, 0, 9), _rec(1, 20, 29), _rec(2, 30, 39)]
    grouped = assign_source_overlap_groups(records, transformed_sha256=TSHA)
    assert [r.index for r, _ in grouped] == [0, 1, 2]


def test_large_ordered_input_bounded_group_state() -> None:
    """With size=10/stride=10-equivalent non-overlapping ranges, grouping a
    large ordered input should produce one group per range and never
    accumulate unbounded 'pending' state."""
    records = [_rec(i, i * 10, i * 10 + 9) for i in range(2000)]
    grouped = assign_source_overlap_groups(records, transformed_sha256=TSHA)
    group_ids = {g for _, g in grouped}
    assert len(group_ids) == 2000  # every range is its own group, none merge


def test_large_fully_overlapping_input_collapses_to_one_group() -> None:
    records = [_rec(i, i * 5, i * 5 + 9) for i in range(500)]  # stride 5 < size 10
    grouped = assign_source_overlap_groups(records, transformed_sha256=TSHA)
    group_ids = {g for _, g in grouped}
    assert len(group_ids) == 1


# ---------------------------------------------------------------------------
# Session grouping
# ---------------------------------------------------------------------------


def test_session_grouping_single_session_one_group() -> None:
    records = [_rec(0, 0, 9), _rec(1, 20, 29)]
    grouped = assign_session_groups(records, transformed_sha256=TSHA, session_ids=["sess_a"])
    group_ids = {g for _, g in grouped}
    assert len(group_ids) == 1


def test_session_grouping_never_splits_same_session() -> None:
    records = [_rec(i, i * 10, i * 10 + 9) for i in range(20)]
    grouped = assign_session_groups(records, transformed_sha256=TSHA, session_ids=["sess_a"])
    assert len({g for _, g in grouped}) == 1
    assert len(grouped) == 20


def test_session_grouping_multi_session_raises() -> None:
    records = [_rec(0, 0, 9)]
    with pytest.raises(MissingGroupMetadataError):
        assign_session_groups(records, transformed_sha256=TSHA, session_ids=["sess_a", "sess_b"])


def test_session_grouping_zero_session_raises() -> None:
    records = [_rec(0, 0, 9)]
    with pytest.raises(MissingGroupMetadataError):
        assign_session_groups(records, transformed_sha256=TSHA, session_ids=[])


def test_session_group_id_deterministic() -> None:
    records = [_rec(0, 0, 9)]
    g1 = assign_session_groups(records, transformed_sha256=TSHA, session_ids=["sess_a"])
    g2 = assign_session_groups(records, transformed_sha256=TSHA, session_ids=["sess_a"])
    assert g1[0][1] == g2[0][1]
