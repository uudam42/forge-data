"""Streaming record readers for integrity checks, keyed by file extension.

These are intentionally separate from app.validation.validators — those are
tightly coupled to structural validation (ErrorAccumulator, RecordEvaluator,
header-vs-row presence checks) and produce validation issues, not raw
records. Integrity checkers need the actual field values instead, so this
module owns its own thin, format-specific iteration:

- CSV and JSONL are streamed (O(1) memory per record), never fully buffered.
- JSON continues to load the whole payload into memory, mirroring the same
  documented MVP limitation as the Step 2 JSON validator.

Since integrity checking only ever runs on data that already passed Step 2
validation, malformed rows/lines are not expected here — if a record cannot
be parsed, it is skipped rather than raised, matching the fail-safe posture
used throughout this module (checkers should never crash the whole run over
one row).
"""

from __future__ import annotations

import csv
import io
import json
from typing import BinaryIO, Iterator


class UnsupportedIntegrityFileTypeError(Exception):
    pass


def _iter_csv_records(stream: BinaryIO) -> Iterator[tuple[int, dict]]:
    text_stream = io.TextIOWrapper(stream, encoding="utf-8", newline="")
    reader = csv.DictReader(text_stream)
    for index, row in enumerate(reader, start=1):
        row.pop(None, None)  # csv.DictReader's restkey for ragged rows
        yield index, row


def _iter_jsonl_records(stream: BinaryIO) -> Iterator[tuple[int, dict]]:
    text_stream = io.TextIOWrapper(stream, encoding="utf-8")
    for line_number, raw_line in enumerate(text_stream, start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            yield line_number, record


def _iter_json_records(stream: BinaryIO) -> Iterator[tuple[int, dict]]:
    text_stream = io.TextIOWrapper(stream, encoding="utf-8")
    try:
        payload = json.load(text_stream)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return

    if isinstance(payload, dict):
        records = [payload]
    elif isinstance(payload, list):
        records = payload
    else:
        return

    for index, record in enumerate(records, start=1):
        if isinstance(record, dict):
            yield index, record


_READERS = {
    ".csv": _iter_csv_records,
    ".json": _iter_json_records,
    ".jsonl": _iter_jsonl_records,
}


def supports(extension: str) -> bool:
    return extension.lower() in _READERS


def iter_records(stream: BinaryIO, extension: str) -> Iterator[tuple[int, dict]]:
    reader = _READERS.get(extension.lower())
    if reader is None:
        raise UnsupportedIntegrityFileTypeError(
            f"Integrity checking is not supported for file type '{extension}'"
        )
    return reader(stream)
