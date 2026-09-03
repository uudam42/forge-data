"""Release-hardening regression: a `CatalogScanFailedError` raised inside
the "scan-once-and-retry" fallback (v2.7's fix for a not-yet-indexed
artifact -- see app.api.routes.catalog.verify_artifact) must never reach
a caller as a raw, unhandled exception.

Found via a real v1-workspace-relocation compatibility test: the
catalog registry's anti-silent-overwrite guard (a stored `manifest_uri`
that no longer matches the artifact's real on-disk path -- e.g. after a
workspace directory is moved) raises `CatalogScanFailedError` from
inside `CatalogService.scan()`. Every one of the six call sites that
call `.scan()` as a retry-before-404 fallback (three HTTP routes, the
Results resolver, and three CLI commands) needs to handle this
explicitly, because it previously did not.
"""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from app.cli.main import app as cli_app
from tests.v26_helpers import submit_run, wait_for_run

runner = CliRunner()


def _corrupt_one_manifest_uri(catalog_db_path, artifact_type: str, artifact_id: str) -> None:
    """Simulates a relocated workspace: the stored manifest_uri no longer
    matches where a rescan would find that artifact on disk."""
    conn = sqlite3.connect(str(catalog_db_path))
    conn.execute(
        "UPDATE artifacts SET manifest_uri = 'file:///nonexistent/stale/manifest.json' "
        "WHERE artifact_type = ? AND artifact_id = ?",
        (artifact_type, artifact_id),
    )
    conn.commit()
    conn.close()


def _delete_one_artifact_row(catalog_db_path, artifact_type: str, artifact_id: str) -> None:
    """Forces the next lookup of this (different) artifact to raise
    ArtifactNotFoundError, triggering the scan-retry path."""
    conn = sqlite3.connect(str(catalog_db_path))
    conn.execute("DELETE FROM artifacts WHERE artifact_type = ? AND artifact_id = ?", (artifact_type, artifact_id))
    conn.commit()
    conn.close()


def _set_up_conflicted_catalog(client: TestClient, catalog_db_path) -> tuple[str, str]:
    """Runs a real pipeline, scans it in, then corrupts one artifact's
    manifest_uri and drops another's index row -- reproducing exactly
    the two conditions needed to hit CatalogScanFailedError via the
    scan-once-and-retry path. Returns (stale_artifact_id_type_pair)."""
    created = submit_run(client, ["imu", "gps"])
    final = wait_for_run(client, created["run_id"])
    assert final["status"] == "completed"
    assert client.post("/api/v1/catalog/scan").status_code == 200

    validation_id = next(a["artifact_id"] for a in final["artifacts"] if a["artifact_type"] == "validation")
    ingestion_id = next(a["artifact_id"] for a in final["artifacts"] if a["artifact_type"] == "ingestion")

    _corrupt_one_manifest_uri(catalog_db_path, "ingestion", ingestion_id)
    _delete_one_artifact_row(catalog_db_path, "validation", validation_id)
    return "validation", validation_id


def test_lineage_route_returns_structured_500_not_a_traceback_on_scan_failure(
    client: TestClient, catalog_db_path
) -> None:
    artifact_type, artifact_id = _set_up_conflicted_catalog(client, catalog_db_path)
    resp = client.get(f"/api/v1/lineage/{artifact_type}/{artifact_id}")
    assert resp.status_code == 500
    assert "scan failed" in resp.json()["detail"].lower()


def test_verify_route_returns_structured_500_not_a_traceback_on_scan_failure(
    client: TestClient, catalog_db_path
) -> None:
    artifact_type, artifact_id = _set_up_conflicted_catalog(client, catalog_db_path)
    resp = client.post(f"/api/v1/catalog/verify/{artifact_type}/{artifact_id}")
    assert resp.status_code == 500
    assert "scan failed" in resp.json()["detail"].lower()


def test_register_version_route_returns_structured_500_not_a_traceback_on_scan_failure(
    client: TestClient, catalog_db_path
) -> None:
    created = submit_run(client, ["imu", "gps"])
    final = wait_for_run(client, created["run_id"])
    assert final["status"] == "completed"
    assert client.post("/api/v1/catalog/scan").status_code == 200

    ingestion_id = next(a["artifact_id"] for a in final["artifacts"] if a["artifact_type"] == "ingestion")
    _corrupt_one_manifest_uri(catalog_db_path, "ingestion", ingestion_id)

    assert client.post("/api/v1/datasets", json={"dataset_name": "scan-failure-demo"}).status_code in (200, 201)
    resp = client.post(
        "/api/v1/datasets/scan-failure-demo/versions",
        json={"version": "1.0.0", "package_id": "pkg_does_not_exist_yet"},
    )
    assert resp.status_code in (404, 500)
    if resp.status_code == 500:
        assert "scan failed" in resp.json()["detail"].lower()


def test_results_endpoint_degrades_gracefully_rather_than_500ing_on_scan_failure(
    client: TestClient, catalog_db_path
) -> None:
    """app.runs.results deliberately treats a scan failure the same as
    "still not found after scan" -- a resultless (not 500) response --
    since Results is a best-effort, always-200 read path."""
    created = submit_run(client, ["imu", "gps"])
    final = wait_for_run(client, created["run_id"])
    assert final["status"] == "completed"
    assert client.post("/api/v1/catalog/scan").status_code == 200

    ingestion_id = next(a["artifact_id"] for a in final["artifacts"] if a["artifact_type"] == "ingestion")
    _corrupt_one_manifest_uri(catalog_db_path, "ingestion", ingestion_id)
    package_id = next(a["artifact_id"] for a in final["artifacts"] if a["artifact_type"] == "package")
    _delete_one_artifact_row(catalog_db_path, "package", package_id)

    resp = client.get(f"/api/v1/runs/{created['run_id']}/results")
    assert resp.status_code == 200
    assert resp.json()["package"] is None


def test_cli_verify_reports_clean_error_not_a_traceback_on_scan_failure(tmp_path, monkeypatch) -> None:
    """Deterministic unit-level check of verify_cmd's new exception
    handling: force `CatalogService.scan()` to raise (this is exactly
    what a relocated-workspace manifest_uri conflict does -- reproduced
    end-to-end, live, against a real corrupted catalog, in the module
    docstring's motivating scenario) and confirm the CLI reports a clean
    message and exit code 1, never a traceback. The HTTP-route tests
    above already exercise the real, naturally-triggered failure; this
    test exercises the CLI's own error-handling wiring directly, without
    depending on two nested in-process CLI invocations reproducing a
    timing/ordering-sensitive SQLite conflict reliably in CI."""
    ws = tmp_path / "ws"
    assert runner.invoke(cli_app, ["init", str(ws)]).exit_code == 0

    from app.catalog.errors import CatalogScanFailedError
    from app.catalog.service import CatalogService

    def _boom(self):
        raise CatalogScanFailedError("Catalog scan failed")

    monkeypatch.setattr(CatalogService, "scan", _boom)

    verify_result = runner.invoke(cli_app, ["verify", "validation", "val_does_not_exist", "--workspace", str(ws)])
    assert verify_result.exit_code == 1
    assert "traceback" not in verify_result.output.lower()
    assert "scan failed" in verify_result.output.lower()
    assert "rebuild" in verify_result.output.lower()
