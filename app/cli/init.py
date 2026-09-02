"""`forge init` (Design Requirement 4/71)."""

from __future__ import annotations

from pathlib import Path

import typer

from app.cli.output import console, print_error
from app.cli.workspace import WORKSPACE_MARKER, workspace_data_roots


_FORGE_YAML = """\
# Forge Data workspace marker. Its presence (not its contents) is what
# `forge` uses to recognize this directory as a workspace -- see
# `app.cli.workspace.resolve_workspace`. Storage roots are always
# <this-directory>/data/<stage>; they are not configurable here on
# purpose (Design Requirement 4: no second storage hierarchy).
workspace_version: 1
"""

_WORKSPACE_README = """\
# Forge Data workspace

Created by `forge init`.

    forge doctor                          # check this workspace is healthy
    forge config validate pipelines/example.yaml
    forge run pipelines/example.yaml
    forge runs
    forge serve                           # http://127.0.0.1:8000

See `pipelines/example.yaml` for a minimal two-stream (IMU + GPS) pipeline
config using the tiny synthetic data in `input/`.
"""

_EXAMPLE_IMU_CSV = "timestamp,accel_x,accel_y,accel_z,gyro_x,gyro_y,gyro_z\n" + "".join(
    f"2026-01-01T00:00:{i:02d}Z,0.{i},0.2,9.8,0.01,0.02,0.03\n" for i in range(20)
)
_EXAMPLE_GPS_CSV = "timestamp,latitude,longitude,altitude,speed\n" + "".join(
    f"2026-01-01T00:00:{i:02d}Z,34.05{i:02d},-118.25{i:02d},100.0,5.{i}\n" for i in range(0, 20, 2)
)

_EXAMPLE_PIPELINE_YAML = """\
# Example Forge Data pipeline: IMU + GPS -> a packaged, split ML dataset.
# Run it with: forge run pipelines/example.yaml
#
# Every key below (besides streams[].path) is a direct field of
# PipelineRunRequest / its nested per-stage configs in app/runs/models.py
# -- see docs/CLI.md for the full field reference.

streams:
  - sensor_type: imu
    path: ../input/imu.csv
    source_units:
      acceleration: m/s^2
      angular_velocity: rad/s
  - sensor_type: gps
    path: ../input/gps.csv
    source_units:
      altitude: m
      speed: m/s

synchronization:
  reference:
    mode: stream
    stream: imu
  alignment:
    default_method: nearest
    max_time_delta_ms: 500

cleaning:
  policy_name: default_multimodal
  policy_version: "1.0.0"
  config:
    required_streams: [imu]

transformation:
  profile_name: multimodal_window_v1
  profile_version: "1.0.0"
  config:
    window:
      mode: count
      size: 5
      stride: 5
      drop_incomplete: true

qc:
  profile_name: default_dataset_qc
  profile_version: "1.0.0"
  config:
    minimum_samples: 1

packaging:
  profile_name: default_ml_package
  profile_version: "1.0.0"
  config:
    # This example's synthetic input is tiny (20 rows) specifically so
    # `forge run` finishes instantly -- too few groups for a meaningful
    # 3-way split, so it all goes to `train` here. On real data, set
    # validation_ratio/test_ratio > 0 for an actual train/val/test split.
    split:
      strategy: group_hash
      train_ratio: 1.0
      validation_ratio: 0.0
      test_ratio: 0.0
      seed: 42
    grouping:
      mode: source_overlap
    exports: [jsonl]
"""


def init(
    directory: Path = typer.Argument(..., help="Directory to create the workspace in"),
    force: bool = typer.Option(False, "--force", help="Initialize even if the directory already exists and is non-empty"),
) -> None:
    """Create a usable local Forge Data workspace: forge.yaml, data/
    storage roots, an example pipeline config, and tiny synthetic input
    data. Idempotent when the directory is already an initialized,
    empty-of-conflicts workspace; fails clearly on a non-empty,
    non-workspace directory unless --force is given."""
    directory = directory.resolve()

    if directory.exists() and any(directory.iterdir()) and not force:
        marker = directory / WORKSPACE_MARKER
        if not marker.is_file():
            print_error(f"'{directory}' already exists and is not empty. Pass --force to initialize it anyway.")
            raise typer.Exit(code=1)

    directory.mkdir(parents=True, exist_ok=True)
    (directory / WORKSPACE_MARKER).write_text(_FORGE_YAML, encoding="utf-8")
    (directory / "README.md").write_text(_WORKSPACE_README, encoding="utf-8")

    for root in workspace_data_roots(directory).values():
        target = root if root.suffix == "" else root.parent
        target.mkdir(parents=True, exist_ok=True)

    pipelines_dir = directory / "pipelines"
    pipelines_dir.mkdir(exist_ok=True)
    (pipelines_dir / "example.yaml").write_text(_EXAMPLE_PIPELINE_YAML, encoding="utf-8")

    input_dir = directory / "input"
    input_dir.mkdir(exist_ok=True)
    (input_dir / "imu.csv").write_text(_EXAMPLE_IMU_CSV, encoding="utf-8")
    (input_dir / "gps.csv").write_text(_EXAMPLE_GPS_CSV, encoding="utf-8")

    console.print(f"[green]Initialized Forge Data workspace at[/green] {directory}")
    console.print()
    console.print("Next:")
    console.print(f"  cd {directory}")
    console.print("  forge doctor")
    console.print("  forge run pipelines/example.yaml")
