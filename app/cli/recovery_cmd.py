"""`forge recover scan|cleanup` (Design Requirement 15) -- thin wrapper
over the existing v2.1 RecoveryService. Cleanup stays conservative:
requires --yes, and only ever removes entries the scan itself classified
STALE (never ACTIVE, never INVALID_STAGING_ENTRY)."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.table import Table

from app.cli.output import console, print_json
from app.cli.services import build_recovery_service
from app.cli.workspace import resolve_settings_or_exit

app = typer.Typer(help="Crash-recovery staging scan/cleanup.")


@app.command("scan")
def scan(workspace: Optional[Path] = typer.Option(None, "--workspace"), as_json: bool = typer.Option(False, "--json")) -> None:
    settings = resolve_settings_or_exit(workspace)
    result = build_recovery_service(settings).scan()
    if as_json:
        print_json(result)
        return
    console.print(f"active={result.active_count}  stale={result.stale_count}  total={len(result.entries)}")
    table = Table(show_header=True, header_style="bold")
    for col in ("Classification", "Stage", "Artifact", "Reason"):
        table.add_column(col)
    for e in result.entries:
        table.add_row(e.classification, e.stage, e.artifact_id or "-", e.reason)
    console.print(table)


@app.command("cleanup")
def cleanup(
    workspace: Optional[Path] = typer.Option(None, "--workspace"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    yes: bool = typer.Option(False, "--yes", help="Required to actually delete anything"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    settings = resolve_settings_or_exit(workspace)
    service = build_recovery_service(settings)

    if not dry_run and not yes:
        console.print("[yellow]This removes every currently-STALE staging entry. Re-run with --yes to proceed, or --dry-run to preview.[/yellow]")
        raise typer.Exit(code=1)

    removed = service.cleanup_stale(dry_run=dry_run)
    if as_json:
        print_json(removed)
        return
    verb = "Would remove" if dry_run else "Removed"
    console.print(f"{verb} {len(removed)} stale staging entr{'y' if len(removed) == 1 else 'ies'}")
    for e in removed:
        console.print(f"  {e.stage}/{e.artifact_id}")
