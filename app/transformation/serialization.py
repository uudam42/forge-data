"""Canonical JSON serialization and deterministic sample-ID generation for
the transformation stage.

Step 6's app.cleaning.rules.common.canonical_json is deliberately NOT
reused here: it omits allow_nan=False because Step 6 only ever passes
pre-validated values through unchanged. Step 7 performs real numeric
computation (statistics, derived magnitudes) that could, in principle,
yield a non-finite result if fed unexpected input — allow_nan=False makes
that fail loudly (a ValueError from json.dumps) instead of silently
emitting invalid JSON (NaN/Infinity are not valid JSON tokens).
"""

from __future__ import annotations

import hashlib
import json

_SAMPLE_ID_LENGTH = 32  # hex chars (128 bits) — enormous collision resistance, still shortened for readability


def canonical_json(obj: object) -> str:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def compute_transformation_config_hash(payload: dict) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def compute_sample_id(
    *,
    cleaned_sha256: str,
    config_hash: str,
    window_index: int,
    start_epoch_us: int,
    end_epoch_us: int,
) -> str:
    """Deterministic, content-derived sample ID: same cleaned bytes + same
    effective config + same window position always produce the same ID.
    Deliberately NOT a random UUID (see Step 7 spec)."""
    payload = f"{cleaned_sha256}:{config_hash}:{window_index}:{start_epoch_us}:{end_epoch_us}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"sample_{digest[:_SAMPLE_ID_LENGTH]}"
