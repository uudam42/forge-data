"""`forge rebuild` (release-hardening) -- a thin CLI wrapper over the
existing v2.4 `CatalogService.rebuild()`, the same call
`POST /api/v1/catalog/rebuild` makes. It exists to make the recovery
workflow `docs/MIGRATION_V1_TO_V2.md` documents (and every `.scan()`
retry-fallback's own error message points to) actually runnable without
starting the HTTP server.

Dataset/governance/run-metadata preservation across a rebuild is already
exhaustively tested at the service layer (tests/test_v25_dataset_version_
governance.py, tests/test_v25_governance_model.py, tests/
test_v26_catalog_rebuild_preserves_runs.py) -- these tests don't repeat
that; they confirm the CLI command itself calls the real service and
surfaces its real, unfabricated result, and that the documented
relocated-workspace recovery path is genuinely runnable end to end via
the CLI alone."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from app.catalog.repository import CatalogRepository
from app.catalog.service import CatalogService
from app.cli.main import app
from app.cli.workspace import build_settings_for_workspace
from app.runs.repository import RunRepository
from app.runs.service import RunService
from app.storage.catalog_store import get_connection
from tests.test_release_scan_failure_handling import _corrupt_one_manifest_uri, _delete_one_artifact_row
from tests.test_v26_run_state_machine import _minimal_request
from tests.test_v27_cli import _init_workspace, _write_pipeline_config, runner


def _run_pipeline_via_cli(ws: Path) -> dict:
    config_path = _write_pipeline_config(ws)
    result = runner.invoke(app, ["run", str(config_path), "--workspace", str(ws), "--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def _artifact_id(run: dict, artifact_type: str) -> str:
    return next(a["artifact_id"] for a in run["artifacts"] if a["artifact_type"] == artifact_type)


def _catalog_service_for(ws: Path) -> CatalogService:
    from app.catalog.rebuild_lock import RebuildLock
    from app.catalog.scanner import CatalogScanner
    from app.catalog.verifier import ArtifactVerifier

    settings = build_settings_for_workspace(ws)
    conn = get_connection(settings.CATALOG_DB_PATH)
    repo = CatalogRepository(conn, db_path=str(settings.CATALOG_DB_PATH))
    lock_path = settings.CATALOG_DB_PATH.parent / "catalog.rebuild.lock"
    return CatalogService(repo=repo, scanner=CatalogScanner(settings), verifier=ArtifactVerifier(settings), rebuild_lock=RebuildLock(lock_path), settings=settings)


def test_forge_rebuild_reports_registered_and_preserved_counts(tmp_path: Path) -> None:
    """1: `forge rebuild --workspace <ws>` succeeds, with the exact
    human-readable field labels the recovery workflow relies on."""
    ws = _init_workspace(tmp_path)
    run = _run_pipeline_via_cli(ws)
    pkg_id = _artifact_id(run, "package")

    reg = runner.invoke(app, ["dataset", "register", "rebuild-cli-check", "--version", "1.0.0", "--package-id", pkg_id, "--workspace", str(ws)])
    assert reg.exit_code == 0, reg.output

    result = runner.invoke(app, ["rebuild", "--workspace", str(ws)])
    assert result.exit_code == 0, result.output
    assert "traceback" not in result.output.lower()
    assert "Catalog rebuild completed" in result.output
    assert "Artifacts registered: 13" in result.output
    assert "Edges registered: 13" in result.output
    assert "Datasets preserved: 1" in result.output
    assert "Dataset versions preserved: 1" in result.output


def test_forge_rebuild_json_matches_service_result(tmp_path: Path) -> None:
    ws = _init_workspace(tmp_path)
    _run_pipeline_via_cli(ws)

    result = runner.invoke(app, ["rebuild", "--workspace", str(ws), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["artifacts_registered"] == 13
    assert payload["edges_registered"] == 13
    assert payload["issues"] == []
    assert payload["datasets_preserved"] == 0
    assert payload["dataset_versions_preserved"] == 0


def test_forge_rebuild_preserves_dataset_mappings(tmp_path: Path) -> None:
    """2: dataset name/version -> package_id mappings survive a rebuild
    performed via the CLI, not just via the service directly."""
    ws = _init_workspace(tmp_path)
    run = _run_pipeline_via_cli(ws)
    pkg_id = _artifact_id(run, "package")

    reg = runner.invoke(app, ["dataset", "register", "dataset-mapping-check", "--version", "1.0.0", "--package-id", pkg_id, "--workspace", str(ws)])
    assert reg.exit_code == 0, reg.output

    before = runner.invoke(app, ["dataset", "show", "dataset-mapping-check", "--workspace", str(ws), "--json"])
    assert before.exit_code == 0, before.output

    rebuild_result = runner.invoke(app, ["rebuild", "--workspace", str(ws)])
    assert rebuild_result.exit_code == 0, rebuild_result.output

    after = runner.invoke(app, ["dataset", "show", "dataset-mapping-check", "--workspace", str(ws), "--json"])
    assert after.exit_code == 0, after.output
    assert json.loads(before.output) == json.loads(after.output)


def test_forge_rebuild_preserves_governance_metadata(tmp_path: Path) -> None:
    """3: an artifact's governance state (set directly at the service
    layer, since there is no CLI surface for governance mutation) is
    untouched by a rebuild performed via the CLI."""
    ws = _init_workspace(tmp_path)
    run = _run_pipeline_via_cli(ws)
    pkg_id = _artifact_id(run, "package")
    ingestion_id = _artifact_id(run, "ingestion")

    # Registering a dataset version scans (and therefore indexes) every
    # artifact on disk, which set_artifact_governance requires.
    reg = runner.invoke(app, ["dataset", "register", "governance-rebuild-check", "--version", "1.0.0", "--package-id", pkg_id, "--workspace", str(ws)])
    assert reg.exit_code == 0, reg.output

    service = _catalog_service_for(ws)
    service.set_artifact_governance("ingestion", ingestion_id, new_state="deprecated", reason="rebuild-preservation regression test")
    before = service.get_artifact_governance("ingestion", ingestion_id)
    assert before.state == "deprecated"

    result = runner.invoke(app, ["rebuild", "--workspace", str(ws)])
    assert result.exit_code == 0, result.output

    after = _catalog_service_for(ws).get_artifact_governance("ingestion", ingestion_id)
    assert after.state == "deprecated"
    assert after.reason == "rebuild-preservation regression test"


def test_forge_rebuild_preserves_run_metadata(tmp_path: Path) -> None:
    """4: PipelineRun/StageRun/run-artifact/run-event rows (v2.6) are
    untouched by a rebuild performed via the CLI -- mirrors
    tests/test_v26_catalog_rebuild_preserves_runs.py, but through
    `forge rebuild` instead of calling CatalogService.rebuild() directly."""
    ws = _init_workspace(tmp_path)
    settings = build_settings_for_workspace(ws)
    conn = get_connection(settings.CATALOG_DB_PATH)
    run_repo = RunRepository(conn)
    catalog_repo = CatalogRepository(conn)
    run_service = RunService(repo=run_repo, settings=settings)

    run_id = run_service.create_run(run_type="pipeline", request=_minimal_request())
    run_service.mark_running(run_id, executor_id="x")
    with run_repo.transaction():
        run_repo.create_stage_run(stage_run_id="stagerun_1", run_id=run_id, stage="ingestion:imu", status="pending")
        run_repo.update_stage_run("stagerun_1", status="completed", records_processed=10)
        run_repo.record_run_artifact(run_id=run_id, stage="ingestion:imu", artifact_type="ingestion", artifact_id="ing_test_1", created_at="2026-01-01T00:00:00Z")
    run_service.mark_completed(run_id)

    before = catalog_repo.count_run_tables()

    result = runner.invoke(app, ["rebuild", "--workspace", str(ws)])
    assert result.exit_code == 0, result.output

    assert catalog_repo.count_run_tables() == before
    run = run_service.get_run(run_id)
    assert run.status == "completed"
    assert len(run.stage_runs) == 1
    assert run.stage_runs[0].records_processed == 10
    assert len(run.artifacts) == 1
    assert run.artifacts[0].artifact_id == "ing_test_1"


def test_forge_rebuild_reports_clean_error_when_another_rebuild_is_in_progress(tmp_path: Path, monkeypatch) -> None:
    """5: a structured CatalogRebuildInProgressError maps to a clean CLI
    failure (exit 1, no traceback) -- deterministic unit-level check of
    forge rebuild's own exception handling, same style as
    tests/test_release_scan_failure_handling.py's CLI verify test."""
    ws = _init_workspace(tmp_path)

    from app.catalog.errors import CatalogRebuildInProgressError
    from app.catalog.service import CatalogService

    def _boom(self):
        raise CatalogRebuildInProgressError(lock_path="/fake/catalog.rebuild.lock", holder={"pid": 999, "hostname": "other-host"})

    monkeypatch.setattr(CatalogService, "rebuild", _boom)

    result = runner.invoke(app, ["rebuild", "--workspace", str(ws)])
    assert result.exit_code == 1
    assert "traceback" not in result.output.lower()
    assert "already in progress" in result.output.lower()


