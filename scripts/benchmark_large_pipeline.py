#!/usr/bin/env python3
"""Reproducible large-data pipeline benchmark (v2.2).

Generates synthetic IMU + GPS sensor data at configurable row counts and
runs it through the full pipeline (ingestion -> validation -> integrity
-> normalization -> synchronization -> cleaning -> transformation -> QC
-> packaging), reporting per-stage duration, throughput, and bytes
written, plus one whole-run peak RSS figure per dataset size.

Usage:
    python scripts/benchmark_large_pipeline.py
    python scripts/benchmark_large_pipeline.py --sizes 100000 500000 1000000
    python scripts/benchmark_large_pipeline.py --sizes 10000 --keep-data

Honesty notes (read before trusting a number from this script):
  - Peak RSS is measured for the ENTIRE benchmark subprocess for one
    dataset size, not isolated per stage -- see the "whole run" figure
    at the bottom of each size's table. Per-stage rows report duration/
    throughput/bytes only, which ARE isolated (wall-clock checkpoints
    between stage calls).
  - This uses the FastAPI TestClient (in-process ASGI transport, no real
    HTTP/socket layer), which adds a small, roughly constant per-request
    overhead vs. a real deployed server -- negligible at these row
    counts, but not zero.
  - Results depend entirely on the machine this runs on. Never copy
    numbers from this script's output into documentation as a
    performance guarantee.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.load.memory_utils import format_bytes, measure_peak_rss  # noqa: E402

_BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


@dataclass
class StageResult:
    name: str
    records: int
    duration_s: float
    bytes_written: int

    @property
    def throughput(self) -> float:
        return self.records / self.duration_s if self.duration_s > 0 else float("inf")


@dataclass
class RunResult:
    row_count: int
    stages: list[StageResult] = field(default_factory=list)


def _generate_imu_csv(path: Path, num_rows: int) -> int:
    with path.open("w", encoding="utf-8") as f:
        f.write("timestamp,accel_x,accel_y,accel_z,gyro_x,gyro_y,gyro_z\n")
        for i in range(num_rows):
            ts = (_BASE_TIME + timedelta(milliseconds=i * 10)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            f.write(f"{ts},0.{i % 10},0.2,9.8,0.01,0.02,0.03\n")
    return path.stat().st_size


def _generate_gps_csv(path: Path, num_rows: int) -> int:
    # GPS at 1/10th the IMU rate -- realistic relative sensor frequencies.
    with path.open("w", encoding="utf-8") as f:
        f.write("timestamp,latitude,longitude,altitude,speed\n")
        for i in range(num_rows):
            ts = (_BASE_TIME + timedelta(milliseconds=i * 100)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            f.write(f"{ts},34.0{i % 90:02d}00,-118.2{i % 90:02d}00,100.0,9.{i % 9}\n")
    return path.stat().st_size


def _run_pipeline(row_count: int, keep_data: bool) -> RunResult:
    workdir = Path(tempfile.mkdtemp(prefix="forge_data_benchmark_"))
    try:
        from fastapi.testclient import TestClient

        from app.core.config import Settings, get_settings
        from app.main import app

        settings = Settings(
            RAW_STORAGE_ROOT=workdir / "raw",
            VALIDATION_STORAGE_ROOT=workdir / "validation",
            INTEGRITY_STORAGE_ROOT=workdir / "integrity",
            NORMALIZED_STORAGE_ROOT=workdir / "normalized",
            SYNCHRONIZED_STORAGE_ROOT=workdir / "synchronized",
            CLEANED_STORAGE_ROOT=workdir / "cleaned",
            TRANSFORMED_STORAGE_ROOT=workdir / "transformed",
            QC_STORAGE_ROOT=workdir / "qc",
            PACKAGE_STORAGE_ROOT=workdir / "packages",
            CATALOG_DB_PATH=workdir / "catalog.db",
            LOG_LEVEL="WARNING",
        )
        from app.core.logging import configure_logging

        configure_logging(settings.LOG_LEVEL)
        app.dependency_overrides[get_settings] = lambda: settings
        client = TestClient(app)

        result = RunResult(row_count=row_count)

        def _timed(name: str, records: int, fn):
            start = time.monotonic()
            out = fn()
            elapsed = time.monotonic() - start
            bytes_written = 0
            result.stages.append(StageResult(name=name, records=records, duration_s=elapsed, bytes_written=bytes_written))
            return out

        imu_path = workdir / "imu.csv"
        gps_path = workdir / "gps.csv"
        imu_bytes = _generate_imu_csv(imu_path, row_count)
        gps_bytes = _generate_gps_csv(gps_path, max(1, row_count // 10))

        session_id = f"bench_{row_count}"

        def _ingest_imu():
            with imu_path.open("rb") as f:
                r = client.post(
                    "/api/v1/ingestion/upload",
                    files={"file": ("imu.csv", f, "text/csv")},
                    data={"customer_id": "benchmark", "session_id": session_id},
                )
            assert r.status_code == 201, r.text
            return r.json()

        def _ingest_gps():
            with gps_path.open("rb") as f:
                r = client.post(
                    "/api/v1/ingestion/upload",
                    files={"file": ("gps.csv", f, "text/csv")},
                    data={"customer_id": "benchmark", "session_id": session_id},
                )
            assert r.status_code == 201, r.text
            return r.json()

        imu_ing = _timed("Ingestion (IMU)", row_count, _ingest_imu)
        gps_ing = _timed("Ingestion (GPS)", max(1, row_count // 10), _ingest_gps)
        result.stages[-2].bytes_written = imu_bytes
        result.stages[-1].bytes_written = gps_bytes

        def _validate(ingestion_id: str, schema_name: str):
            r = client.post(f"/api/v1/validation/{ingestion_id}", json={"schema_name": schema_name, "schema_version": "1.0.0"})
            assert r.status_code == 200, r.text
            return r.json()

        _timed("Validation (IMU)", row_count, lambda: _validate(imu_ing["ingestion_id"], "imu"))
        _timed("Validation (GPS)", max(1, row_count // 10), lambda: _validate(gps_ing["ingestion_id"], "gps"))

        def _integrity(ingestion_id: str, schema_name: str):
            r = client.post(f"/api/v1/integrity/{ingestion_id}", json={"schema_name": schema_name, "schema_version": "1.0.0"})
            assert r.status_code == 200, r.text
            return r.json()

        _timed("Integrity (IMU)", row_count, lambda: _integrity(imu_ing["ingestion_id"], "imu"))
        _timed("Integrity (GPS)", max(1, row_count // 10), lambda: _integrity(gps_ing["ingestion_id"], "gps"))

        def _normalize(ingestion_id: str, schema_name: str, profile_name: str, source_units: dict):
            r = client.post(
                f"/api/v1/normalization/{ingestion_id}",
                json={
                    "schema_name": schema_name, "schema_version": "1.0.0",
                    "profile_name": profile_name, "profile_version": "1.0.0",
                    "source_units": source_units,
                },
            )
            assert r.status_code == 200, r.text
            return r.json()

        imu_norm = _timed(
            "Normalization (IMU)", row_count,
            lambda: _normalize(imu_ing["ingestion_id"], "imu", "imu_canonical", {"acceleration": "m/s^2", "angular_velocity": "rad/s"}),
        )
        gps_norm = _timed(
            "Normalization (GPS)", max(1, row_count // 10),
            lambda: _normalize(gps_ing["ingestion_id"], "gps", "gps_canonical", {"altitude": "m", "speed": "m/s"}),
        )

        def _synchronize():
            r = client.post(
                "/api/v1/synchronization",
                json={
                    "streams": [
                        {"name": "imu", "normalization_id": imu_norm["normalization_id"]},
                        {"name": "gps", "normalization_id": gps_norm["normalization_id"]},
                    ],
                    "reference": {"mode": "stream", "stream": "imu"},
                    "alignment": {"default_method": "nearest", "max_time_delta_ms": 500},
                },
            )
            assert r.status_code == 200, r.text
            return r.json()

        sync = _timed("Synchronization", row_count, _synchronize)

        def _clean():
            r = client.post(
                f"/api/v1/cleaning/{sync['synchronization_id']}",
                json={"policy_name": "default_multimodal", "policy_version": "1.0.0", "config": {"required_streams": ["imu"]}},
            )
            assert r.status_code == 200, r.text
            return r.json()

        cleaned = _timed("Cleaning (memory dedup)", sync["rows_written"], _clean)

        def _transform():
            r = client.post(
                f"/api/v1/transformation/{cleaned['cleaning_id']}",
                json={
                    "profile_name": "multimodal_window_v1", "profile_version": "1.0.0",
                    "config": {"window": {"mode": "count", "size": 100, "stride": 100, "drop_incomplete": True}},
                },
            )
            assert r.status_code == 200, r.text
            return r.json()

        xform = _timed("Transformation", cleaned["summary"]["retained_rows"], _transform)

        def _qc():
            r = client.post(
                f"/api/v1/qc/{xform['transformation_id']}",
                json={"profile_name": "default_dataset_qc", "profile_version": "1.0.0", "config": {"minimum_samples": 1}},
            )
            assert r.status_code == 200, r.text
            return r.json()

        qc = _timed("QC", xform["summary"]["samples_written"], _qc)

        def _package():
            r = client.post(
                f"/api/v1/packaging/{xform['transformation_id']}",
                json={
                    "qc_id": qc["qc_id"], "profile_name": "default_ml_package", "profile_version": "1.0.0",
                    "config": {
                        "split": {"strategy": "group_hash", "train_ratio": 0.8, "validation_ratio": 0.1, "test_ratio": 0.1, "seed": 42},
                        "grouping": {"mode": "source_overlap"}, "exports": ["jsonl"],
                    },
                },
            )
            assert r.status_code == 200, r.text
            return r.json()

        _timed("Packaging", xform["summary"]["samples_written"], _package)

        # Total bytes written on disk for this run.
        total_bytes = sum(f.stat().st_size for f in workdir.rglob("*") if f.is_file())
        result.stages.append(StageResult(name="(disk total)", records=0, duration_s=0.0, bytes_written=total_bytes))

        return result
    finally:
        if not keep_data:
            shutil.rmtree(workdir, ignore_errors=True)
        else:
            print(f"  (kept benchmark data at {workdir})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sizes", type=int, nargs="+", default=[100_000, 500_000, 1_000_000])
    parser.add_argument("--keep-data", action="store_true", help="don't delete generated benchmark data afterward")
    args = parser.parse_args()

    print(f"Forge Data v2.2 benchmark -- platform: {sys.platform}, python: {sys.version.split()[0]}\n")

    for size in args.sizes:
        print(f"=== Dataset: {size:,} IMU rows (+{max(1, size // 10):,} GPS rows) ===")
        run = measure_peak_rss(_run_pipeline, size, args.keep_data, timeout=1800)
        result: RunResult = run.result

        header = f"{'Stage':<24}{'Records':>12}{'Time (s)':>10}{'Throughput':>16}{'Bytes':>14}"
        print(header)
        print("-" * len(header))
        for stage in result.stages:
            if stage.name == "(disk total)":
                print(f"{'(disk total written)':<24}{'':>12}{'':>10}{'':>16}{format_bytes(stage.bytes_written):>14}")
                continue
            throughput = f"{stage.throughput:,.0f} rec/s" if stage.records else "-"
            bytes_str = format_bytes(stage.bytes_written) if stage.bytes_written else "-"
            print(f"{stage.name:<24}{stage.records:>12,}{stage.duration_s:>10.2f}{throughput:>16}{bytes_str:>14}")
        print(f"\nWhole-run peak RSS: {format_bytes(run.peak_rss_bytes)}  (wall: {run.wall_seconds:.1f}s)\n")


if __name__ == "__main__":
    main()
