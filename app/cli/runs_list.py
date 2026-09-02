"""`forge runs` -- list runs (Design Requirement 10/68)."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.table import Table

from app.cli.output import console, print_json
from app.cli.services import build_run_service
from app.cli.workspace import resolve_settings_or_exit

def runs(
    workspace: Optional[Path] = typer.Option(None, "--workspace"),
    status_filter: Optional[str] = typer.Option(None, "--status"),
    run_type: Optional[str] = typer.Option(None, "--run-type"),
    limit: int = typer.Option(20, "--limit", min=1, max=100),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List recent pipeline runs."""
    settings = resolve_settings_or_exit(workspace)
    run_service = build_run_service(settings)
    listing = run_service.list_runs(status=status_filter, run_type=run_type, limit=limit, offset=0)

    if as_json:
        print_json(listing)
        return

    table = Table(show_header=True, header_style="bold")
    for col in ("Run ID", "Type", "Status", "Stage", "Created"):
        table.add_column(col)
    for r in listing:
        table.add_row(r.run_id, r.run_type, r.status, r.current_stage or "-", r.created_at)
    console.print(table)
