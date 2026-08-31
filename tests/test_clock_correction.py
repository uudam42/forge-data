"""Unit tests for deterministic clock offset/drift correction.

corrected_time = anchor_time + (original_time - anchor_time) * scale + offset
scale = 1 + drift_ppm / 1_000_000
anchor = the stream's own first (uncorrected) timestamp.
"""

from __future__ import annotations

from app.synchronization.clocks.correction import apply_clock_correction, apply_stream_correction
from app.synchronization.models import ClockCorrectionConfig


def test_zero_correction_is_identity() -> None:
    result = apply_clock_correction(
        original_epoch_us=1_000_000, anchor_epoch_us=1_000_000, correction=ClockCorrectionConfig()
    )
    assert result == 1_000_000


def test_offset_correction_shifts_every_point() -> None:
    correction = ClockCorrectionConfig(offset_ms=-25.0, drift_ppm=0.0)
    # anchor at t=0; a later sample at t=1_000_000us (1s) should shift by -25ms = -25_000us
    result = apply_clock_correction(original_epoch_us=1_000_000, anchor_epoch_us=0, correction=correction)
    assert result == 1_000_000 - 25_000


def test_offset_correction_applies_even_at_anchor() -> None:
    correction = ClockCorrectionConfig(offset_ms=-25.0, drift_ppm=0.0)
    result = apply_clock_correction(original_epoch_us=0, anchor_epoch_us=0, correction=correction)
    assert result == -25_000


def test_positive_drift_correction_correct() -> None:
    # drift_ppm=20 over 1_000_000us (1s) from anchor -> scale=1.000020
    # corrected = 0 + 1_000_000 * 1.00002 = 1_000_020
    correction = ClockCorrectionConfig(offset_ms=0.0, drift_ppm=20.0)
    result = apply_clock_correction(original_epoch_us=1_000_000, anchor_epoch_us=0, correction=correction)
    assert result == 1_000_020


def test_negative_drift_correction_correct() -> None:
    correction = ClockCorrectionConfig(offset_ms=0.0, drift_ppm=-20.0)
    result = apply_clock_correction(original_epoch_us=1_000_000, anchor_epoch_us=0, correction=correction)
    assert result == 999_980


def test_offset_and_drift_combined_correct() -> None:
    # scale = 1 + 20/1e6 = 1.00002; (1_000_000 - 0) * 1.00002 = 1_000_020; + offset(-25000) = 995_020
    correction = ClockCorrectionConfig(offset_ms=-25.0, drift_ppm=20.0)
    result = apply_clock_correction(original_epoch_us=1_000_000, anchor_epoch_us=0, correction=correction)
    assert result == 1_000_020 - 25_000


def test_drift_scales_relative_to_anchor_not_epoch_zero() -> None:
    # anchor at t=5_000_000; a sample 1s after the anchor drifts by the same
    # amount as if the anchor were at zero — drift is relative to the anchor.
    correction = ClockCorrectionConfig(offset_ms=0.0, drift_ppm=20.0)
    result = apply_clock_correction(
        original_epoch_us=6_000_000, anchor_epoch_us=5_000_000, correction=correction
    )
    assert result == 5_000_000 + 1_000_020


def test_apply_stream_correction_does_not_mutate_input_records() -> None:
    records = [(1, 0, {"a": 1}), (2, 1_000_000, {"a": 2})]
    correction = ClockCorrectionConfig(offset_ms=-25.0, drift_ppm=0.0)

    corrected = list(apply_stream_correction(iter(records), correction))

    # Original tuples/dicts must be untouched — correction only affects the
    # yielded epoch_us, never the record payload, and never mutates the
    # source list in place.
    assert records == [(1, 0, {"a": 1}), (2, 1_000_000, {"a": 2})]
    assert corrected[0] == (1, -25_000, {"a": 1})
    assert corrected[1] == (2, 1_000_000 - 25_000, {"a": 2})


def test_apply_stream_correction_anchors_on_first_uncorrected_timestamp() -> None:
    records = [(1, 10_000_000, {"a": 1}), (2, 11_000_000, {"a": 2})]
    correction = ClockCorrectionConfig(offset_ms=0.0, drift_ppm=20.0)

    corrected = list(apply_stream_correction(iter(records), correction))

    # anchor = 10_000_000 (the stream's own first timestamp) — not epoch 0
    # and not wall-clock time.
    assert corrected[0][1] == 10_000_000  # anchor itself: (t-anchor)=0, no drift applied
    assert corrected[1][1] == 10_000_000 + 1_000_020


def test_apply_stream_correction_on_empty_stream_yields_nothing() -> None:
    corrected = list(apply_stream_correction(iter([]), ClockCorrectionConfig()))
    assert corrected == []