def test_scan_failure_then_forge_rebuild_recovers_end_to_end(tmp_path: Path) -> None:
    """6: the full documented recovery workflow, driven entirely through
    the CLI (no HTTP server) -- forge lineage hits a real
    CatalogScanFailedError, reports it cleanly, and forge rebuild then
    genuinely recovers the catalog."""
    ws = _init_workspace(tmp_path)
    run = _run_pipeline_via_cli(ws)
    ingestion_id = _artifact_id(run, "ingestion")
    package_id = _artifact_id(run, "package")  # any other real artifact to corrupt

    # Index everything first (forge rebuild also exercises this path,
    # covered by other tests above -- here it's just setup).
    setup = runner.invoke(app, ["rebuild", "--workspace", str(ws)])
    assert setup.exit_code == 0, setup.output

    catalog_db_path = ws / "data" / "catalog" / "catalog.db"
    _corrupt_one_manifest_uri(catalog_db_path, "package", package_id)
    _delete_one_artifact_row(catalog_db_path, "ingestion", ingestion_id)

    failure = runner.invoke(app, ["lineage", "ingestion", ingestion_id, "--workspace", str(ws)])
    assert failure.exit_code == 1
    assert "traceback" not in failure.output.lower()
    assert "scan failed" in failure.output.lower()
    assert "rebuild" in failure.output.lower()

    recovery = runner.invoke(app, ["rebuild", "--workspace", str(ws)])
    assert recovery.exit_code == 0, recovery.output
    assert "Catalog rebuild completed" in recovery.output

    success = runner.invoke(app, ["lineage", "ingestion", ingestion_id, "--workspace", str(ws)])
    assert success.exit_code == 0, success.output
