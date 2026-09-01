"""Unit-level tests for dataset-version governance: explicit
active/deprecated/invalid state, the immutable (dataset, version) ->
package_id mapping, and effective-status derivation from upstream.
Direct repo/service construction (no full pipeline needed)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.catalog.repository import CatalogRepository
from app.catalog.scanner import CatalogScanner
from app.catalog.service import CatalogService
from app.catalog.serialization import canonical_json
from app.catalog.verifier import ArtifactVerifier
from app.core.config import Settings
from app.storage.catalog_store import get_connection


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        RAW_STORAGE_ROOT=tmp_path / "raw",
        VALIDATION_STORAGE_ROOT=tmp_path / "validation",
        INTEGRITY_STORAGE_ROOT=tmp_path / "integrity",
        NORMALIZED_STORAGE_ROOT=tmp_path / "normalized",
        SYNCHRONIZED_STORAGE_ROOT=tmp_path / "synchronized",
        CLEANED_STORAGE_ROOT=tmp_path / "cleaned",
        TRANSFORMED_STORAGE_ROOT=tmp_path / "transformed",
        QC_STORAGE_ROOT=tmp_path / "qc",
        PACKAGE_STORAGE_ROOT=tmp_path / "packages",
        CATALOG_DB_PATH=tmp_path / "catalog" / "catalog.db",
    )


def _service(settings: Settings) -> CatalogService:
    conn = get_connection(settings.CATALOG_DB_PATH)
    repo = CatalogRepository(conn)
    return CatalogService(repo=repo, scanner=CatalogScanner(settings), verifier=ArtifactVerifier(settings), settings=settings)


def _seed_accepted_package(service: CatalogService, package_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with service._repo.transaction():  # noqa: SLF001
        service._repo.upsert_artifact(
            {
                "artifact_type": "package", "artifact_id": package_id, "pipeline_stage": 9, "status": "completed",
                "storage_uri": f"data/packages/{package_id}", "content_sha256": "a" * 64,
                "manifest_uri": f"data/packages/{package_id}.manifest.json", "manifest_sha256": "b" * 64,
                "created_at": now, "session_id": None,
                "metadata_json": canonical_json({"source_qc_status": "passed"}), "registered_at": now,
            }
        )


def _seed_dataset_and_version(service: CatalogService, *, dataset_name: str, version: str, package_id: str) -> None:
    service.create_dataset(dataset_name=dataset_name, description=None, metadata={})
    service.register_version(dataset_name, version=version, package_id=package_id, description=None, tags=[])


def test_dataset_version_active_by_default(tmp_path: Path) -> None:
    service = _service(_settings(tmp_path))
    _seed_accepted_package(service, "pkg_1")
    _seed_dataset_and_version(service, dataset_name="ds1", version="1.0.0", package_id="pkg_1")
    gov = service.get_dataset_version_governance("ds1", "1.0.0")
    assert gov.state == "active"


def test_dataset_version_deprecated_and_invalid(tmp_path: Path) -> None:
    service = _service(_settings(tmp_path))
    _seed_accepted_package(service, "pkg_1")
    _seed_dataset_and_version(service, dataset_name="ds1", version="1.0.0", package_id="pkg_1")

    dep = service.set_dataset_version_governance("ds1", "1.0.0", new_state="deprecated", reason="superseded by 1.1.0")
    assert dep.state == "deprecated"
    response = service.get_version("ds1", "1.0.0")
    assert response.effective_status == "deprecated"  # explicit governance wins

    inv = service.set_dataset_version_governance("ds1", "1.0.0", new_state="invalid", reason="dataset found corrupted")
    assert inv.state == "invalid"
    response = service.get_version("ds1", "1.0.0")
    assert response.effective_status == "invalid"


def test_dataset_version_mapping_is_never_mutated_by_governance(tmp_path: Path) -> None:
    service = _service(_settings(tmp_path))
    _seed_accepted_package(service, "pkg_1")
    _seed_dataset_and_version(service, dataset_name="ds1", version="1.0.0", package_id="pkg_1")
    service.set_dataset_version_governance("ds1", "1.0.0", new_state="invalid", reason="bad data")

    response = service.get_version("ds1", "1.0.0")
    assert response.package_id == "pkg_1"  # governance never touches this


def test_effective_status_affected_from_invalid_upstream_artifact(tmp_path: Path) -> None:
    service = _service(_settings(tmp_path))
    _seed_accepted_package(service, "pkg_1")
    _seed_dataset_and_version(service, dataset_name="ds1", version="1.0.0", package_id="pkg_1")

    # No explicit version governance -- but the package ITSELF is invalid.
    service.set_artifact_governance("package", "pkg_1", new_state="invalid", reason="corrupted export")
    response = service.get_version("ds1", "1.0.0")
    assert response.effective_status == "affected"
    assert service.get_dataset_version_governance("ds1", "1.0.0").state == "active"  # no explicit row was ever written


def test_deprecated_upstream_alone_does_not_mark_version_affected(tmp_path: Path) -> None:
    """A deprecated ancestor doesn't retroactively affect an existing
    descendant's dataset version (Design Requirement 2: deprecated
    descendants remain historically intact)."""
    service = _service(_settings(tmp_path))
    _seed_accepted_package(service, "pkg_1")
    _seed_dataset_and_version(service, dataset_name="ds1", version="1.0.0", package_id="pkg_1")
    service.set_artifact_governance("package", "pkg_1", new_state="deprecated", reason="better package available now")
    response = service.get_version("ds1", "1.0.0")
    assert response.effective_status == "healthy"


def test_new_registration_blocked_on_invalid_package(tmp_path: Path) -> None:
    from app.catalog.errors import ArtifactInvalidError

    service = _service(_settings(tmp_path))
    _seed_accepted_package(service, "pkg_bad")
    service.create_dataset(dataset_name="ds2", description=None, metadata={})
    service.set_artifact_governance("package", "pkg_bad", new_state="invalid", reason="known bad export")

    with pytest.raises(ArtifactInvalidError) as excinfo:
        service.register_version("ds2", version="1.0.0", package_id="pkg_bad", description=None, tags=[])
    assert excinfo.value.artifact_id == "pkg_bad"


def test_new_registration_allowed_for_deprecated_package_with_override(tmp_path: Path) -> None:
    service = _service(_settings(tmp_path))
    _seed_accepted_package(service, "pkg_dep")
    service.create_dataset(dataset_name="ds3", description=None, metadata={})
    service.set_artifact_governance("package", "pkg_dep", new_state="deprecated", reason="an alternative exists")

    from app.catalog.errors import ArtifactDeprecatedError

    with pytest.raises(ArtifactDeprecatedError):
        service.register_version("ds3", version="1.0.0", package_id="pkg_dep", description=None, tags=[])

    response, created = service.register_version("ds3", version="1.0.0", package_id="pkg_dep", description=None, tags=[], allow_deprecated=True)
    assert created is True
    assert response.package_id == "pkg_dep"
