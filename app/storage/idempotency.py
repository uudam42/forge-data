"""Idempotency-key infrastructure for deterministic derived stages (v2.1).

Deliberately NOT wired into any live service in this milestone. Every
stage today intentionally allows two identical requests to produce two
distinct artifacts with distinct IDs — that is v1.0's existing, tested
behavior (e.g. re-normalizing the same ingestion with the same config
twice yields two normalization_ids), and forcing dedup onto it would
change semantics several existing tests already rely on.

What v2.1 adds is the reusable *computation* an opt-in caller would need
to detect "this exact deterministic execution already happened":

    key = execution_key(
        stage="normalization",
        upstream_identity="ing_...",
        upstream_content_sha256="...",
        config_hash="...",
        implementation_version="1.0.0",
    )

The key is a pure function of content/config identity, never of a
randomly generated execution ID — the same inputs always produce the
same key, independent of when or how many times they're computed. A
future stage can opt in by looking up an existing finalized artifact
whose recorded execution_key matches before starting new work; nothing
here performs that lookup itself, since what counts as "the same
upstream" is stage-specific (see docs/DETAILED_GUIDE.md, "idempotency").
"""

from __future__ import annotations

import hashlib
import json


def _canonical_json(obj: object) -> str:
    """Same convention used throughout this project (see
    app.catalog.serialization.canonical_json) — duplicated rather than
    imported so app.storage never depends on app.catalog, which is built
    on top of it."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)


def execution_key(
    *,
    stage: str,
    upstream_identity: str,
    upstream_content_sha256: str,
    config_hash: str,
    implementation_version: str,
) -> str:
    """SHA-256 over exactly these five fields, canonically serialized.

    Deliberately excludes anything that would make two equivalent
    executions produce different keys: no artifact_id, no timestamp, no
    session_id. `upstream_identity` is intentionally the upstream's own
    ID (e.g. ingestion_id), not just its content hash, matching this
    project's existing manifest convention of recording explicit parent
    IDs rather than only their checksums — see app.catalog.models
    (RelationshipType) for the same distinction.
    """
    payload = {
        "stage": stage,
        "upstream_identity": upstream_identity,
        "upstream_content_sha256": upstream_content_sha256,
        "config_hash": config_hash,
        "implementation_version": implementation_version,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
