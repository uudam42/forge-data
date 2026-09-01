"""Disk-space preflight checks (v2.2).

Because every artifact is immutable (v1.0) and every stage publishes
atomically (v2.1), disk usage only ever grows across the pipeline —
nothing is ever compacted or overwritten in place. For a stage whose
output can be large relative to its input, failing 90% of the way
through an expensive write because the disk filled up is worse than
refusing to start: it wastes the time already spent, and it can leave
a stale staging entry that still needs recovery. This module lets a
service check *before* starting.

This is a preflight heuristic, not a guarantee: `estimate_required_bytes`
is a ratio-based estimate, not a measurement, and free space can still
change between the check and the actual write (another process, another
request). Treat a passed preflight as "very unlikely to run out," not
"provably safe."
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from app.storage.errors import InsufficientDiskSpaceError


@dataclass(frozen=True)
class DiskPreflightResult:
    available_bytes: int
    estimated_required_bytes: int
    reserve_bytes: int
    ok: bool


def estimate_required_bytes(input_bytes: int, *, ratio: float) -> int:
    """A conservative, explicitly-labeled ESTIMATE, never an exact figure.
    `ratio` is a per-stage, documented multiplier on input size (e.g.
    packaging writing train/validation/test/split_index/report/manifest
    for a single train_ratio=1.0 split roughly doubles bytes-on-disk
    relative to the transformed input; multi-split or Parquet exports
    cost more). Callers own choosing a ratio appropriate to their stage
    and configuration — this function does no stage-specific reasoning.
    """
    return int(input_bytes * ratio)


def check_disk_space(
    path: Path, *, estimated_required_bytes: int, reserve_bytes: int, safety_factor: float = 1.0
) -> DiskPreflightResult:
    """Read-only check against the filesystem that hosts `path` (which
    need not exist yet — shutil.disk_usage walks up to an existing
    ancestor). Never writes anything."""
    probe = path
    while not probe.exists():
        if probe.parent == probe:
            break
        probe = probe.parent
    usage = shutil.disk_usage(probe)

    required_with_margin = int(estimated_required_bytes * safety_factor) + reserve_bytes
    ok = usage.free >= required_with_margin and usage.free >= reserve_bytes
    return DiskPreflightResult(
        available_bytes=usage.free,
        estimated_required_bytes=required_with_margin,
        reserve_bytes=reserve_bytes,
        ok=ok,
    )


def require_disk_space(
    path: Path,
    *,
    stage: str,
    estimated_required_bytes: int,
    reserve_bytes: int,
    safety_factor: float = 1.0,
) -> DiskPreflightResult:
    """Same check as `check_disk_space`, raising InsufficientDiskSpaceError
    (structured: stage, available_bytes, estimated_required_bytes,
    reserve_bytes) instead of returning ok=False. Call this before
    starting an expensive write; a passing call performs no filesystem
    writes of its own."""
    result = check_disk_space(
        path,
        estimated_required_bytes=estimated_required_bytes,
        reserve_bytes=reserve_bytes,
        safety_factor=safety_factor,
    )
    if not result.ok:
        raise InsufficientDiskSpaceError(
            stage=stage,
            available_bytes=result.available_bytes,
            estimated_required_bytes=result.estimated_required_bytes,
            reserve_bytes=result.reserve_bytes,
        )
    return result
