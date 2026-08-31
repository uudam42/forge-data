"""Reads one normalized artifact into an ordered (record_number, epoch_us,
record) stream: parses Step 4's canonical timestamp representation into
integer microseconds since epoch, casts every other field to its
schema-declared native type, and enforces monotonic ordering.

Step 4 already guarantees canonical UTC "Z" timestamps — this module does
NOT re-implement arbitrary timezone parsing; it only converts the one
canonical representation into a numeric form suitable for comparison.
Deliberately integer microseconds (datetime's own native resolution),
never floating-point, so ordering/equality/arithmetic stay exact.

Reading itself reuses app.normalization.records.iter_records (which in
turn reuses app.integrity.records) rather than a fourth reimplementation of
CSV/JSON/JSONL parsing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import BinaryIO, Iterator

from app.normalization import records as normalization_records
from app.normalization.transforms.common import to_bool, to_float
from app.validation.schemas.base import FieldType, SchemaDefinition

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class InvalidTimestampError(Exception):
    pass


class NonMonotonicStreamError(Exception):
    pass


def parse_canonical_timestamp_us(value: object) -> int:
    """Parses a canonical (timezone-aware ISO-8601) timestamp string into
    integer microseconds since the Unix epoch. Raises InvalidTimestampError
    for anything else — never guesses, never falls back to a naive parse.
    """
    if not isinstance(value, str):
        raise InvalidTimestampError(f"Expected a canonical timestamp string, got {type(value).__name__}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise InvalidTimestampError(f"'{value}' is not a valid ISO-8601 timestamp: {exc}") from exc
    if parsed.tzinfo is None:
        raise InvalidTimestampError(f"Timestamp '{value}' is not timezone-aware")

    delta = parsed.astimezone(timezone.utc) - _EPOCH
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def format_epoch_us(epoch_us: int) -> str:
    """Inverse of parse_canonical_timestamp_us — same canonical format Step 4
    uses (YYYY-MM-DDTHH:MM:SS[.ffffff]Z), preserving sub-second precision
    only when present.
    """
    dt = _EPOCH + timedelta(microseconds=epoch_us)
    if dt.microsecond:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + "Z"


def _cast_field(value: object, field_type: FieldType | None) -> object:
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return None
    if field_type in (FieldType.FLOAT, FieldType.INTEGER):
        return to_float(value)
    if field_type is FieldType.BOOLEAN:
        return to_bool(value)
    # STRING / DATETIME / unknown: passthrough — already normalized by Step 4.
    return value


def iter_typed_records(
    stream: BinaryIO,
    extension: str,
    schema: SchemaDefinition,
    *,
    timestamp_field: str = "timestamp",
) -> Iterator[tuple[int, int, dict]]:
    """Yields (record_number, epoch_us, record) for a normalized artifact,
    in source order. `record` excludes `timestamp_field` (it becomes the
    numeric epoch_us instead) and has every other field cast to its
    schema-declared native type.

    Raises NonMonotonicStreamError immediately if a timestamp goes backward
    — never silently sorts, since that would both hide an upstream lineage
    problem and require buffering the whole stream.
    """
    previous_epoch_us: int | None = None

    for record_number, raw_record in normalization_records.iter_records(stream, extension):
        raw_timestamp = raw_record.get(timestamp_field)
        epoch_us = parse_canonical_timestamp_us(raw_timestamp)

        if previous_epoch_us is not None and epoch_us < previous_epoch_us:
            raise NonMonotonicStreamError(
                f"Record {record_number}: timestamp went backward "
                f"({format_epoch_us(epoch_us)} follows {format_epoch_us(previous_epoch_us)})"
            )
        previous_epoch_us = epoch_us

        typed_record = {}
        for field_name, value in raw_record.items():
            if field_name == timestamp_field:
                continue
            field_def = schema.fields.get(field_name)
            typed_record[field_name] = _cast_field(value, field_def.type if field_def else None)

        yield record_number, epoch_us, typed_record
