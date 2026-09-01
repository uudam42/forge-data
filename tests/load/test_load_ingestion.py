"""Load test: raw upload streaming stays bounded-memory relative to file
size. Opt-in only -- run with `pytest -m load`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.storage.local import LocalRawStorage
from tests.load.memory_utils import format_bytes, measure_peak_rss


def _generate_file(path: Path, size_bytes: int) -> None:
    chunk = b"0123456789abcdef" * 4096  # 64 KiB
    written = 0
    with path.open("wb") as f:
        while written < size_bytes:
            n = min(len(chunk), size_bytes - written)
            f.write(chunk[:n])
            written += n


def _run_ingestion(root_str: str, file_path_str: str) -> dict:
    storage = LocalRawStorage(root=Path(root_str))
    with open(file_path_str, "rb") as stream:
        saved = storage.save(
            customer_id="load_customer", session_id="load_session", ingestion_id="ing_load", filename="big.bin", stream=stream
        )
    return {"size_bytes": saved.size_bytes, "sha256": saved.sha256}


@pytest.mark.load
def test_ingestion_peak_memory_does_not_scale_with_upload_size(tmp_path: Path) -> None:
    small_file = tmp_path / "small.bin"
    large_file = tmp_path / "large.bin"
    _generate_file(small_file, 20 * 1024 * 1024)  # 20 MiB
    _generate_file(large_file, 400 * 1024 * 1024)  # 400 MiB -- 20x larger

    small_run = measure_peak_rss(_run_ingestion, str(tmp_path / "small_root"), str(small_file), timeout=300)
    large_run = measure_peak_rss(_run_ingestion, str(tmp_path / "large_root"), str(large_file), timeout=300)

    print(
        f"\ningestion 20MB: {format_bytes(small_run.peak_rss_bytes)} peak RSS, "
        f"400MB: {format_bytes(large_run.peak_rss_bytes)} peak RSS"
    )
    assert small_run.result["size_bytes"] == 20 * 1024 * 1024
    assert large_run.result["size_bytes"] == 400 * 1024 * 1024
    # A 20x file-size increase must not come close to a 20x memory
    # increase for a genuinely chunked, O(chunk) writer.
    assert large_run.peak_rss_bytes <= small_run.peak_rss_bytes * 2 + 50 * 1024 * 1024
