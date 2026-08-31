"""Post-assignment leakage verification — an explicit, separate pass that
audits the final sample -> group -> split assignment for the invariants
Step 9 exists to guarantee. This is a deliberate "trust but verify" step:
grouping/splitting should make violations structurally impossible, but
this pass re-derives the checks independently rather than assuming the
upstream logic is bug-free. Any violation here means an internal engine
bug, not a request or data problem — the package is never committed in
that case.
"""

from __future__ import annotations

from dataclasses import dataclass


class LeakageInvariantViolation(Exception):
    pass


class SampleCountMismatch(Exception):
    pass


@dataclass(frozen=True)
class LeakageCheckResult:
    duplicate_sample_ids: int
    cross_split_groups: int
    cross_split_overlaps: int
    passed: bool


def _count_cross_split_overlaps(ranges_and_splits: list[tuple[int, int, str]]) -> int:
    """Independently re-derives connected overlap-chains directly from
    (start, end) ranges — NOT from the group_id column — and counts how
    many such chains contain more than one distinct split. For correct
    source-overlap grouping this is always 0; a non-zero result means the
    grouping algorithm itself has a bug, not that the data is unusual.
    Only meaningful for grouping.mode='source_overlap'; callers pass an
    empty list otherwise."""
    violations = 0
    current_max_end: int | None = None
    current_splits: set[str] = set()
    for start, end, split in ranges_and_splits:
        if current_max_end is not None and start <= current_max_end:
            current_splits.add(split)
            current_max_end = max(current_max_end, end)
        else:
            if len(current_splits) > 1:
                violations += 1
            current_splits = {split}
            current_max_end = end
    if len(current_splits) > 1:
        violations += 1
    return violations


def run_leakage_checks(
    *,
    assignments: list[tuple[str, str, str]],
    source_sample_count: int,
    overlap_ranges_and_splits: list[tuple[int, int, str]] | None = None,
) -> LeakageCheckResult:
    """`assignments` is (sample_id, group_id, split) per sample, in any
    order. `overlap_ranges_and_splits` — (source_row_start, source_row_end,
    split), sorted by start — is supplied only for grouping.mode=
    'source_overlap'; pass None/empty for other modes.

    Raises LeakageInvariantViolation if any invariant fails; a caller must
    never commit a package when this raises.
    """
    seen_sample_ids: set[str] = set()
    duplicate_sample_ids = 0
    group_splits: dict[str, set[str]] = {}

    for sample_id, group_id, split in assignments:
        if sample_id in seen_sample_ids:
            duplicate_sample_ids += 1
        else:
            seen_sample_ids.add(sample_id)
        group_splits.setdefault(group_id, set()).add(split)

    cross_split_groups = sum(1 for splits in group_splits.values() if len(splits) > 1)
    cross_split_overlaps = (
        _count_cross_split_overlaps(overlap_ranges_and_splits) if overlap_ranges_and_splits else 0
    )

    sample_count_ok = len(assignments) == source_sample_count
    passed = duplicate_sample_ids == 0 and cross_split_groups == 0 and cross_split_overlaps == 0 and sample_count_ok

    result = LeakageCheckResult(
        duplicate_sample_ids=duplicate_sample_ids,
        cross_split_groups=cross_split_groups,
        cross_split_overlaps=cross_split_overlaps,
        passed=passed,
    )

    if not sample_count_ok:
        raise SampleCountMismatch(
            f"Packaged {len(assignments)} samples but source transformed dataset has "
            f"{source_sample_count} — refusing to commit."
        )
    if not result.passed:
        raise LeakageInvariantViolation(f"Leakage invariant violated: {result}")
    return result
