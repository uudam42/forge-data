"""Shared pytest fixtures.

All fixtures route storage through a pytest tmp_path so tests never touch
the real data/raw directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.main import app


@pytest.fixture
def storage_root(tmp_path: Path) -> Path:
    return tmp_path / "raw"


@pytest.fixture
def schema_dir() -> Path:
    """The project's real built-in schemas — read-only in tests.

    Resolved the same way the application itself resolves them
    (`app.core.config._default_schema_dir`), so tests exercise the real
    packaged-resource lookup rather than a test-only shortcut.
    """
    from app.core.config import _default_schema_dir

    return _default_schema_dir()


@pytest.fixture
def validation_root(tmp_path: Path) -> Path:
    return tmp_path / "validation"


@pytest.fixture
def integrity_root(tmp_path: Path) -> Path:
    return tmp_path / "integrity"


@pytest.fixture
def normalized_root(tmp_path: Path) -> Path:
    return tmp_path / "normalized"


@pytest.fixture
def synchronized_root(tmp_path: Path) -> Path:
    return tmp_path / "synchronized"


@pytest.fixture
def cleaned_root(tmp_path: Path) -> Path:
    return tmp_path / "cleaned"


@pytest.fixture
def transformed_root(tmp_path: Path) -> Path:
    return tmp_path / "transformed"


@pytest.fixture
def qc_root(tmp_path: Path) -> Path:
    return tmp_path / "qc"


@pytest.fixture
def package_root(tmp_path: Path) -> Path:
    return tmp_path / "packages"


@pytest.fixture
def catalog_db_path(tmp_path: Path) -> Path:
    return tmp_path / "catalog" / "catalog.db"


@pytest.fixture
def test_settings(
    storage_root: Path,
    schema_dir: Path,
    validation_root: Path,
    integrity_root: Path,
    normalized_root: Path,
    synchronized_root: Path,
    cleaned_root: Path,
    transformed_root: Path,
    qc_root: Path,
    package_root: Path,
    catalog_db_path: Path,
) -> Settings:
    return Settings(
        RAW_STORAGE_ROOT=storage_root,
        MAX_UPLOAD_SIZE_MB=1,
        SCHEMA_DIR=schema_dir,
        VALIDATION_STORAGE_ROOT=validation_root,
        MAX_VALIDATION_ERRORS=1000,
        INTEGRITY_STORAGE_ROOT=integrity_root,
        MAX_INTEGRITY_ISSUES=1000,
        NORMALIZED_STORAGE_ROOT=normalized_root,
        SYNCHRONIZED_STORAGE_ROOT=synchronized_root,
        CLEANED_STORAGE_ROOT=cleaned_root,
        TRANSFORMED_STORAGE_ROOT=transformed_root,
        QC_STORAGE_ROOT=qc_root,
        PACKAGE_STORAGE_ROOT=package_root,
        CATALOG_DB_PATH=catalog_db_path,
    )


@pytest.fixture
def client(test_settings: Settings):
    app.dependency_overrides[get_settings] = lambda: test_settings
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
