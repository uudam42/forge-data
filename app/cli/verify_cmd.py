"""`forge verify <artifact_type> <artifact_id>` (Design Requirement 14) --
thin wrapper over the existing ArtifactVerifier/CatalogService.verify."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from app.catalog.errors import ArtifactNotFoundError, CatalogScanFailedError, InvalidArtifactTypeError
from app.cli.output import console, print_error, print_json
from app.cli.services import build_catalog_service
from app.cli.workspace import resolve_settings_or_exit

def verify(
    artifact_type: str,
    artifact_id: str,
    recursive: bool = typer.Option(False, "--recursive"),
    workspace: Optional[Path] = typer.Option(None, "--workspace"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    settings = resolve_settings_or_exit(workspace)
    catalog_service = build_catalog_service(settings)
    try:
        result = catalog_service.verify(artifact_type, artifact_id, recursive=recursive)
    except ArtifactNotFoundError:
        # The catalog's artifact index is populated by an explicit scan
        # (see app.runs.results for the same pattern) -- a real, just-
        # produced artifact can legitimately not be indexed yet. Scan
        # once and retry before reporting "not found".
        try:
            catalog_service.scan()
        except CatalogScanFailedError as exc:
            # A relocated workspace can trip the registry's anti-silent-
            # overwrite guard (a stale absolute manifest_uri) -- see
            # docs/MIGRATION_V1_TO_V2.md. A full rebuild recovers; a plain
            # scan does not. Report this clearly rather than a traceback.
            print_error(f"{exc} -- a catalog rebuild may be required (see docs/MIGRATION_V1_TO_V2.md)")
            raise typer.Exit(code=1) from exc
        try:
            result = catalog_service.verify(artifact_type, artifact_id, recursive=recursive)
        except (InvalidArtifactTypeError, ArtifactNotFoundError) as exc:
            print_error(str(exc))
            raise typer.Exit(code=3) from exc
    except InvalidArtifactTypeError as exc:
        print_error(str(exc))
        raise typer.Exit(code=3) from exc

    if as_json:
        print_json(result)
        return

    console.print(f"[bold]{artifact_type}/{artifact_id}[/bold]  status={result.status}")

    def _print_checks(checks, indent: str = "  ") -> None:
        for check in checks:
            check_icon = "✓" if check.status == "verified" else "✗"
            console.print(f"{indent}{check_icon} {check.name}" + (f" -- {check.detail}" if check.detail else ""))

    if result.nodes is not None:
        for node in result.nodes:
            icon = "[green]OK[/green]" if node.status == "verified" else "[bold red]FAIL[/bold red]"
            console.print(f"  {icon}  {node.artifact_type}/{node.artifact_id}")
            _print_checks(node.checks, indent="      ")
    else:
        _print_checks(result.checks)

    if result.status != "verified":
        raise typer.Exit(code=1)
