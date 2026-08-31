"""Group assignment: every transformed sample is assigned to exactly one
leakage-prevention group before any split decision is made. All samples in
the same group always go to the same split — this is the central
leakage-prevention guarantee this whole stage exists for.

Grouping and splitting are deliberately separate abstractions (see
splitter.py) so a splitting strategy can never itself decide to break a
group apart — a splitter only ever sees whole groups.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.packaging.serialization import compute_group_id, compute_session_group_id


class GroupingError(Exception):
    pass


class MissingGroupMetadataError(GroupingError):
    pass


class UnsupportedGroupingModeError(GroupingError):
    pass


@dataclass(frozen=True)
class SampleRecord:
    """Lightweight per-sample identity extracted during pass 1 — never the
    full feature payload, only what grouping/splitting/auditing need."""

    index: int
    sample_id: str
    source_row_start: int | None
    source_row_end: int | None


def assign_source_overlap_groups(
    records: list[SampleRecord], *, transformed_sha256: str
) -> list[tuple[SampleRecord, str]]:
    """Streaming connected-component grouping over inclusive source row
    ranges. Transformed samples are assumed to arrive already ordered by
    source row range — true for every Step 7 windowing mode — which lets
    this run with only O(1) *group* state (the running maximum end row of
    the current group), never re-scanning prior samples.

    Boundary semantics: ranges are INCLUSIVE on both ends, matching Step
    7's `metadata.source_row_start`/`source_row_end`. A window ending at
    row 19 and one starting at row 20 do NOT overlap; if both include row
    19, they do.

    A, B, C with A=[0,19], B=[10,29], C=[25,44]: A overlaps B (10<=19),
    B overlaps C (25<=29) — so A, B, C all land in ONE connected group,
    even though A and C never directly overlap (transitive closure).

    Group finalization happens the moment a sample doesn't extend the
    current group (or at end of input) — the group's min/max is therefore
    always fully known before its content-derived ID is computed (see
    serialization.compute_group_id), rather than relying on an
    incrementing runtime-only counter.
    """
    results: list[tuple[SampleRecord, str]] = []
    pending: list[SampleRecord] = []
    current_min: int | None = None
    current_max: int | None = None

    def flush() -> None:
        if not pending:
            return
        group_id = compute_group_id(
            transformed_sha256=transformed_sha256, group_min_row=current_min, group_max_row=current_max
        )
        results.extend((record, group_id) for record in pending)

    for record in records:
        if record.source_row_start is None or record.source_row_end is None:
            raise MissingGroupMetadataError(
                f"Sample at index {record.index} (sample_id={record.sample_id!r}) is missing "
                f"metadata.source_row_start/source_row_end, required for grouping.mode='source_overlap'"
            )

        if current_max is not None and record.source_row_start <= current_max:
            pending.append(record)
            current_max = max(current_max, record.source_row_end)
        else:
            flush()
            pending = [record]
            current_min = record.source_row_start
            current_max = record.source_row_end

    flush()
    return results


def assign_session_groups(
    records: list[SampleRecord], *, transformed_sha256: str, session_ids: list[str]
) -> list[tuple[SampleRecord, str]]:
    """All samples from the same session belong to one group. Transformed
    samples carry no per-sample session field — only the transformation
    manifest's dataset-wide `upstream.session_ids` does — so this mode
    only works when that list has exactly one entry (the overwhelmingly
    common case: one cleaning run from one synchronized session). With
    more than one, per-sample attribution isn't possible from lineage
    alone, and this deliberately refuses rather than fabricating a
    breakdown — mirroring Step 8's identical limitation for
    session_distribution."""
    if len(session_ids) != 1:
        raise MissingGroupMetadataError(
            f"grouping.mode='session' requires exactly one distinct session_id in this "
            f"transformation's lineage; found {len(session_ids)}. Per-sample session "
            f"attribution is not available from lineage alone for multi-session runs."
        )
    group_id = compute_session_group_id(transformed_sha256=transformed_sha256, session_id=session_ids[0])
    return [(record, group_id) for record in records]
