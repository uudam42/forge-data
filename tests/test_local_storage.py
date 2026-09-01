"""Tests for LocalRawStorage, exercised independently of the API layer."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest

from app.storage.base import ArtifactAlreadyExistsError
from app.storage.local import LocalRawStorage


def _storage(tmp_path: Path) -> LocalRawStorage:
    return LocalRawStorage(root=tmp_path / "raw")


def test_save_writes_file_and_returns_correct_hash_and_size(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    content = b"imu,accel,gyro\n1,2,3\n"

    result = storage.save(
        customer_id="cust_a",
        session_id="sess_a",
        ingestion_id="ing_a",
        filename="imu.csv",
        stream=io.BytesIO(content),
    )

    assert result.size_bytes == len(content)
    assert result.sha256 == hashlib.sha256(content).hexdigest()

    stored_path = Path(storage.get_path(customer_id="cust_a", session_id="sess_a", ingestion_id="ing_a", filename="imu.csv"))
    assert stored_path.exists()
    assert stored_path.read_bytes() == content


def test_exists_reflects_saved_ingestions(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    assert not storage.exists(customer_id="c", session_id="s", ingestion_id="i")

    storage.save(customer_id="c", session_id="s", ingestion_id="i", filename="a.json", stream=io.BytesIO(b"{}"))

    assert storage.exists(customer_id="c", session_id="s", ingestion_id="i")


def test_save_raises_on_duplicate_ingestion_id(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    storage.save(customer_id="c", session_id="s", ingestion_id="i", filename="a.json", stream=io.BytesIO(b"{}"))

    with pytest.raises(ArtifactAlreadyExistsError):
        storage.save(customer_id="c", session_id="s", ingestion_id="i", filename="a.json", stream=io.BytesIO(b'{"other": true}'))

    # Original content must be untouched.
    stored_path = Path(storage.get_path(customer_id="c", session_id="s", ingestion_id="i", filename="a.json"))
    assert stored_path.read_bytes() == b"{}"


def test_write_manifest_persists_json(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    storage.save(customer_id="c", session_id="s", ingestion_id="i", filename="a.json", stream=io.BytesIO(b"{}"))

    manifest = {"ingestion_id": "i", "session_id": "s", "pipeline_stage": "raw"}
    storage.write_manifest(customer_id="c", session_id="s", ingestion_id="i", manifest=manifest)

    manifest_path = Path(storage.get_path(customer_id="c", session_id="s", ingestion_id="i")) / "manifest.json"
    assert manifest_path.exists()
    assert json.loads(manifest_path.read_text()) == manifest


def test_write_manifest_raises_if_already_written(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    storage.save(customer_id="c", session_id="s", ingestion_id="i", filename="a.json", stream=io.BytesIO(b"{}"))
    storage.write_manifest(customer_id="c", session_id="s", ingestion_id="i", manifest={"a": 1})

    with pytest.raises(ArtifactAlreadyExistsError):
        storage.write_manifest(customer_id="c", session_id="s", ingestion_id="i", manifest={"a": 2})


def test_save_reads_in_bounded_chunks_never_the_whole_stream_at_once(tmp_path: Path) -> None:
    """v2.1's staging rewrite must not regress the pre-existing streaming
    guarantee: save() must never call stream.read() with no size limit
    (which would buffer an arbitrarily large upload fully in memory)."""
    storage = _storage(tmp_path)
    content = b"x" * (5 * 1024 * 1024)  # 5 MiB, several times the write chunk size

    class _BoundedReadStream(io.BytesIO):
        def read(self, size: int = -1) -> bytes:  # noqa: A003
            if size is None or size < 0:
                raise AssertionError("save() must always request a bounded chunk size, never a full read")
            return super().read(size)

    result = storage.save(
        customer_id="c", session_id="s", ingestion_id="i", filename="big.bin", stream=_BoundedReadStream(content)
    )
    assert result.size_bytes == len(content)
    assert result.sha256 == hashlib.sha256(content).hexdigest()


def test_anonymous_customer_uses_literal_directory(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    storage.save(customer_id="anonymous", session_id="s", ingestion_id="i", filename="a.json", stream=io.BytesIO(b"{}"))

    assert (tmp_path / "raw" / "anonymous" / "s" / "i" / "original" / "a.json").exists()
