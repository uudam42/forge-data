"""`forge datasets` / `forge dataset show|register` (Design Requirement 12)."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.table import Table

from app.cli.output import console, print_error, print_json
from app.cli.services import build_catalog_service
from app.cli.workspace import resolve_settings_or_exit
from app.catalog.errors import (
    ArtifactDeprecatedError,
    ArtifactInvalidError,
    CatalogScanFailedError,
    DatasetNotFoundError,
    DatasetVersionImmutableError,
    InvalidDatasetVersionError,
    PackageNotAcceptedError,
    PackageNotFoundError,
    UpstreamArtifactDeprecatedError,
    UpstreamArtifactInvalidError,
)

dataset_app = typer.Typer(help="Inspect or register a single dataset's versions.")


def datasets(workspace: Optional[Path] = typer.Option(None, "--workspace"), as_json: bool = typer.Option(False, "--json")) -> None:
    """List registered datasets."""
    settings = resolve_settings_or_exit(workspace)
    catalog_service = build_catalog_service(settings)
    listing = catalog_service.list_datasets()
    if as_json:
        print_json(listing)
        return
    table = Table(show_header=True, header_style="bold")
    for col in ("Name", "Versions", "Latest"):
        table.add_column(col)
    for d in listing:
        table.add_row(d.dataset_name, str(d.version_count), d.latest_version or "-")
    console.print(table)


@dataset_app.command("show")
def dataset_show(dataset_name: str, workspace: Optional[Path] = typer.Option(None, "--workspace"), as_json: bool = typer.Option(False, "--json")) -> None:
    settings = resolve_settings_or_exit(workspace)
    catalog_service = build_catalog_service(settings)
    try:
        versions = catalog_service.list_versions(dataset_name)
    except DatasetNotFoundError as exc:
        print_error(str(exc))
        raise typer.Exit(code=3) from exc

    if as_json:
        print_json(versions)
        return
    table = Table(show_header=True, header_style="bold")
    for col in ("Version", "Status", "Effective status", "Package"):
        table.add_column(col)
    for v in versions:
        table.add_row(v.version, v.status, v.effective_status, v.package_id)
    console.print(table)


@dataset_app.command("register")
def dataset_register(
    dataset_name: str,
    version: str = typer.Option(..., "--version"),
    package_id: str = typer.Option(..., "--package-id"),
    workspace: Optional[Path] = typer.Option(None, "--workspace"),
    allow_deprecated: bool = typer.Option(False, "--allow-deprecated"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Registers an existing completed package as a new dataset version.
    Never auto-picks or auto-increments the version -- always explicit."""
    settings = resolve_settings_or_exit(workspace)
    catalog_service = build_catalog_service(settings)
    try:
        catalog_service.create_dataset(dataset_name=dataset_name, description=None, metadata={})
        try:
            result, _created = catalog_service.register_version(
                dataset_name, version=version, package_id=package_id, description=None, tags=[], allow_deprecated=allow_deprecated
            )
        except PackageNotFoundError:
            # See app.runs.results for why: the artifact index is
            # populated by an explicit scan, not written live by each
            # stage -- scan once and retry before reporting "not found".
            catalog_service.scan()
            result, _created = catalog_service.register_version(
                dataset_name, version=version, package_id=package_id, description=None, tags=[], allow_deprecated=allow_deprecated
            )
    except (
        DatasetNotFoundError,
        PackageNotFoundError,
        InvalidDatasetVersionError,
        PackageNotAcceptedError,
        DatasetVersionImmutableError,
        ArtifactInvalidError,
        UpstreamArtifactInvalidError,
        ArtifactDeprecatedError,
        UpstreamArtifactDeprecatedError,
    ) as exc:
        print_error(str(exc))
        raise typer.Exit(code=1) from exc
    except CatalogScanFailedError as exc:
        # A relocated workspace can trip the registry's anti-silent-
        # overwrite guard (a stale absolute manifest_uri) -- see
        # docs/MIGRATION_V1_TO_V2.md. A full rebuild recovers; a plain
        # scan does not.
        print_error(f"{exc} -- a catalog rebuild may be required (see docs/MIGRATION_V1_TO_V2.md)")
        raise typer.Exit(code=1) from exc

    if as_json:
        print_json(result)
    else:
        console.print(f"[green]Registered[/green] {dataset_name} v{version} -> {package_id}")
