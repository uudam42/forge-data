"""Explicit, deterministic clock correction.

No offset or drift is ever estimated automatically — both come only from
request configuration (`ClockCorrectionConfig`). Correction is applied only
inside a synchronization run; it never touches the normalized source
artifact.

Model (an affine transform of the clock):

    corrected_time = anchor_time + (original_time - anchor_time) * scale + offset

    scale = 1 + drift_ppm / 1_000_000

The anchor is always the stream's own first (uncorrected) timestamp — never
wall-clock execution time — so the same input always produces the same
correction regardless of when synchronization is run.

All timestamps are integer microseconds since the Unix epoch (see
app.synchronization.readers) — offset_ms/drift_ppm are converted internally
so the public API stays in intuitive units (ms, ppm) while arithmetic stays
integer wherever exact.
"""

from __future__ import annotations

from typing import Iterator

from app.synchronization.models import ClockCorrectionConfig

_MICROSECONDS_PER_MS = 1000
_PPM_DENOMINATOR = 1_000_000.0

#: A correction with no effect — used when a stream has no configured
#: correction, so the rest of the pipeline can apply corrections uniformly
#: rather than branching on "is there a correction at all."
IDENTITY_CORRECTION = ClockCorrectionConfig(offset_ms=0.0, drift_ppm=0.0)


def apply_clock_correction(
    *, original_epoch_us: int, anchor_epoch_us: int, correction: ClockCorrectionConfig
) -> int:
    """Applies the affine correction and rounds to the nearest microsecond.

    Rounding is deterministic (Python's round-half-to-even on a value
    derived solely from the inputs) — the same inputs always round the
    same way.
    """
    scale = 1.0 + correction.drift_ppm / _PPM_DENOMINATOR
    offset_us = correction.offset_ms * _MICROSECONDS_PER_MS
    corrected = anchor_epoch_us + (original_epoch_us - anchor_epoch_us) * scale + offset_us
    return round(corrected)


def apply_stream_correction(
    records: Iterator[tuple[int, int, dict]], correction: ClockCorrectionConfig
) -> Iterator[tuple[int, int, dict]]:
    """Applies an affine correction to every record's epoch_us in a stream,
    anchored to the stream's own first (uncorrected) timestamp — never
    wall-clock execution time.

    A single forward pass: peeks exactly one record to establish the
    anchor, then re-yields it (corrected) before continuing — no
    buffering beyond that one record, so this stays streamable.
    """
    first = next(records, None)
    if first is None:
        return

    anchor_record_number, anchor_epoch_us, anchor_record = first
    yield (
        anchor_record_number,
        apply_clock_correction(
            original_epoch_us=anchor_epoch_us, anchor_epoch_us=anchor_epoch_us, correction=correction
        ),
        anchor_record,
    )
    for record_number, epoch_us, record in records:
        corrected = apply_clock_correction(
            original_epoch_us=epoch_us, anchor_epoch_us=anchor_epoch_us, correction=correction
        )
        yield record_number, corrected, record
