"""Canonical JSON serialization for the QC stage.

Mirrors app.transformation.serialization's reasoning: QC computes real
numeric aggregates (means, standardized mean differences) that could, in
principle, be non-finite, so `allow_nan=False` makes that fail loudly
rather than silently emit invalid JSON.
"""

from __future__ import annotations

import hashlib
import json


def canonical_json(obj: object) -> str:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def compute_qc_config_hash(payload: dict) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def compute_report_sha256(report_bytes: bytes) -> str:
    return hashlib.sha256(report_bytes).hexdigest()
