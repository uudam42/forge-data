"""Unit tests for optional Parquet export (app.packaging.exporters.parquet).

Parquet is an optional dependency — every test here is skipped cleanly if
pyarrow isn't installed, demonstrating that the optional-dependency
behavior itself is clean (test #105)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pyarrow = pytest.importorskip("pyarrow")

from app.packaging.exporters.parquet import ParquetExporter

EXPORTER = ParquetExporter()


def _write_jsonl(path: Path, samples: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for sample in samples:
            f.write(json.dumps(sample, sort_keys=True) + "\n")


def test_parquet_file_created(tmp_path: Path) -> None:
    jsonl_path = tmp_path / "train.jsonl"
    _write_jsonl(jsonl_path, [{"sample_id": "s0", "window": {"index": 0, "start_timestamp": "t0", "end_timestamp": "t1"}}])
    output_path = tmp_path / "train.parquet"
    sha256, size_bytes = EXPORTER.export(jsonl_path=jsonl_path, output_path=output_path)
    assert output_path.exists()
    assert size_bytes > 0
    assert len(sha256) == 64


def test_parquet_row_count_equals_jsonl_line_count(tmp_path: Path) -> None:
    import pyarrow.parquet as pq

    samples = [{"sample_id": f"s{i}", "window": {"index": i, "start_timestamp": "t", "end_timestamp": "t"}} for i in range(5)]
    jsonl_path = tmp_path / "train.jsonl"
    _write_jsonl(jsonl_path, samples)
    output_path = tmp_path / "train.parquet"
    EXPORTER.export(jsonl_path=jsonl_path, output_path=output_path)

    table = pq.read_table(output_path)
    assert table.num_rows == 5


def test_sample_id_preserved_as_column(tmp_path: Path) -> None:
    import pyarrow.parquet as pq

    samples = [{"sample_id": "sample_abc", "window": {"index": 0, "start_timestamp": "t", "end_timestamp": "t"}}]
    jsonl_path = tmp_path / "train.jsonl"
    _write_jsonl(jsonl_path, samples)
    output_path = tmp_path / "train.parquet"
    EXPORTER.export(jsonl_path=jsonl_path, output_path=output_path)

    table = pq.read_table(output_path)
    assert table.column("sample_id").to_pylist() == ["sample_abc"]


def test_sample_json_round_trips_semantically(tmp_path: Path) -> None:
    import pyarrow.parquet as pq

    sample = {
        "sample_id": "sample_abc",
        "window": {"index": 0, "start_timestamp": "t0", "end_timestamp": "t1"},
        "features": {"imu": {"statistics": {"x": 1.5}}},
    }
    jsonl_path = tmp_path / "train.jsonl"
    _write_jsonl(jsonl_path, [sample])
    output_path = tmp_path / "train.parquet"
    EXPORTER.export(jsonl_path=jsonl_path, output_path=output_path)

    table = pq.read_table(output_path)
    round_tripped = json.loads(table.column("sample_json").to_pylist()[0])
    assert round_tripped == sample


def test_empty_jsonl_produces_empty_parquet(tmp_path: Path) -> None:
    import pyarrow.parquet as pq

    jsonl_path = tmp_path / "empty.jsonl"
    jsonl_path.write_text("")
    output_path = tmp_path / "empty.parquet"
    sha256, size_bytes = EXPORTER.export(jsonl_path=jsonl_path, output_path=output_path)
    table = pq.read_table(output_path)
    assert table.num_rows == 0
