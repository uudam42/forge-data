"""Canonical JSON serialization and deterministic hashing for the
packaging stage.

Mirrors app.qc.serialization's reasoning: `allow_nan=False` makes an
unexpected non-finite value fail loudly at serialization time rather than
silently emit invalid JSON. Step 9 never computes new numeric values from
feature content (it only reserializes existing samples), so this is a
pure defensive measure, not an expected code path.
"""

from __future__ import annotations

import hashlib
import json


def canonical_json(obj: object) -> str:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def compute_packaging_config_hash(payload: dict) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def compute_file_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compute_group_id(*, transformed_sha256: str, group_min_row: int, group_max_row: int) -> str:
    """Deterministic group ID derived from stable content provenance —
    transformed_sha256 (not transformation_id) so the group ID is tied to
    exact byte content, giving stronger reproducibility than an ID alone
    would."""
    payload = f"{transformed_sha256}:{group_min_row}:{group_max_row}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"grp_{digest[:16]}"


def compute_session_group_id(*, transformed_sha256: str, session_id: str) -> str:
    payload = f"{transformed_sha256}:session:{session_id}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"grp_{digest[:16]}"


def group_split_fraction(*, group_id: str, seed: int, profile_name: str, profile_version: str) -> float:
    """Maps a group deterministically into [0, 1) via SHA-256 — never
    Python's hash() (which is randomized per-process for str by default)
    and never runtime RNG state. The same group_id + seed + profile always
    produces the same fraction, and a given group's fraction never depends
    on any other group — this is what makes assignments stable as new,
    unrelated groups are added later (see README "Stability under dataset
    growth")."""
    payload = f"{group_id}:{seed}:{profile_name}:{profile_version}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    as_int = int(digest[:8], 16)  # first 32 bits
    return as_int / 0x1_0000_0000
