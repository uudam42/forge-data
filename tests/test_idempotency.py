"""Unit tests for the standalone idempotency-key infrastructure
(app.storage.idempotency). Not wired into any live service in v2.1 —
see the module docstring — so these tests exercise the function
directly rather than through the API."""

from __future__ import annotations

from app.storage.idempotency import execution_key


def _base(**overrides) -> dict:
    payload = dict(
        stage="normalization",
        upstream_identity="ing_a",
        upstream_content_sha256="a" * 64,
        config_hash="cfg1",
        implementation_version="1.0.0",
    )
    payload.update(overrides)
    return payload


def test_execution_key_is_deterministic() -> None:
    k1 = execution_key(**_base())
    k2 = execution_key(**_base())
    assert k1 == k2
    assert len(k1) == 64  # sha256 hex digest


def test_execution_key_changes_with_upstream_identity() -> None:
    assert execution_key(**_base()) != execution_key(**_base(upstream_identity="ing_b"))


def test_execution_key_changes_with_content_checksum() -> None:
    assert execution_key(**_base()) != execution_key(**_base(upstream_content_sha256="b" * 64))


def test_execution_key_changes_with_config_hash() -> None:
    assert execution_key(**_base()) != execution_key(**_base(config_hash="cfg2"))


def test_execution_key_changes_with_stage() -> None:
    assert execution_key(**_base()) != execution_key(**_base(stage="cleaning"))


def test_execution_key_changes_with_implementation_version() -> None:
    assert execution_key(**_base()) != execution_key(**_base(implementation_version="2.0.0"))


def test_execution_key_never_incorporates_a_random_execution_id() -> None:
    """The whole point of the key: it must be computable from content and
    config alone, never from a randomly generated artifact_id -- there is
    no artifact_id parameter to this function at all."""
    import inspect

    params = inspect.signature(execution_key).parameters
    assert "artifact_id" not in params
    assert "created_at" not in params
