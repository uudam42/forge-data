"""Unit tests for the disk-space preflight helper (v2.2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.storage.disk_preflight import check_disk_space, estimate_required_bytes, require_disk_space
from app.storage.errors import InsufficientDiskSpaceError


def test_estimate_required_bytes_applies_ratio() -> None:
    assert estimate_required_bytes(1000, ratio=1.5) == 1500
    assert estimate_required_bytes(1000, ratio=0.5) == 500


def test_check_disk_space_passes_for_a_tiny_realistic_request(tmp_path: Path) -> None:
    result = check_disk_space(tmp_path, estimated_required_bytes=1024, reserve_bytes=1024, safety_factor=1.2)
    assert result.ok is True
    assert result.available_bytes > 0


def test_check_disk_space_rejects_an_impossible_request(tmp_path: Path) -> None:
    huge = 10**18  # an exabyte -- no real disk has this much free space
    result = check_disk_space(tmp_path, estimated_required_bytes=huge, reserve_bytes=0, safety_factor=1.0)
    assert result.ok is False


def test_require_disk_space_raises_structured_error(tmp_path: Path) -> None:
    huge = 10**18
    with pytest.raises(InsufficientDiskSpaceError) as exc_info:
        require_disk_space(tmp_path, stage="packaging", estimated_required_bytes=huge, reserve_bytes=0)

    error = exc_info.value
    assert error.stage == "packaging"
    assert error.available_bytes >= 0
    assert error.estimated_required_bytes >= huge
    payload = error.to_dict()
    assert payload["code"] == "INSUFFICIENT_DISK_SPACE"
    assert payload["stage"] == "packaging"
    assert "available_bytes" in payload and "estimated_required_bytes" in payload and "reserve_bytes" in payload


def test_require_disk_space_passes_silently_for_realistic_request(tmp_path: Path) -> None:
    result = require_disk_space(tmp_path, stage="normalization", estimated_required_bytes=1024, reserve_bytes=1024)
    assert result.ok is True


def test_check_disk_space_handles_a_path_that_does_not_exist_yet(tmp_path: Path) -> None:
    nonexistent = tmp_path / "does" / "not" / "exist" / "yet"
    result = check_disk_space(nonexistent, estimated_required_bytes=1024, reserve_bytes=1024)
    assert result.available_bytes > 0  # resolved via an existing ancestor, not an error
