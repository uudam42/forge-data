"""Per-target-timestamp alignment across every participating stream —
builds the "streams" and "alignment" payloads for one synchronized row.

This is the one place that knows how to turn (target timestamp, N stream
cursors/strategies) into a row; app.synchronization.service only wires up
the StreamRuntime objects and drives the target-timestamp loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

from app.synchronization.strategies.base import AlignmentContext, AlignmentStrategy, StreamCursor
from app.validation.schemas.base import SchemaDefinition

_TIMESTAMP_FIELD = "timestamp"


@dataclass
class StreamRuntime:
    """Everything needed to align one stream against the output timeline."""

    name: str
    schema: SchemaDefinition
    strategy: AlignmentStrategy
    cursor: StreamCursor
    tolerance_us: int
    is_reference: bool = False


def build_row(
    *, target_epoch_us: int, reference_record: dict | None, streams: list[StreamRuntime]
) -> tuple[dict[str, dict | None], dict[str, dict]]:
    """Returns (streams_payload, alignment_payload) for one target timestamp.

    `reference_record` is the reference stream's own record for this exact
    target when running in "stream" reference mode (already known — no
    cursor lookup needed, delta is trivially 0); pass None in "fixed_rate"
    mode, where every stream (including the one that would otherwise be
    the reference) is aligned via its own cursor/strategy like any other.
    """
    streams_payload: dict[str, dict | None] = {}
    alignment_payload: dict[str, dict] = {}

    for stream in streams:
        if stream.is_reference and reference_record is not None:
            streams_payload[stream.name] = reference_record
            alignment_payload[stream.name] = {"matched": True, "method": "reference", "delta_ms": 0.0}
            continue

        stream.cursor.advance_to(target_epoch_us)
        context = AlignmentContext(
            target_epoch_us=target_epoch_us,
            tolerance_us=stream.tolerance_us,
            schema=stream.schema,
            timestamp_field=_TIMESTAMP_FIELD,
        )
        record, outcome = stream.strategy.align(stream.cursor, context)

        streams_payload[stream.name] = record
        result: dict[str, object] = {"matched": outcome.matched, "method": outcome.method}
        if outcome.matched:
            result["delta_ms"] = outcome.delta_ms
        else:
            result["reason"] = outcome.reason
        alignment_payload[stream.name] = result

    return streams_payload, alignment_payload


def iter_rows(
    *, targets: Iterator[tuple[int, dict | None]], streams: list[StreamRuntime]
) -> Iterator[tuple[int, dict[str, dict | None], dict[str, dict]]]:
    """Drives the full target-timestamp loop, yielding
    (target_epoch_us, streams_payload, alignment_payload) triples.

    `targets` yields (target_epoch_us, reference_record_or_None) — see
    build_row() for what reference_record means in each reference mode.
    """
    for target_epoch_us, reference_record in targets:
        streams_payload, alignment_payload = build_row(
            target_epoch_us=target_epoch_us, reference_record=reference_record, streams=streams
        )
        yield target_epoch_us, streams_payload, alignment_payload
