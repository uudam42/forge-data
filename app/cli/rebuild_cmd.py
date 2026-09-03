"""`forge rebuild --workspace <path>` -- thin CLI wrapper over the
existing v2.4 CatalogService.rebuild(), the same service call
`POST /api/v1/catalog/rebuild` makes (see app.api.routes.catalog). No
rebuild logic lives here; this only translates the call and its
structured errors into CLI output, the same pattern as `forge verify`/
`forge lineage`.

This is the documented recovery path (see docs/MIGRATION_V1_TO_V2.md,
"Relocated workspaces") for a workspace whose absolute path changed
since it was last scanned: an incremental scan refuses to silently
overwrite a registered artifact's manifest_uri when it now points
somewhere else (CatalogScanFailedError), but a full rebuild re-derives
the whole index from what's actually on disk and recovers cleanly."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from app.catalog.errors import (
    CatalogBusyError,
    CatalogLockFailedError,
    CatalogRebuildFailedError,
    CatalogRebuildInProgressError,
)
from app.cli.output import console, print_error, print_json
from app.cli.services import build_catalog_service
from app.cli.workspace import resolve_settings_or_exit

def rebuild(
    workspace: Optional[Path] = typer.Option(None, "--workspace"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Rebuilds the catalog's artifact index and lineage edges from the
    artifacts actually present on disk. Datasets, dataset versions, and
    all governance/run metadata are preserved untouched -- only the
    artifact/lineage index is reconstructed. Use this after relocating a
    workspace directory, or whenever `forge lineage`/`forge verify`/
    `forge dataset register` reports that a catalog scan failed."""
    settings = resolve_settings_or_exit(workspace)
    catalog_service = build_catalog_service(settings)
    try:
        result = catalog_service.rebuild()
    except CatalogRebuildInProgressError as exc:
        print_error(str(exc))
        raise typer.Exit(code=1) from exc
    except (CatalogBusyError, CatalogLockFailedError, CatalogRebuildFailedError) as exc:
        print_error(str(exc))
        raise typer.Exit(code=1) from exc

    if as_json:
        print_json(result)
        return

    console.print("[bold green]Catalog rebuild completed[/bold green]")
    console.print()
    console.print(f"Artifacts registered: {result.artifacts_registered}")
    console.print(f"Edges registered: {result.edges_registered}")
    console.print(f"Datasets preserved: {result.datasets_preserved}")
    console.print(f"Dataset versions preserved: {result.dataset_versions_preserved}")
    if result.issues:
        console.print()
        console.print(f"[yellow]{len(result.issues)} issue(s) found:[/yellow]")
        for issue in result.issues:
            console.print(f"  [yellow]![/yellow] {issue.artifact_type}/{issue.artifact_id}: {issue.issue_code}" + (f" -- {issue.detail}" if issue.detail else ""))
