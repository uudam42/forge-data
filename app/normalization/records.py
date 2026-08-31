"""Source-record reading and normalized-record writing for normalization,
keyed by file extension.

Reading reuses app.integrity.records's CSV/JSON/JSONL iteration — the same
generic (record_number, record) parsing already used by Step 3, with no
integrity-specific semantics. Reimplementing the same csv.DictReader /
json.load plumbing a third time (Step 2 validators, Step 3, now Step 4)
would be pure duplication for zero behavioral benefit.

Writing is new here. CSV and JSONL are streamed (O(1) memory per record).
JSON output is always a top-level array, regardless of whether the source
was a single object or an array — this is a deliberate MVP simplification
(a normalized dataset is a collection of records; an array is the more
general, consistent shape) and is loaded fully into memory, matching the
same non-streaming MVP limitation documented for JSON elsewhere in this
pipeline.
"""

from __future__ import annotations

import csv
import io
import json
from typing import BinaryIO, Iterator

from app.integrity import records as _integrity_records


class UnsupportedNormalizationFileTypeError(Exception):
    pass


def supports(extension: str) -> bool:
    return _integrity_records.supports(extension)


def iter_records(stream: BinaryIO, extension: str) -> Iterator[tuple[int, dict]]:
    try:
        return _integrity_records.iter_records(stream, extension)
    except _integrity_records.UnsupportedIntegrityFileTypeError as exc:
        raise UnsupportedNormalizationFileTypeError(str(exc)) from exc


def write_records(
    destination: BinaryIO, extension: str, records: Iterator[dict], *, fieldnames: list[str]
) -> int:
    ext = extension.lower()
    if ext == ".csv":
        return _write_csv_records(destination, fieldnames, records)
    if ext == ".jsonl":
        return _write_jsonl_records(destination, records)
    if ext == ".json":
        return _write_json_records(destination, list(records))
    raise UnsupportedNormalizationFileTypeError(f"No normalized writer for extension '{extension}'")


def _write_csv_records(destination: BinaryIO, fieldnames: list[str], records: Iterator[dict]) -> int:
    text_stream = io.TextIOWrapper(destination, encoding="utf-8", newline="")
    writer = csv.DictWriter(text_stream, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()

    count = 0
    for record in records:
        writer.writerow({name: _csv_cell(record.get(name)) for name in fieldnames})
        count += 1

    text_stream.flush()
    text_stream.detach()  # leave `destination` open — the caller owns its lifecycle
    return count


def _write_jsonl_records(destination: BinaryIO, records: Iterator[dict]) -> int:
    count = 0
    for record in records:
        destination.write((json.dumps(record) + "\n").encode("utf-8"))
        count += 1
    return count


def _write_json_records(destination: BinaryIO, records: list[dict]) -> int:
    destination.write(json.dumps(records, indent=2).encode("utf-8"))
    return len(records)


def _csv_cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value)
    return str(value)
