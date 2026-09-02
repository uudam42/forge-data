"""CLI/GUI must never change determinism (v2.7, Design Requirements 65/66).

Running the same effective pipeline config over the same input bytes
through the CLI (`forge run`, direct in-process execution) and through
the real HTTP API (`POST /api/v1/runs`, the same path the GUI uses) must
produce byte-identical final artifacts -- run_id/session_id/timestamps/
progress differ, but none of those are baked into content hashes.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from app.cli.main import app as cli_app
from app.cli.services import build_catalog_service
from app.cli.workspace import build_settings_for_workspace
from tests.v26_helpers import GPS_CSV, IMU_CSV, submit_run, wait_for_run

runner = CliRunner()

_STAGES_CONFIG = {
    "synchronization": {"reference": {"mode": "stream", "stream": "imu"}, "alignment": {"default_method": "nearest", "max_time_delta_ms": 400}},
    "cleaning": {"policy_name": "default_multimodal", "policy_version": "1.0.0", "config": {"required_streams": ["imu"]}},
    "transformation": {"profile_name": "multimodal_window_v1", "profile_version": "1.0.0", "config": {"window": {"mode": "count", "size": 10, "stride": 10, "drop_incomplete": True}}},
    "qc": {"profile_name": "default_dataset_qc", "profile_version": "1.0.0", "config": {"minimum_samples": 1}},
    "packaging": {
        "profile_name": "default_ml_package", "profile_version": "1.0.0",
        "config": {"split": {"strategy": "group_hash", "train_ratio": 1.0, "validation_ratio": 0.0, "test_ratio": 0.0, "seed": 1}, "grouping": {"mode": "source_overlap"}, "exports": ["jsonl"]},
    },
}


def test_cli_run_and_http_api_run_produce_byte_identical_packages(tmp_path: Path, client: TestClient) -> None:
    # 1. Same pipeline, run through the real HTTP API (the path the GUI uses).
    api_result = submit_run(client, ["imu", "gps"], session_id="sess_v27_api")
    api_final = wait_for_run(client, api_result["run_id"])
    assert api_final["status"] == "completed"
    assert client.post("/api/v1/catalog/scan").status_code == 200
    api_pkg_id = next(a["artifact_id"] for a in api_final["artifacts"] if a["artifact_type"] == "package")
    api_artifact = client.get(f"/api/v1/catalog/artifacts/package/{api_pkg_id}").json()

    # 2. The exact same config and byte-identical input, run through the
    # CLI's own workspace-rooted, in-process execution path -- a
    # completely separate Settings/catalog.db instance.
    ws = tmp_path / "ws"
    init = runner.invoke(cli_app, ["init", str(ws)])
    assert init.exit_code == 0, init.output

    config = json.loads(json.dumps(_STAGES_CONFIG))
    config["streams"] = [
        {"sensor_type": "imu", "path": "imu.csv", "source_units": {"acceleration": "m/s^2", "angular_velocity": "rad/s"}},
        {"sensor_type": "gps", "path": "gps.csv", "source_units": {"altitude": "m", "speed": "m/s"}},
    ]
    (tmp_path / "imu.csv").write_text(IMU_CSV)
    (tmp_path / "gps.csv").write_text(GPS_CSV)
    config_path = tmp_path / "pipeline.yaml"
    config_path.write_text(yaml.safe_dump(config))

    cli_result = runner.invoke(cli_app, ["run", str(config_path), "--workspace", str(ws), "--json"])
    assert cli_result.exit_code == 0, cli_result.output
    cli_run = json.loads(cli_result.output)
    assert cli_run["status"] == "completed"
    cli_pkg_id = next(a["artifact_id"] for a in cli_run["artifacts"] if a["artifact_type"] == "package")

    cli_settings = build_settings_for_workspace(ws)
    cli_catalog_service = build_catalog_service(cli_settings)
    cli_catalog_service.scan()
    cli_artifact = cli_catalog_service.get_artifact("package", cli_pkg_id)

    assert api_artifact["artifact"]["content_sha256"] == cli_artifact.artifact.content_sha256


def test_cli_and_api_produce_the_same_config_hash_for_equivalent_requests(tmp_path: Path) -> None:
    """The run's own `config_hash` (Design Requirement 57: canonical,
    deterministic, excludes run_id/timestamps) must be identical whether
    the effective `PipelineRunRequest` was built by the CLI's YAML loader
    or constructed directly (as the HTTP API's request body would be) --
    proving the CLI's config file is a pure serialization layer over the
    same real model, never a second semantics system."""
    from app.cli.pipeline_config import load_pipeline_config
    from app.runs.models import PipelineRunRequest
    from app.runs.service import compute_config_hash

    config = json.loads(json.dumps(_STAGES_CONFIG))
    config["streams"] = [
        {"sensor_type": "imu", "path": "imu.csv", "source_units": {"acceleration": "m/s^2", "angular_velocity": "rad/s"}},
        {"sensor_type": "gps", "path": "gps.csv", "source_units": {"altitude": "m", "speed": "m/s"}},
    ]
    (tmp_path / "imu.csv").write_text(IMU_CSV)
    (tmp_path / "gps.csv").write_text(GPS_CSV)
    config_path = tmp_path / "pipeline.yaml"
    config_path.write_text(yaml.safe_dump(config))

    loaded = load_pipeline_config(config_path)
    cli_hash = compute_config_hash(loaded.request)

    # The equivalent request an HTTP API caller (or the GUI) would send --
    # same fields, just built directly instead of parsed from YAML.
    api_request = PipelineRunRequest.model_validate(
        {
            "streams": [
                {"sensor_type": "imu", "source_units": {"acceleration": "m/s^2", "angular_velocity": "rad/s"}},
                {"sensor_type": "gps", "source_units": {"altitude": "m", "speed": "m/s"}},
            ],
            **_STAGES_CONFIG,
        }
    )
    api_hash = compute_config_hash(api_request)

    assert cli_hash == api_hash
