"""The `forge` CLI (v2.7, Design Requirements 1-17, 67-70).

Every command is invoked in-process via Typer's CliRunner against
`app.cli.main.app` -- never a real subprocess -- and every test passes an
explicit `--workspace <tmp_path>` (after `forge init`ing it) so nothing
here ever reads or writes the real repository `data/` directory,
matching the same test-isolation invariant every other test file in this
suite follows via `tmp_path`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from typer.testing import CliRunner

from app.cli.main import app
from app.version import __version__

runner = CliRunner()

IMU_CSV = "timestamp,accel_x,accel_y,accel_z,gyro_x,gyro_y,gyro_z\n" + "".join(
    f"2026-08-30T18:{i//60:02d}:{i%60:02d}Z,0.{i%10},0.2,9.8,0.01,0.02,0.03\n" for i in range(40)
)
GPS_CSV = "timestamp,latitude,longitude,altitude,speed\n" + "".join(
    f"2026-08-30T18:{i//60:02d}:{i%60:02d}Z,34.02{i%90:02d},-118.28{i%90:02d},100.0,9.{i%9}\n" for i in range(0, 40, 3)
)

_PIPELINE_CONFIG: dict = {
    "streams": [
        {"sensor_type": "imu", "path": "imu.csv", "source_units": {"acceleration": "m/s^2", "angular_velocity": "rad/s"}},
        {"sensor_type": "gps", "path": "gps.csv", "source_units": {"altitude": "m", "speed": "m/s"}},
    ],
    "synchronization": {"reference": {"mode": "stream", "stream": "imu"}, "alignment": {"default_method": "nearest", "max_time_delta_ms": 400}},
    "cleaning": {"policy_name": "default_multimodal", "policy_version": "1.0.0", "config": {"required_streams": ["imu"]}},
    "transformation": {"profile_name": "multimodal_window_v1", "profile_version": "1.0.0", "config": {"window": {"mode": "count", "size": 10, "stride": 10, "drop_incomplete": True}}},
    "qc": {"profile_name": "default_dataset_qc", "profile_version": "1.0.0", "config": {"minimum_samples": 1}},
    "packaging": {
        "profile_name": "default_ml_package", "profile_version": "1.0.0",
        "config": {"split": {"strategy": "group_hash", "train_ratio": 1.0, "validation_ratio": 0.0, "test_ratio": 0.0, "seed": 1}, "grouping": {"mode": "source_overlap"}, "exports": ["jsonl"]},
    },
}


def _init_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    result = runner.invoke(app, ["init", str(ws)])
    assert result.exit_code == 0, result.output
    return ws


def _write_pipeline_config(directory: Path, *, overrides: dict | None = None) -> Path:
    config = json.loads(json.dumps(_PIPELINE_CONFIG))
    if overrides:
        config.update(overrides)
    (directory / "imu.csv").write_text(IMU_CSV)
    (directory / "gps.csv").write_text(GPS_CSV)
    config_path = directory / "pipeline.yaml"
    config_path.write_text(yaml.safe_dump(config))
    return config_path


# --- 1-3: core / version ----------------------------------------------------


def test_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "forge" in result.output.lower()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


# --- 4-5: init / workspace resolution ---------------------------------------


def test_init_creates_workspace(tmp_path: Path) -> None:
    ws = _init_workspace(tmp_path)
    assert (ws / "forge.yaml").is_file()
    assert (ws / "pipelines" / "example.yaml").is_file()
    assert (ws / "input" / "imu.csv").is_file()
    assert (ws / "input" / "gps.csv").is_file()


def test_init_nonempty_directory_without_force_fails(tmp_path: Path) -> None:
    target = tmp_path / "occupied"
    target.mkdir()
    (target / "unrelated.txt").write_text("hello")
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code != 0


def test_init_nonempty_directory_with_force_succeeds(tmp_path: Path) -> None:
    target = tmp_path / "occupied"
    target.mkdir()
    (target / "unrelated.txt").write_text("hello")
    result = runner.invoke(app, ["init", str(target), "--force"])
    assert result.exit_code == 0
    assert (target / "forge.yaml").is_file()
    assert (target / "unrelated.txt").is_file()  # pre-existing content untouched


def test_workspace_resolution_via_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = _init_workspace(tmp_path)
    monkeypatch.setenv("FORGE_WORKSPACE", str(ws))
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    # Rich word-wraps long paths across lines in the CliRunner's captured
    # (narrow) output width -- strip all whitespace before comparing so a
    # wrap point inside the path doesn't break the match.
    assert str(ws.resolve()).replace(" ", "") in result.output.replace("\n", "").replace(" ", "")


def test_workspace_resolution_fails_with_no_marker_and_no_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = tmp_path / "not-a-workspace"
    empty.mkdir()
    monkeypatch.delenv("FORGE_WORKSPACE", raising=False)
    monkeypatch.chdir(empty)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code != 0
    assert "forge init" in result.output


# --- 6-7: config parsing / relative-path resolution / validation -----------


def test_config_relative_paths_resolve_against_config_file_directory(tmp_path: Path) -> None:
    """Design Requirement 70: a config's `streams[].path` is relative to
    the CONFIG FILE's own directory, not the workspace root or cwd --
    proven here by putting the config in a directory that is neither."""
    ws = _init_workspace(tmp_path)
    configs_dir = tmp_path / "elsewhere"
    configs_dir.mkdir()
    config_path = _write_pipeline_config(configs_dir)

    result = runner.invoke(app, ["config", "validate", str(config_path), "--json"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["valid"] is True, body


def test_config_validate_success(tmp_path: Path) -> None:
    config_path = _write_pipeline_config(tmp_path)
    result = runner.invoke(app, ["config", "validate", str(config_path)])
    assert result.exit_code == 0, result.output
    assert "Valid" in result.output


def test_config_validate_failure_unknown_sensor(tmp_path: Path) -> None:
    config_path = _write_pipeline_config(tmp_path, overrides={"streams": [{"sensor_type": "does_not_exist", "path": "imu.csv", "source_units": {}}]})
    result = runner.invoke(app, ["config", "validate", str(config_path), "--json"])
    assert result.exit_code != 0
    body = json.loads(result.output)
    assert body["valid"] is False
    assert any("does_not_exist" in e for e in body["errors"])


def test_config_validate_failure_bad_split_ratios(tmp_path: Path) -> None:
    config = json.loads(json.dumps(_PIPELINE_CONFIG))
    config["packaging"]["config"]["split"]["train_ratio"] = 0.5  # sums to 0.5, not 1.0
    (tmp_path / "imu.csv").write_text(IMU_CSV)
    (tmp_path / "gps.csv").write_text(GPS_CSV)
    config_path = tmp_path / "pipeline.yaml"
    config_path.write_text(yaml.safe_dump(config))

    result = runner.invoke(app, ["config", "validate", str(config_path), "--json"])
    assert result.exit_code != 0
    body = json.loads(result.output)
    assert any("sum to" in e for e in body["errors"])


# --- 8: dry-run --------------------------------------------------------------


def test_dry_run_creates_no_run_and_no_artifacts(tmp_path: Path) -> None:
    ws = _init_workspace(tmp_path)
    config_path = _write_pipeline_config(tmp_path)

    result = runner.invoke(app, ["run", str(config_path), "--dry-run", "--workspace", str(ws)])
    assert result.exit_code == 0, result.output
    assert "No artifacts were created" in result.output

    runs_result = runner.invoke(app, ["runs", "--workspace", str(ws), "--json"])
    assert json.loads(runs_result.output) == []
    assert not any((ws / "data" / "packages").iterdir())


# --- 9-10: forge run / show / cancel / events -------------------------------


def test_run_successful(tmp_path: Path) -> None:
    ws = _init_workspace(tmp_path)
    config_path = _write_pipeline_config(tmp_path)

    result = runner.invoke(app, ["run", str(config_path), "--workspace", str(ws), "--json"])
    assert result.exit_code == 0, result.output
    run = json.loads(result.output)
    assert run["status"] == "completed"
    assert any(a["artifact_type"] == "package" for a in run["artifacts"])


def test_run_failed_exits_nonzero_and_reports_structured_error(tmp_path: Path) -> None:
    ws = _init_workspace(tmp_path)
    config_path = _write_pipeline_config(tmp_path, overrides={"cleaning": {"policy_name": "does_not_exist", "policy_version": "1.0.0", "config": {}}})

    result = runner.invoke(app, ["run", str(config_path), "--workspace", str(ws), "--json"])
    assert result.exit_code == 2
    run = json.loads(result.output)
    assert run["status"] == "failed"
    assert run["error_code"]


def test_runs_lists_recent_runs(tmp_path: Path) -> None:
    ws = _init_workspace(tmp_path)
    config_path = _write_pipeline_config(tmp_path)
    runner.invoke(app, ["run", str(config_path), "--workspace", str(ws), "--json"])

    result = runner.invoke(app, ["runs", "--workspace", str(ws), "--json"])
    assert result.exit_code == 0
    listing = json.loads(result.output)
    assert len(listing) == 1
    assert listing[0]["status"] == "completed"


def test_run_show_known_and_unknown_run(tmp_path: Path) -> None:
    ws = _init_workspace(tmp_path)
    config_path = _write_pipeline_config(tmp_path)
    run = json.loads(runner.invoke(app, ["run", str(config_path), "--workspace", str(ws), "--json"]).output)

    ok = runner.invoke(app, ["run", "show", run["run_id"], "--workspace", str(ws)])
    assert ok.exit_code == 0
    assert run["run_id"] in ok.output

    missing = runner.invoke(app, ["run", "show", "run_does_not_exist", "--workspace", str(ws)])
    assert missing.exit_code == 3


def test_run_cancel_idempotent_on_finished_run_and_unknown_run(tmp_path: Path) -> None:
    ws = _init_workspace(tmp_path)
    config_path = _write_pipeline_config(tmp_path)
    run = json.loads(runner.invoke(app, ["run", str(config_path), "--workspace", str(ws), "--json"]).output)

    result = runner.invoke(app, ["run", "cancel", run["run_id"], "--workspace", str(ws), "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output)["status"] == "completed"  # already finished -- cancel is a no-op, not an error

    missing = runner.invoke(app, ["run", "cancel", "run_does_not_exist", "--workspace", str(ws)])
    assert missing.exit_code == 3


def test_run_events(tmp_path: Path) -> None:
    ws = _init_workspace(tmp_path)
    config_path = _write_pipeline_config(tmp_path)
    run = json.loads(runner.invoke(app, ["run", str(config_path), "--workspace", str(ws), "--json"]).output)

    result = runner.invoke(app, ["run", "events", run["run_id"], "--workspace", str(ws), "--json"])
    assert result.exit_code == 0
    events = json.loads(result.output)
    assert any(e["event_type"] == "RUN_CREATED" for e in events)
    assert any(e["event_type"] == "RUN_COMPLETED" for e in events)


# --- 11-13: sensors / datasets / lineage ------------------------------------


def test_sensors(tmp_path: Path) -> None:
    result = runner.invoke(app, ["sensors", "--json"])
    assert result.exit_code == 0
    types = {p["sensor_type"] for p in json.loads(result.output)}
    assert {"imu", "gps", "force_torque"} <= types


def test_datasets_show_and_register(tmp_path: Path) -> None:
    ws = _init_workspace(tmp_path)
    config_path = _write_pipeline_config(tmp_path)
    run = json.loads(runner.invoke(app, ["run", str(config_path), "--workspace", str(ws), "--json"]).output)
    package_id = next(a["artifact_id"] for a in run["artifacts"] if a["artifact_type"] == "package")

    empty = runner.invoke(app, ["datasets", "--workspace", str(ws), "--json"])
    assert json.loads(empty.output) == []

    register = runner.invoke(
        app, ["dataset", "register", "robotics-demo", "--version", "1.0.0", "--package-id", package_id, "--workspace", str(ws)]
    )
    assert register.exit_code == 0, register.output

    listing = json.loads(runner.invoke(app, ["datasets", "--workspace", str(ws), "--json"]).output)
    assert listing[0]["dataset_name"] == "robotics-demo"

    show = json.loads(runner.invoke(app, ["dataset", "show", "robotics-demo", "--workspace", str(ws), "--json"]).output)
    assert show[0]["version"] == "1.0.0"
    assert show[0]["package_id"] == package_id


def test_lineage(tmp_path: Path) -> None:
    ws = _init_workspace(tmp_path)
    config_path = _write_pipeline_config(tmp_path)
    run = json.loads(runner.invoke(app, ["run", str(config_path), "--workspace", str(ws), "--json"]).output)
    package_id = next(a["artifact_id"] for a in run["artifacts"] if a["artifact_type"] == "package")

    result = runner.invoke(app, ["lineage", "package", package_id, "--workspace", str(ws), "--json"])
    assert result.exit_code == 0, result.output
    graph = json.loads(result.output)
    assert graph["root"] == {"artifact_type": "package", "artifact_id": package_id}
    assert any(n["artifact_type"] == "ingestion" for n in graph["nodes"])


# --- 14: verify ---------------------------------------------------------


def test_verify(tmp_path: Path) -> None:
    ws = _init_workspace(tmp_path)
    config_path = _write_pipeline_config(tmp_path)
    run = json.loads(runner.invoke(app, ["run", str(config_path), "--workspace", str(ws), "--json"]).output)
    package_id = next(a["artifact_id"] for a in run["artifacts"] if a["artifact_type"] == "package")

    result = runner.invoke(app, ["verify", "package", package_id, "--workspace", str(ws)])
    assert result.exit_code == 0, result.output
    assert "verified" in result.output.lower()


# --- 15: recovery ------------------------------------------------------------


def test_recovery_scan_on_clean_workspace(tmp_path: Path) -> None:
    ws = _init_workspace(tmp_path)
    result = runner.invoke(app, ["recover", "scan", "--workspace", str(ws), "--json"])
    assert result.exit_code == 0
    body = json.loads(result.output)
    assert body["active_count"] == 0
    assert body["stale_count"] == 0


# --- 16: doctor ---------------------------------------------------------


def test_doctor(tmp_path: Path) -> None:
    ws = _init_workspace(tmp_path)
    result = runner.invoke(app, ["doctor", "--workspace", str(ws)])
    assert result.exit_code == 0, result.output
    assert "System ready" in result.output


def test_doctor_json_output(tmp_path: Path) -> None:
    ws = _init_workspace(tmp_path)
    result = runner.invoke(app, ["doctor", "--workspace", str(ws), "--json"])
    assert result.exit_code == 0
    body = json.loads(result.output)
    assert "checks" in body
    assert any(c["name"] == "Catalog integrity_check" and c["ok"] for c in body["checks"])


# --- 17: serve command construction -----------------------------------


@pytest.fixture
def _restore_environ():
    """`forge serve` sets workspace-rooted storage-root env vars via
    plain `os.environ.update(...)` (not `monkeypatch.setenv`, since real
    CLI usage needs them to persist for the server process's lifetime) --
    snapshot and restore around any test that invokes it, so a real
    `Settings()`/`get_settings()` constructed later in this same pytest
    process can never pick up a leaked CATALOG_DB_PATH/etc. pointing at
    this test's tmp_path workspace."""
    before = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(before)


def test_serve_binds_localhost_by_default_and_never_actually_blocks(tmp_path: Path, _restore_environ: None) -> None:
    """Never starts a real server (would hang the test) -- patches
    uvicorn.run and asserts it would have been called with the correct,
    localhost-default host/port (Design Requirement 62)."""
    ws = _init_workspace(tmp_path)
    with patch("uvicorn.run") as mock_run:
        result = runner.invoke(app, ["serve", "--workspace", str(ws), "--port", "8123"])
    assert result.exit_code == 0, result.output
    mock_run.assert_called_once()
    _, kwargs = mock_run.call_args
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 8123


def test_serve_warns_on_non_localhost_bind(tmp_path: Path, _restore_environ: None) -> None:
    ws = _init_workspace(tmp_path)
    with patch("uvicorn.run"):
        result = runner.invoke(app, ["serve", "--workspace", str(ws), "--host", "0.0.0.0"])
    assert result.exit_code == 0
    normalized = " ".join(result.output.lower().split())
    assert "no authentication is implemented" in normalized
