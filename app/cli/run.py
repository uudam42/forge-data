"""`forge run <pipeline.yaml>` plus its `show`/`cancel`/`events`
sub-actions (Design Requirement 8/9/10).

`forge run <file>` and `forge run show <run_id>` share one command name
by design (matching the spec literally), which plain Typer/Click can't
resolve on its own -- a Click Group always tries to match the first
positional token against a registered subcommand name first, so
`forge run pipeline.yaml` would fail with "No such command
'pipeline.yaml'". `_DefaultCommandGroup` below is the standard fix: if the
first token isn't a known subcommand name, it's routed to the hidden
"exec" command instead.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Optional

import typer
from rich.live import Live
from rich.table import Table
from typer.core import TyperGroup

from app.cli.errors import CliError
from app.cli.output import console, print_error, print_json, stage_glyph
from app.cli.pipeline_config import PipelineConfigError, load_pipeline_config
from app.cli.services import build_run_results_service, build_run_service
from app.cli.workspace import build_settings_for_workspace, resolve_workspace
from app.runs.errors import InvalidPipelineConfigError, RunCapacityExceededError, RunNotFoundError
from app.runs.executor import StreamFile, plan_stages
from app.runs.local_executor import LocalRunExecutor
from app.sensors.base import SensorPluginNotFoundError
from app.sensors.registry import get_default_registry


class _DefaultCommandGroup(TyperGroup):
    """Routes an unrecognized first token to the "exec" command instead
    of raising "No such command" -- see module docstring."""

    default_command = "exec"

    def resolve_command(self, ctx, args):
        if args and args[0] not in self.commands and not args[0].startswith("-"):
            args = [self.default_command, *args]
        return super().resolve_command(ctx, args)


app = typer.Typer(cls=_DefaultCommandGroup, help="Run a pipeline (forge run <file>), or inspect/cancel an existing run.")

_WORKSPACE_OPTION = typer.Option(None, "--workspace", help="Workspace directory (default: resolved per Design Requirement 69)")


def _resolve_settings(workspace: Optional[Path]):
    ws = resolve_workspace(workspace)
    return build_settings_for_workspace(ws)


def _print_dry_run(config_path: Path) -> None:
    loaded = load_pipeline_config(config_path)
    registry = get_default_registry()

    table = Table(show_header=True, header_style="bold")
    table.add_column("Stream")
    table.add_column("Sensor type")
    table.add_column("Schema")
    for stream in loaded.streams:
        try:
            plugin = registry.get(stream.sensor_type)
        except SensorPluginNotFoundError as exc:
            raise CliError(str(exc)) from exc
        table.add_row(stream.path.name, stream.sensor_type, f"{plugin.normalization_profile.schema_name} v{plugin.schema_version}")

    console.print("[bold green]Pipeline valid[/bold green]")
    console.print()
    console.print(table)
    console.print()
    console.print("Stages:")
    for stage in plan_stages([s.sensor_type for s in loaded.streams]):
        console.print(f"  {stage}")
    console.print()
    console.print("No artifacts were created.")


def _render_stage_table(run: dict) -> Table:
    table = Table(show_header=False, box=None)
    for stage in run["stage_runs"]:
        line = f"{stage_glyph(stage['status'])} {stage['stage']}"
        if stage["status"] == "running" and stage.get("progress_fraction") is not None:
            line += f"  {stage['progress_fraction'] * 100:.0f}%"
        elif stage["status"] == "running" and stage.get("records_processed"):
            line += f"  {stage['records_processed']:,} records processed"
        table.add_row(line)
    return table


def _execute_pipeline(config_path: Path, *, dry_run: bool, workspace: Optional[Path], as_json: bool) -> None:
    if dry_run:
        _print_dry_run(config_path)
        return

    try:
        loaded = load_pipeline_config(config_path)
        settings = _resolve_settings(workspace)
    except (PipelineConfigError, CliError) as exc:
        print_error(str(exc))
        raise typer.Exit(code=1) from exc

    run_service = build_run_service(settings)
    try:
        run_service.validate_request(loaded.request)
    except InvalidPipelineConfigError as exc:
        print_error(str(exc))
        raise typer.Exit(code=1) from exc

    stream_files = []
    for stream, stream_cfg in zip(loaded.streams, loaded.request.streams):
        if not stream.path.is_file():
            print_error(f"input path does not exist: {stream.path}")
            raise typer.Exit(code=1)
        stream_files.append(
            StreamFile(sensor_type=stream.sensor_type, filename=stream.path.name, content_type="text/csv", stream=open(stream.path, "rb"), source_units=stream_cfg.source_units)
        )

    try:
        run_id = run_service.create_run(run_type="pipeline", request=loaded.request)
    except RunCapacityExceededError as exc:
        print_error(f"local run capacity exceeded ({exc.current}/{exc.limit} active runs) -- try again once a run finishes")
        raise typer.Exit(code=1) from exc

    executor = LocalRunExecutor(settings=settings)
    thread = threading.Thread(target=executor.run, args=(run_id, loaded.request, stream_files), daemon=True)
    thread.start()

    if as_json:
        # --json output must be pure JSON on stdout for scripting
        # (Design Requirement 68) -- no "Run <id>" banner, no Live
        # table, no other console output mixed in.
        thread.join()
        run = run_service.get_run(run_id).model_dump(mode="json")
        print_json(run)
        if run["status"] in ("failed", "cancelled"):
            raise typer.Exit(code=2)
        return

    console.print(f"Run [bold]{run_id}[/bold]")
    with Live(console=console, refresh_per_second=4) as live:
        while thread.is_alive():
            run = run_service.get_run(run_id).model_dump(mode="json")
            live.update(_render_stage_table(run))
            time.sleep(0.25)
        run = run_service.get_run(run_id).model_dump(mode="json")
        live.update(_render_stage_table(run))

    console.print()
    if run["status"] == "completed":
        console.print("[bold green]completed[/bold green]")
        from app.cli.services import build_catalog_service

        catalog_service = build_catalog_service(settings)
        results = build_run_results_service(settings, catalog_service).get_results(run_service.get_run(run_id))
        if results.package is not None:
            console.print(f"Package   {results.package.package_id}")
            console.print(f"Samples   {results.package.sample_count:,}")
            console.print(f"Output    {results.package.local_path}")
    elif run["status"] == "failed":
        console.print(f"[bold red]failed[/bold red] at stage {run.get('current_stage')}: {run.get('error_code')} -- {run.get('error_message')}")
    elif run["status"] == "cancelled":
        console.print("[dim]cancelled[/dim]")

    if run["status"] == "failed":
        raise typer.Exit(code=2)
    if run["status"] == "cancelled":
        raise typer.Exit(code=2)


@app.command("exec", hidden=True)
def exec_(
    pipeline_file: Path = typer.Argument(..., help="Pipeline YAML/JSON config to execute"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate and print the plan; create no run, write no artifacts"),
    workspace: Optional[Path] = _WORKSPACE_OPTION,
    as_json: bool = typer.Option(False, "--json", help="Machine-readable final output"),
) -> None:
    _execute_pipeline(pipeline_file, dry_run=dry_run, workspace=workspace, as_json=as_json)


@app.command("show")
def show(
    run_id: str,
    workspace: Optional[Path] = _WORKSPACE_OPTION,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Show a run's current status, stage list, and (if completed) result
    summary."""
    settings = _resolve_settings(workspace)
    run_service = build_run_service(settings)
    try:
        run = run_service.get_run(run_id)
    except RunNotFoundError as exc:
        print_error(str(exc))
        raise typer.Exit(code=3) from exc

    if as_json:
        print_json(run)
        return

    console.print(f"Run       {run.run_id}")
    console.print(f"Status    {run.status}")
    console.print(f"Stage     {run.current_stage or '-'}")
    console.print()
    console.print(_render_stage_table(run.model_dump(mode="json")))
    if run.status == "failed":
        console.print()
        console.print(f"[bold red]{run.error_code}[/bold red]: {run.error_message}")


