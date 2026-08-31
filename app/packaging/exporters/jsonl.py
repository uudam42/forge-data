"""JSONL export — mandatory, always produced regardless of the requested
`exports` list. Uses the same canonical JSON convention as every prior
stage (sort_keys, compact separators, allow_nan=False)."""

from __future__ import annotations

from app.packaging.exporters.base import StreamingLineExporter
from app.packaging.serialization import canonical_json


class JSONLExporter(StreamingLineExporter):
    format_name = "jsonl"

    def serialize_sample(self, sample: dict) -> bytes:
        return (canonical_json(sample) + "\n").encode("utf-8")
