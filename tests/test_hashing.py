"""Tests for chunked SHA-256 hashing utilities."""

from __future__ import annotations

import hashlib
from pathlib import Path

from app.utils.hashing import ChunkedSha256, sha256_of_path


def test_sha256_of_path_matches_hashlib(tmp_path: Path) -> None:
    content = b"the quick brown fox jumps over the lazy dog" * 1000
    file_path = tmp_path / "sample.bin"
    file_path.write_bytes(content)

    expected = hashlib.sha256(content).hexdigest()
    assert sha256_of_path(file_path) == expected


def test_chunked_sha256_matches_hashlib_across_multiple_updates() -> None:
    content = b"robotics sensor payload " * 5000
    expected = hashlib.sha256(content).hexdigest()

    digest = ChunkedSha256()
    step = 4096
    for i in range(0, len(content), step):
        digest.update(content[i : i + step])

    assert digest.hexdigest() == expected


def test_chunked_sha256_empty_input_matches_hashlib() -> None:
    assert ChunkedSha256().hexdigest() == hashlib.sha256(b"").hexdigest()