@app.command("cancel")
def cancel(run_id: str, workspace: Optional[Path] = _WORKSPACE_OPTION, as_json: bool = typer.Option(False, "--json")) -> None:
    """Requests cooperative cancellation -- idempotent, non-blocking; the
    run reaches `cancelled` at its next safe stage boundary, not
    instantly (v2.6 semantics)."""
    settings = _resolve_settings(workspace)
    run_service = build_run_service(settings)
    try:
        run = run_service.request_cancel(run_id)
    except RunNotFoundError as exc:
        print_error(str(exc))
        raise typer.Exit(code=3) from exc

    if as_json:
        print_json(run)
    else:
        console.print(f"Cancellation requested for {run_id} (status: {run.status})")


@app.command("events")
def events(run_id: str, workspace: Optional[Path] = _WORKSPACE_OPTION, as_json: bool = typer.Option(False, "--json")) -> None:
    settings = _resolve_settings(workspace)
    run_service = build_run_service(settings)
    try:
        run_events = run_service.get_events(run_id)
    except RunNotFoundError as exc:
        print_error(str(exc))
        raise typer.Exit(code=3) from exc

    if as_json:
        print_json(run_events)
        return
    for event in run_events:
        console.print(f"{event.created_at}  {event.event_type}  {event.detail or ''}")
