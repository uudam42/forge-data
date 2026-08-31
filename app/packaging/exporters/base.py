"""Dataset exporter contracts. PackagingService never contains
format-specific serialization logic for any export type — each format is
an isolated DatasetExporter implementation.

JSONL is the pipeline's native line-oriented representation (matching
every prior stage's own artifact format), so it's written inline in the
service's main streaming pass via a `StreamingLineExporter` — one sample
serialized at a time, never materializing more than one sample in memory.

Every other format (currently just Parquet) is a `PostProcessExporter`
that reads back an already-written, already-committed-to-staging split
JSONL file and produces an additional representation from it. This keeps
the core two-pass loop simple and format-agnostic, and bounds each
optional export's memory to one split's worth of already-serialized text,
never the whole dataset's feature payloads at once.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class ExportDependencyMissingError(Exception):
    pass


class DatasetExporter(ABC):
    format_name: str


class StreamingLineExporter(DatasetExporter):
    @abstractmethod
    def serialize_sample(self, sample: dict) -> bytes:
        """Returns the exact bytes (including trailing newline) to write
        for one sample."""
        raise NotImplementedError


class PostProcessExporter(DatasetExporter):
    @abstractmethod
    def export(self, *, jsonl_path: Path, output_path: Path) -> tuple[str, int]:
        """Reads an already-written split JSONL file and writes
        output_path in this exporter's format. Returns (sha256,
        size_bytes) of the new output file. Never mutates jsonl_path."""
        raise NotImplementedError
