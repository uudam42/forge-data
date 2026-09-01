"""Reusable crash-safety invariant assertions, shared across the v2.1
test files in this directory (not a test module itself — pytest ignores
files without a test_ prefix)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Callable

_FINALIZED_STATUSES = {"completed", "rejected", "passed", "passed_with_warnings", "failed", "stored"}


def assert_no_partial_final_artifacts(final_dir: Path) -> None:
    """A finalized artifact directory, if it exists at all, must be
    complete: it carries a manifest.json/report.json declaring a
    genuinely finalized status, never a directory sitting there without
    one, and never status="running" or similar mid-flight state."""
    if not final_dir.exists():
        return
    manifest_path = final_dir / "manifest.json"
    report_path = final_dir / "report.json"
    manifest_like = manifest_path if manifest_path.exists() else report_path
    assert manifest_like.exists(), f"{final_dir} exists but has no manifest.json/report.json -- partial artifact"
    payload = json.loads(manifest_like.read_text(encoding="utf-8"))
    status = payload.get("status")
    if status is not None:
        assert status in _FINALIZED_STATUSES, f"{manifest_like} declares a non-finalized status: {status!r}"


def assert_staging_not_discoverable(lookup: Callable[[], object]) -> None:
    """`lookup` performs a store's own find_* call for a staging-only ID.
    The invariant: it returns a "not found" value, never raises, and
    never returns anything resembling committed data."""
    result = lookup()
    assert result in (None, [], {}), f"a staging entry was discoverable via lookup: {result!r}"


def assert_upstream_unchanged(path: Path, expected_sha256: str) -> None:
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    assert actual == expected_sha256, f"upstream artifact {path} was mutated (expected {expected_sha256}, got {actual})"


def assert_final_artifact_checksums_valid(final_dir: Path, *, data_filename: str, manifest_field: str) -> None:
    manifest = json.loads((final_dir / "manifest.json").read_text(encoding="utf-8"))
    expected = manifest[manifest_field]
    actual = hashlib.sha256((final_dir / data_filename).read_bytes()).hexdigest()
    assert actual == expected, f"{data_filename} checksum mismatch: manifest says {expected}, actual {actual}"
