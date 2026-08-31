"""Optional Parquet export.

Parquet is an optional dependency (`pip install .[parquet]`) — the base
install works fully without it; requesting `"parquet"` in `exports`
without pyarrow installed raises ExportDependencyMissingError, mapped by
the service to a clear 400 UNSUPPORTED_EXPORT_FORMAT rather than crashing.

Transformed samples are nested (arbitrary feature trees per stream), so
this deliberately does NOT flatten every feature into its own column —
that would create hundreds of unstable columns tied to whatever features
happened to be configured in Step 7. Instead: a handful of stable index
columns (sample_id, window_index, start/end timestamp) plus the full
canonical sample JSON as a string column. This keeps Parquet support
generic without hardcoding any feature schema.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.packaging.exporters.base import ExportDependencyMissingError, PostProcessExporter
from app.packaging.serialization import compute_file_sha256


class ParquetExporter(PostProcessExporter):
    format_name = "parquet"

    def export(self, *, jsonl_path: Path, output_path: Path) -> tuple[str, int]:
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError as exc:
            raise ExportDependencyMissingError(
                "Parquet export requires the optional 'pyarrow' dependency "
                "(install with: pip install .[parquet])"
            ) from exc

        sample_ids: list[str | None] = []
        window_indices: list[int | None] = []
        start_timestamps: list[str | None] = []
        end_timestamps: list[str | None] = []
        sample_json: list[str] = []

        with jsonl_path.open("r", encoding="utf-8") as source:
            for line in source:
                stripped = line.strip()
                if not stripped:
                    continue
                obj = json.loads(stripped)
                window = obj.get("window") or {}
                sample_ids.append(obj.get("sample_id"))
                window_indices.append(window.get("index"))
                start_timestamps.append(window.get("start_timestamp"))
                end_timestamps.append(window.get("end_timestamp"))
                sample_json.append(stripped)

        table = pa.table(
            {
                "sample_id": sample_ids,
                "window_index": window_indices,
                "start_timestamp": start_timestamps,
                "end_timestamp": end_timestamps,
                "sample_json": sample_json,
            }
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, output_path)

        data = output_path.read_bytes()
        return compute_file_sha256(data), len(data)
