"""Canonical JSON serialization and the deterministic lineage fingerprint.

Mirrors every prior stage's canonical_json convention. Metadata is always
stored as canonical JSON text in SQLite — never pickled — so the catalog
never deserializes anything beyond plain JSON, and never executes code
found in a manifest.
"""

from __future__ import annotations

import hashlib
import json


def canonical_json(obj: object) -> str:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def compute_manifest_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_lineage_fingerprint(payload: dict) -> str:
    """A deterministic provenance digest — NOT a cryptographic signature.

    Built exclusively from content hashes, config hashes, and versioned
    logic identifiers (see the caller in app.catalog.service for exactly
    what goes in). Deliberately excludes execution IDs (ingestion_id,
    transformation_id, package_id, ...), created_at timestamps, and
    filesystem paths — two independent runs over equivalent data and
    configuration should produce the same fingerprint even though their
    random execution IDs differ.
    """
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
