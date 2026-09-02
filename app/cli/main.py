"""Forge Data CLI root (Design Requirement 1/2/3).

Every subcommand is a thin client: it either calls the application/service
layer directly (`app.runs`, `app.catalog`, `app.sensors`,
`app.storage.recovery`) or, for `forge serve`, starts the same FastAPI app
the HTTP API uses. No pipeline stage logic is implemented here.

Single-action commands (init/sensors/datasets/lineage/verify/doctor/serve/
runs) are registered directly as root-level `@app.command(...)`s rather
than as one-callback sub-Typer groups -- a Click Group whose only action
is an `invoke_without_command=True` callback (no real subcommand) mis-
parses options placed after positional arguments (e.g. `forge init demo
--force` would fail to see `demo`); a real `@app.command()` doesn't have
that problem. `config`, `recover`, `dataset`, and `run` genuinely have
multiple named subcommands, so they're mounted as real sub-apps via
`add_typer`.
"""

from __future__ import annotations

import typer

from app.cli.config_cmd import app as config_app
from app.cli.datasets_cmd import dataset_app, datasets
from app.cli.doctor import doctor
from app.cli.init import init
from app.cli.lineage_cmd import lineage
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
    version: bool = typer.Option(False, "--version", callback=_version_callback, is_eager=True, help="Show the Forge Data version and exit."),
) -> None:
    return


app.command("init", help="Create a Forge Data workspace.")(init)
app.command("sensors", help="List registered sensor plugins.")(sensors)
app.command("datasets", help="List registered datasets.")(datasets)
app.command("lineage", help="Show an artifact's lineage graph.")(lineage)
app.command("verify", help="Verify an artifact's checksums/references.")(verify)
app.command("doctor", help="Diagnose this workspace.")(doctor)
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
