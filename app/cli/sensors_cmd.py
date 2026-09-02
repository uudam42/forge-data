"""`forge sensors` (Design Requirement 11) -- reads the v2.3
SensorPluginRegistry directly; no hard-coded sensor list."""

from __future__ import annotations

import typer
from rich.table import Table

from app.cli.output import console, print_json
from app.sensors.registry import get_default_registry

def sensors(as_json: bool = typer.Option(False, "--json")) -> None:
    """List registered sensor plugins."""
    plugins = get_default_registry().list_plugins()
    if as_json:
        print_json(
            [
                {
                    "sensor_type": p.sensor_type,
                    "schema_name": p.normalization_profile.schema_name,
                    "schema_version": p.schema_version,
                    "normalization_profile": p.normalization_profile.profile_name,
                    "required_fields": list(p.required_fields),
                }
                for p in plugins
            ]
        )
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Type")
    table.add_column("Schema")
    table.add_column("Normalization profile")
    table.add_column("Required fields")
    for p in plugins:
        table.add_row(p.sensor_type, f"{p.normalization_profile.schema_name} v{p.schema_version}", p.normalization_profile.profile_name, ", ".join(p.required_fields))
    console.print(table)
