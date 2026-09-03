"""Forge Data CLI root (Design Requirement 1/2/3).

Every subcommand is a thin client: it either calls the application/service
layer directly (`app.runs`, `app.catalog`, `app.sensors`,
`app.storage.recovery`) or, for `forge serve`, starts the same FastAPI app
the HTTP API uses. No pipeline stage logic is implemented here.

Single-action commands (init/sensors/datasets/lineage/verify/doctor/
rebuild/serve/runs) are registered directly as root-level `@app.command(...)`s rather
than as one-callback sub-Typer groups -- a Click Group whose only action
is an `invoke_without_command=True` callback (no real subcommand) mis-
parses options placed after positional arguments (e.g. `forge init demo
--force` would fail to see `demo`); a real `@app.command()` doesn't have
that problem. `config`, `recover`, `dataset`, and `run` genuinely have
multiple named subcommands, so they're mounted as real sub-apps via
`add_typer`.
"""

from __future__ import annotations

import logging

import typer

from app.cli.config_cmd import app as config_app
from app.cli.datasets_cmd import dataset_app, datasets
from app.cli.doctor import doctor
from app.cli.init import init
from app.cli.lineage_cmd import lineage
from app.cli.rebuild_cmd import rebuild
from app.cli.recovery_cmd import app as recovery_app
from app.cli.run import app as run_app
from app.cli.runs_list import runs
from app.cli.sensors_cmd import sensors
from app.cli.serve import serve
from app.cli.verify_cmd import verify
from app.version import __version__

app = typer.Typer(name="forge", help="Forge Data -- local robotics/physical-AI data pipeline CLI.", no_args_is_help=True)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"forge {__version__}")
        raise typer.Exit()


@app.callback()
def main_callback(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", callback=_version_callback, is_eager=True, help="Show the Forge Data version and exit."),
) -> None:
    # Every command except `serve` runs a pipeline stage/service directly
    # in this process and reports failures through its own clean,
    # structured `print_error(...)` + exit-code path -- never through
    # stdlib logging. Without a handler here, a stage exception's
    # `logger.exception(...)` call (see app.runs.executor, RUN_STAGE_FAILED)
    # falls through to Python's default "handler of last resort", which
    # dumps a raw traceback to stderr ahead of that clean message. `serve`
    # is exempt: it lazily imports app.main, which calls
    # app.core.logging.configure_logging(...) to set up real, leveled,
    # stdout server logs -- and configure_logging no-ops if the root
    # logger already has a handler, so this must not run for `serve`.
    if ctx.invoked_subcommand != "serve":
        logging.getLogger().addHandler(logging.NullHandler())


app.command("init", help="Create a Forge Data workspace.")(init)
app.command("sensors", help="List registered sensor plugins.")(sensors)
app.command("datasets", help="List registered datasets.")(datasets)
app.command("lineage", help="Show an artifact's lineage graph.")(lineage)
app.command("verify", help="Verify an artifact's checksums/references.")(verify)
app.command("doctor", help="Diagnose this workspace.")(doctor)
app.command("rebuild", help="Rebuild the catalog's artifact index from disk (e.g. after a relocated workspace).")(rebuild)
app.command("serve", help="Start the Forge Data API + local GUI.")(serve)
app.command("runs", help="List recent pipeline runs.")(runs)

app.add_typer(config_app, name="config")
app.add_typer(run_app, name="run")
app.add_typer(dataset_app, name="dataset")
app.add_typer(recovery_app, name="recover")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
