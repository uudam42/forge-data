"""Tests for the packaging config hash and byte-for-byte determinism of
split artifacts, split_index, and group assignment stability."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.packaging.models import GroupingConfig, PackagingConfig, SplitConfig
from app.packaging.profiles.default import DEFAULT_ML_PACKAGE
from app.packaging.serialization import canonical_json

PKG_URL = "/api/v1/packaging"

IMU_CSV = "timestamp,accel_x,accel_y,accel_z,gyro_x,gyro_y,gyro_z\n" + "".join(
    f"2026-08-30T18:{i//60:02d}:{i%60:02d}Z,0.{i%10},0.2,9.8,0.01,0.02,0.03\n" for i in range(180)
)
GPS_CSV = "timestamp,latitude,longitude,altitude,speed\n" + "".join(
    f"2026-08-30T18:{i//60:02d}:{i%60:02d}Z,34.02{i%90:02d},-118.28{i%90:02d},100.0,9.{i%9}\n" for i in range(0, 180, 3)
)


# ---------------------------------------------------------------------------
# Config hash — unit level
# ---------------------------------------------------------------------------


def _config(**split_overrides) -> PackagingConfig:
    split = {"strategy": "group_hash", "train_ratio": 0.7, "validation_ratio": 0.15, "test_ratio": 0.15, "seed": 42}
    split.update(split_overrides)
    return PackagingConfig(split=SplitConfig(**split), grouping=GroupingConfig(), exports=["jsonl"])


def test_config_hash_deterministic() -> None:
    h1 = DEFAULT_ML_PACKAGE.config_hash(_config())
    h2 = DEFAULT_ML_PACKAGE.config_hash(_config())
    assert h1 == h2
    assert len(h1) == 64


def test_changing_seed_changes_config_hash() -> None:
    h1 = DEFAULT_ML_PACKAGE.config_hash(_config(seed=1))
    h2 = DEFAULT_ML_PACKAGE.config_hash(_config(seed=2))
    assert h1 != h2


def test_changing_ratios_changes_config_hash() -> None:
    h1 = DEFAULT_ML_PACKAGE.config_hash(_config(train_ratio=0.7, validation_ratio=0.15, test_ratio=0.15))
    h2 = DEFAULT_ML_PACKAGE.config_hash(_config(train_ratio=0.8, validation_ratio=0.1, test_ratio=0.1))
    assert h1 != h2


def test_changing_export_formats_changes_config_hash() -> None:
    config_a = PackagingConfig(split=SplitConfig(train_ratio=1.0), grouping=GroupingConfig(), exports=["jsonl"])
    config_b = PackagingConfig(split=SplitConfig(train_ratio=1.0), grouping=GroupingConfig(), exports=["jsonl", "parquet"])
    h1 = DEFAULT_ML_PACKAGE.config_hash(config_a)
    h2 = DEFAULT_ML_PACKAGE.config_hash(config_b)
    assert h1 != h2


def test_config_hash_independent_of_dict_key_order() -> None:
    payload_a = {"b": 2, "a": 1}
    payload_b = {"a": 1, "b": 2}
    assert canonical_json(payload_a) == canonical_json(payload_b)


# ---------------------------------------------------------------------------
# End-to-end determinism
# ---------------------------------------------------------------------------


def _upload(client: TestClient, filename: str, content: str, **fields) -> dict:
    response = client.post(
        "/api/v1/ingestion/upload", files={"file": (filename, content.encode(), None)}, data=fields
    )
    assert response.status_code == 201, response.text
    return response.json()


def _pipeline(client: TestClient, filename, content, schema_name, profile_name, source_units, **fields) -> dict:
    ingestion = _upload(client, filename, content, **fields)
    for path, body in (
        (f"/api/v1/validation/{ingestion['ingestion_id']}", {"schema_name": schema_name, "schema_version": "1.0.0"}),
        (f"/api/v1/integrity/{ingestion['ingestion_id']}", {"schema_name": schema_name, "schema_version": "1.0.0"}),
    ):
        r = client.post(path, json=body)
        assert r.status_code == 200, r.text
    r = client.post(
        f"/api/v1/normalization/{ingestion['ingestion_id']}",
        json={
            "schema_name": schema_name,
            "schema_version": "1.0.0",
            "profile_name": profile_name,
            "profile_version": "1.0.0",
            "source_units": source_units,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _qc_ready(client: TestClient, session_id: str) -> dict:
    imu = _pipeline(client, "imu.csv", IMU_CSV, "imu", "imu_canonical", {"acceleration": "m/s^2", "angular_velocity": "rad/s"}, session_id=session_id)
    gps = _pipeline(client, "gps.csv", GPS_CSV, "gps", "gps_canonical", {"altitude": "m", "speed": "m/s"}, session_id=session_id)
    sync = client.post(
        "/api/v1/synchronization",
        json={
            "streams": [
                {"name": "imu", "normalization_id": imu["normalization_id"]},
                {"name": "gps", "normalization_id": gps["normalization_id"]},
            ],
            "reference": {"mode": "stream", "stream": "imu"},
            "alignment": {"default_method": "nearest", "max_time_delta_ms": 400},
        },
    ).json()
    cleaned = client.post(
        f"/api/v1/cleaning/{sync['synchronization_id']}",
        json={"policy_name": "default_multimodal", "policy_version": "1.0.0", "config": {"required_streams": ["imu"]}},
    ).json()
    xform = client.post(
        f"/api/v1/transformation/{cleaned['cleaning_id']}",
        json={
            "profile_name": "multimodal_window_v1",
            "profile_version": "1.0.0",
            "config": {
                "window": {"mode": "count", "size": 10, "stride": 10, "drop_incomplete": True},
                "features": {"imu": {"statistics": ["mean", "std"]}, "gps": {"statistics": ["mean"]}},
            },
        },
    ).json()
    qc = client.post(
        f"/api/v1/qc/{xform['transformation_id']}",
        json={"profile_name": "default_dataset_qc", "profile_version": "1.0.0", "config": {"minimum_samples": 1}},
    ).json()
    return {"transformation_id": xform["transformation_id"], "qc_id": qc["qc_id"]}


def _default_request(qc_id: str, **overrides) -> dict:
    split = {"strategy": "group_hash", "train_ratio": 0.7, "validation_ratio": 0.15, "test_ratio": 0.15, "seed": 42}
    split.update(overrides)
    return {
        "qc_id": qc_id,
        "profile_name": "default_ml_package",
        "profile_version": "1.0.0",
        "config": {"split": split, "grouping": {"mode": "source_overlap"}, "exports": ["jsonl"]},
    }


def test_same_input_and_config_produces_byte_identical_split_files(client: TestClient, package_root: Path) -> None:
    ready = _qc_ready(client, "sess_pkg_det")
    request = _default_request(ready["qc_id"])

    body1 = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request).json()
    body2 = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request).json()
    assert body1["package_id"] != body2["package_id"]

    manifest1 = json.loads((package_root / ready["transformation_id"] / body1["package_id"] / "manifest.json").read_text())
    manifest2 = json.loads((package_root / ready["transformation_id"] / body2["package_id"] / "manifest.json").read_text())

    for name in ("train", "validation", "test"):
        bytes1 = Path(manifest1["splits"][name]["artifact_uri"].replace("file://", "")).read_bytes()
        bytes2 = Path(manifest2["splits"][name]["artifact_uri"].replace("file://", "")).read_bytes()
        assert bytes1 == bytes2, f"{name}.jsonl differs across identical runs"
        assert manifest1["splits"][name]["sha256"] == manifest2["splits"][name]["sha256"]


def test_same_input_and_config_produces_byte_identical_split_index(client: TestClient, package_root: Path) -> None:
    ready = _qc_ready(client, "sess_pkg_det_index")
    request = _default_request(ready["qc_id"])

    body1 = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request).json()
    body2 = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request).json()

    idx1 = (package_root / ready["transformation_id"] / body1["package_id"] / "split_index.jsonl").read_bytes()
    idx2 = (package_root / ready["transformation_id"] / body2["package_id"] / "split_index.jsonl").read_bytes()
    assert idx1 == idx2


def test_deterministic_group_ids_across_runs(client: TestClient, package_root: Path) -> None:
    ready = _qc_ready(client, "sess_pkg_det_groups")
    request = _default_request(ready["qc_id"])

    body1 = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request).json()
    body2 = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request).json()

    idx1 = [json.loads(l) for l in (package_root / ready["transformation_id"] / body1["package_id"] / "split_index.jsonl").read_text().splitlines()]
    idx2 = [json.loads(l) for l in (package_root / ready["transformation_id"] / body2["package_id"] / "split_index.jsonl").read_text().splitlines()]

    groups1 = {e["sample_id"]: e["group_id"] for e in idx1}
    groups2 = {e["sample_id"]: e["group_id"] for e in idx2}
    assert groups1 == groups2


def test_split_assignment_independent_of_package_id(client: TestClient, package_root: Path) -> None:
    ready = _qc_ready(client, "sess_pkg_det_assign")
    request = _default_request(ready["qc_id"])

    body1 = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request).json()
    body2 = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request).json()
    assert body1["package_id"] != body2["package_id"]

    idx1 = [json.loads(l) for l in (package_root / ready["transformation_id"] / body1["package_id"] / "split_index.jsonl").read_text().splitlines()]
    idx2 = [json.loads(l) for l in (package_root / ready["transformation_id"] / body2["package_id"] / "split_index.jsonl").read_text().splitlines()]
    splits1 = {e["sample_id"]: e["split"] for e in idx1}
    splits2 = {e["sample_id"]: e["split"] for e in idx2}
    assert splits1 == splits2


def test_different_seed_can_change_assignments_end_to_end(client: TestClient, package_root: Path) -> None:
    ready = _qc_ready(client, "sess_pkg_det_seed")
    request_a = _default_request(ready["qc_id"], seed=1)
    request_b = _default_request(ready["qc_id"], seed=999)

    body_a = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request_a).json()
    body_b = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request_b).json()

    idx_a = [json.loads(l) for l in (package_root / ready["transformation_id"] / body_a["package_id"] / "split_index.jsonl").read_text().splitlines()]
    idx_b = [json.loads(l) for l in (package_root / ready["transformation_id"] / body_b["package_id"] / "split_index.jsonl").read_text().splitlines()]
    splits_a = {e["sample_id"]: e["split"] for e in idx_a}
    splits_b = {e["sample_id"]: e["split"] for e in idx_b}
    assert splits_a != splits_b


def test_source_sample_order_preserved_within_each_split(client: TestClient, package_root: Path) -> None:
    ready = _qc_ready(client, "sess_pkg_order")
    request = _default_request(ready["qc_id"])
    body = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request).json()

    pkg_dir = package_root / ready["transformation_id"] / body["package_id"]
    for name in ("train", "validation", "test"):
        lines = (pkg_dir / f"{name}.jsonl").read_text().splitlines()
        indices = [json.loads(line)["window"]["index"] for line in lines]
        assert indices == sorted(indices), f"{name}.jsonl is not in source window order"


def test_split_index_in_source_order_across_all_splits(client: TestClient, package_root: Path) -> None:
    ready = _qc_ready(client, "sess_pkg_index_order")
    request = _default_request(ready["qc_id"])
    body = client.post(f"{PKG_URL}/{ready['transformation_id']}", json=request).json()

    pkg_dir = package_root / ready["transformation_id"] / body["package_id"]
    index_entries = [json.loads(line) for line in (pkg_dir / "split_index.jsonl").read_text().splitlines()]
    starts = [e["source_row_start"] for e in index_entries]
    assert starts == sorted(starts)
