"""`forge config validate` (Design Requirement 6)."""

from __future__ import annotations

import os
from pathlib import Path

import typer

from app.cli.output import console, print_error, print_json
from app.cli.pipeline_config import PipelineConfigError, load_pipeline_config
from app.sensors.base import SensorPluginNotFoundError
from app.sensors.registry import get_default_registry

def _validate(config_path: Path) -> list[str]:
    """Returns a list of problems; empty means valid. Raises
    PipelineConfigError for structural problems (bad YAML/JSON, doesn't
    match PipelineRunRequest at all) since those make the remaining
    semantic checks meaningless."""
    loaded = load_pipeline_config(config_path)
    problems: list[str] = []
    registry = get_default_registry()

    for stream in loaded.streams:
        if not stream.path.is_file():
            problems.append(f"stream '{stream.sensor_type}': input path does not exist: {stream.path}")
        try:
            registry.get(stream.sensor_type)
        except SensorPluginNotFoundError:
            known = ", ".join(p.sensor_type for p in registry.list_plugins())
            problems.append(f"stream '{stream.sensor_type}': no registered sensor plugin (known: {known or 'none'})")

    split = loaded.request.packaging.config.split
    ratio_sum = split.train_ratio + split.validation_ratio + split.test_ratio
    if abs(ratio_sum - 1.0) > 1e-6:
        problems.append(f"packaging.config.split ratios sum to {ratio_sum:.4f}, not 1.0 (train={split.train_ratio}, validation={split.validation_ratio}, test={split.test_ratio})")

    if not os.access(config_path.parent, os.W_OK):
        problems.append(f"config file directory is not writable: {config_path.parent}")

    return problems


app = typer.Typer(help="Validate a pipeline config file without running it.")


@app.command("validate")
def validate(
    config_file: Path = typer.Argument(..., help="Path to a pipeline YAML/JSON config"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
) -> None:
    """Validates a pipeline config: syntax, PipelineRunRequest structure,
    input file existence, sensor plugin registration, and split ratios.
    Never executes any stage."""
    try:
        problems = _validate(config_file)
    except PipelineConfigError as exc:
        if as_json:
            print_json({"valid": False, "errors": [str(exc)]})
        else:
            print_error(str(exc))
        raise typer.Exit(code=1) from exc

    if as_json:
        print_json({"valid": not problems, "errors": problems})
    elif problems:
        console.print(f"[bold red]Invalid[/bold red] {config_file}")
        for p in problems:
            console.print(f"  - {p}")
    else:
        console.print(f"[bold green]Valid[/bold green] {config_file}")

    if problems:
        raise typer.Exit(code=1)
