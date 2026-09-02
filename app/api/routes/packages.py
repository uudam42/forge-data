"""HTTP layer for local, host-filesystem actions on a package (v2.7,
Design Requirement 38/61).

Forge Data is local-first, so "show me the file" is a legitimate GUI
action -- but the browser can never be trusted to name an arbitrary path.
`POST /{package_id}/open-folder` accepts only a catalog-known package_id,
resolves its directory itself via the catalog (never from a client-
supplied path string), and refuses to proceed unless that resolved,
`.resolve()`d directory is actually inside the configured
`PACKAGE_STORAGE_ROOT`. The platform file-manager command is invoked as a
fixed argv list -- never `shell=True`, never string-built from anything
client-controlled.

Status codes:
- 200: the folder was opened (or a best-effort open command was issued)
- 404: no package artifact with that id
- 501: no supported way to open a folder on this platform
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.catalog.repository import CatalogRepository
from app.core.config import Settings, get_settings
from app.storage.catalog_store import get_connection

router = APIRouter(prefix="/api/v1/packages", tags=["packages"])


def get_catalog_repository(settings: Settings = Depends(get_settings)) -> CatalogRepository:
    conn = get_connection(settings.CATALOG_DB_PATH, busy_timeout_ms=settings.CATALOG_BUSY_TIMEOUT_MS, journal_mode=settings.CATALOG_JOURNAL_MODE)
    return CatalogRepository(conn, db_path=str(settings.CATALOG_DB_PATH), busy_timeout_ms=settings.CATALOG_BUSY_TIMEOUT_MS)


class OpenFolderResponse(BaseModel):
    opened: bool
    path: str


def _package_dir(repo: CatalogRepository, package_id: str) -> Path:
    row = repo.get_artifact("package", package_id)
    if row is None or not row.get("manifest_uri"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No package artifact '{package_id}'")
    manifest_uri = row["manifest_uri"]
    manifest_path = Path(manifest_uri[len("file://") :] if manifest_uri.startswith("file://") else manifest_uri)
    return manifest_path.parent


def _resolve_within_package_root(package_dir: Path, settings: Settings) -> Path:
    resolved = package_dir.resolve()
    root = settings.PACKAGE_STORAGE_ROOT.resolve()
    if root not in resolved.parents and resolved != root:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resolved path is outside the package storage root")
    if not resolved.is_dir():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Package directory no longer exists on disk")
    return resolved


def _open_in_file_manager(path: Path) -> bool:
    if sys.platform == "darwin":
        argv = ["open", str(path)]
    elif sys.platform.startswith("linux"):
        argv = ["xdg-open", str(path)]
    elif sys.platform == "win32":
        argv = ["explorer", str(path)]
    else:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=f"Open-folder is not supported on platform '{sys.platform}'")
    result = subprocess.run(argv, capture_output=True, timeout=10, check=False)  # noqa: S603 -- fixed argv, no shell, path is a server-resolved catalog path
    return result.returncode == 0


@router.post("/{package_id}/open-folder", response_model=OpenFolderResponse, status_code=status.HTTP_200_OK)
async def open_package_folder(
    package_id: str, settings: Settings = Depends(get_settings), repo: CatalogRepository = Depends(get_catalog_repository)
) -> OpenFolderResponse:
    package_dir = _package_dir(repo, package_id)
    resolved = _resolve_within_package_root(package_dir, settings)
    opened = _open_in_file_manager(resolved)
    return OpenFolderResponse(opened=opened, path=str(resolved))
