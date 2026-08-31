"""Canonical timestamp normalization: UTC, ISO-8601, 'Z' suffix.

Format: YYYY-MM-DDTHH:MM:SS[.ffffff]Z — the fractional-second component is
included only when the source timestamp carried one, always at microsecond
(6-digit) resolution when present. This one rule is applied uniformly,
which is what "consistent representation" means here: the same timestamp
always renders the same way, and sub-second precision is preserved rather
than silently dropped, but never invented for a source value that had none.

This only re-represents a single, already-valid timestamp string — it never
resamples, interpolates, or changes sampling frequency.
"""

from __future__ import annotations

from datetime import datetime, timezone


def normalize_timestamp(raw_value: str) -> str:
    """Converts an already timezone-aware ISO-8601 string to canonical UTC Z form.

    Callers must ensure `raw_value` already passed Step 2's timezone-aware
    ISO-8601 validation — this does not re-validate leniently, it re-raises
    (as ValueError) if the value is not a parseable, timezone-aware
    timestamp, since normalization must fail loudly rather than guess.
    """
    parsed = datetime.fromisoformat(raw_value)
    if parsed.tzinfo is None:
        raise ValueError(f"Timestamp '{raw_value}' is not timezone-aware")

    as_utc = parsed.astimezone(timezone.utc)
    if as_utc.microsecond:
        return as_utc.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    return as_utc.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
