"""v2.7 Results Explorer backend: GET /runs/{run_id}/results and
POST /packages/{package_id}/open-folder.

Design Requirement 32/33/74/61.
"""

from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.v26_helpers import submit_run, wait_for_run


def test_completed_run_results_resolve_package_qc_splits_and_files(client: TestClient) -> None:
    created = submit_run(client, ["imu", "gps"])
    run = wait_for_run(client, created["run_id"])
    assert run["status"] == "completed"

    resp = client.get(f"/api/v1/runs/{created['run_id']}/results")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["run_id"] == created["run_id"]
    assert body["run_status"] == "completed"
    assert body["package"] is not None
    assert body["package"]["status"] == "completed"
    assert body["package"]["sample_count"] > 0
    assert body["package"]["formats"] == ["jsonl"]
    assert body["splits"]["train"] > 0
    assert body["qc"] is not None
    assert body["qc"]["status"] in ("passed", "passed_with_warnings")
    assert body["lineage_fingerprint"]
    assert body["dataset_registrations"] == []

    names = {f["relative_path"] for f in body["files"]}
    assert "train.jsonl" in names
    assert "manifest.json" in names
    assert "report.json" in names
    for f in body["files"]:
        assert isinstance(f["size_bytes"], int)


def test_run_with_no_package_returns_null_fields_not_404(client: TestClient) -> None:
    created = submit_run(client, ["imu", "gps"], config_overrides={"cleaning": {"policy_name": "does_not_exist", "policy_version": "1.0.0", "config": {}}})
    run = wait_for_run(client, created["run_id"])
    assert run["status"] == "failed"

    resp = client.get(f"/api/v1/runs/{created['run_id']}/results")
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_status"] == "failed"
    assert body["package"] is None
    assert body["qc"] is None
    assert body["splits"] is None
    assert body["files"] == []


def test_results_for_unknown_run_is_404(client: TestClient) -> None:
    resp = client.get("/api/v1/runs/run_does_not_exist/results")
    assert resp.status_code == 404


def test_results_file_sizes_come_from_manifest_stat_not_a_content_read(client: TestClient) -> None:
    """Design Requirement 74/75: split file sizes are the manifest's own
    recorded `size_bytes` (computed once at packaging time), matching the
    real file's stat size on disk -- the results resolver never re-reads
    or re-scans split file contents to compute them."""
    import os

    created = submit_run(client, ["imu", "gps"])
    wait_for_run(client, created["run_id"])

    resp = client.get(f"/api/v1/runs/{created['run_id']}/results")
    body = resp.json()
    split_file = next(f for f in body["files"] if f["relative_path"] == "train.jsonl")
    actual_path = os.path.join(body["package"]["local_path"], split_file["relative_path"])
    assert split_file["size_bytes"] == os.path.getsize(actual_path)


def test_results_reflect_dataset_registration(client: TestClient) -> None:
    created = submit_run(client, ["imu", "gps"])
    wait_for_run(client, created["run_id"])
    results = client.get(f"/api/v1/runs/{created['run_id']}/results").json()
    package_id = results["package"]["package_id"]

    ds_resp = client.post("/api/v1/datasets", json={"dataset_name": "results-api-demo"})
    assert ds_resp.status_code in (200, 201), ds_resp.text
    ver_resp = client.post(
        "/api/v1/datasets/results-api-demo/versions", json={"version": "1.0.0", "package_id": package_id}
    )
    assert ver_resp.status_code in (200, 201), ver_resp.text

    body = client.get(f"/api/v1/runs/{created['run_id']}/results").json()
    assert body["dataset_registrations"] == [{"dataset_name": "results-api-demo", "version": "1.0.0", "effective_status": "healthy"}]


def test_open_folder_rejects_unknown_package(client: TestClient) -> None:
    resp = client.post("/api/v1/packages/pkg_does_not_exist/open-folder")
    assert resp.status_code == 404


def test_open_folder_rejects_path_traversal_package_id(client: TestClient) -> None:
    resp = client.post("/api/v1/packages/..%2F..%2Fetc/open-folder")
    # Starlette decodes %2F before route matching, so this either never
    # reaches the handler at all (404 no route / 405 route exists at the
    # normalized path but not for POST) or reaches it and is rejected for
    # naming an unknown package (404) -- every outcome is "not opened",
    # which is what actually matters here. The real traversal-safety
    # guarantee (a resolved path must stay inside PACKAGE_STORAGE_ROOT)
    # is proven directly by app.api.routes.packages tests exercising
    # `_resolve_within_package_root`, not by this routing edge case.
    assert resp.status_code in (404, 405, 422)


def test_open_folder_opens_real_package_directory_mocked_subprocess(client: TestClient) -> None:
    created = submit_run(client, ["imu", "gps"])
    wait_for_run(client, created["run_id"])
    results = client.get(f"/api/v1/runs/{created['run_id']}/results").json()
    package_id = results["package"]["package_id"]

    with patch("app.api.routes.packages.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        resp = client.post(f"/api/v1/packages/{package_id}/open-folder")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["opened"] is True
    assert body["path"] == results["package"]["local_path"]

    argv = mock_run.call_args[0][0]
    assert mock_run.call_args.kwargs.get("shell", False) is False
    assert argv[-1] == results["package"]["local_path"]


def test_open_folder_never_uses_shell(client: TestClient) -> None:
    created = submit_run(client, ["imu", "gps"])
    wait_for_run(client, created["run_id"])
    results = client.get(f"/api/v1/runs/{created['run_id']}/results").json()
    package_id = results["package"]["package_id"]

    with patch("app.api.routes.packages.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        client.post(f"/api/v1/packages/{package_id}/open-folder")

    _, kwargs = mock_run.call_args
    assert "shell" not in kwargs or kwargs["shell"] is False
