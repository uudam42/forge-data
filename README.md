# Forge Data

**A production-grade data pipeline platform for robotics, physical AI, and multimodal sensor data.**

[中文版 README](README.zh-CN.md) · [Full Technical Guide](docs/DETAILED_GUIDE.md)

Raw sensor files in, versioned, reproducible, lineage-tracked ML-ready datasets out —
across 10 independently-testable stages, with a governance layer that ties them all together.

```
Status: 10 / 10 stages complete · 878 tests passing · FastAPI + Pydantic v2 + SQLite
```

---

## Why this exists

Robotics and physical-AI teams generate messy, multi-sensor time-series data (IMU, GPS, and more)
that has to survive a long trip before it's trainable: ingest it safely, validate it, check its
integrity, normalize units, synchronize streams that drift out of clock alignment, clean and
filter it, turn it into model-ready features, run dataset-level QC, package it into versioned
train/val/test splits — and, after all that, still be able to answer *"where did this exact
package come from, and can I reproduce it?"*

**Forge Data implements the whole chain as one coherent, incrementally-built platform** — every
stage is a bounded, independently testable service; nothing is a notebook script; every artifact
is immutable, checksummed, and traceable back to its raw source.

## The pipeline

```
 raw upload          schema        data           unit          multi-stream       filter /        feature          dataset            versioned
 (IMU / GPS)   -->   validation -> integrity   -> normalization -> synchronization -> cleaning ->  extraction   ->  QC          -->    package   --+
   Step 1            Step 2        Step 3          Step 4           Step 5            Step 6         Step 7           Step 8           Step 9      |
                                                                                                                                                     v
                                            +------------------------------------------------------------------------------------------------------+
                                            |
                                            v
                          Step 10 — Catalog, Global Lineage & Dataset Registry (SQLite index over every manifest above)
```

Every stage writes an immutable, checksummed artifact + manifest to its own storage root.
Step 10 never rewrites, reruns, or repairs any of them — it only reads, indexes, and verifies.

| # | Stage | What it does |
|---|---|---|
| 1 | **Ingestion** | Immutable raw upload, streamed SHA-256 hashing, no in-place overwrite |
| 2 | **Schema Validation** | Per-record schema conformance (IMU / GPS), structured error reports |
| 3 | **Data Integrity** | Deeper semantic/range/consistency checks beyond schema shape |
| 4 | **Normalization** | Canonical units + timestamps, pluggable per-schema profiles |
| 5 | **Synchronization** | Aligns independently-clocked streams (IMU + GPS...) onto one timeline |
| 6 | **Cleaning** | Deterministic filtering, deduplication, redaction policies |
| 7 | **Transformation** | Deterministic windowing + handcrafted feature generation |
| 8 | **Dataset QC** | Distribution/coverage/drift checks over a whole transformed dataset |
| 9 | **Packaging** | Leakage-safe, group-aware train/val/test splits, framework-neutral export |
| 10 | **Catalog & Lineage** | Cross-stage DAG, dataset registry, immutable SemVer versions, reproducibility fingerprint |

## Engineering principles

- **Immutable by construction** — every artifact is write-once; a change means a new ID, never an in-place edit.
- **Filesystem is the source of truth** — SQLite (introduced only at Step 10) is a rebuildable *index*, never the record of truth.
- **Fail loud, never silently repair** — validation, QC, and lineage verification report problems; nothing auto-fixes them.
- **Deterministic everywhere** — splitting, fingerprinting, and feature generation are seed-driven and reproducible, never random-by-default.
- **Every stage independently testable** — 878 tests, no stage's tests depend on another stage's runtime state.

## Quick start

```bash
git clone https://github.com/uudam42/forge-data.git
cd forge-data
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

uvicorn app.main:app --reload   # http://localhost:8000
pytest                          # 878 tests
```

Minimal end-to-end taste (full walkthrough in the [Full Technical Guide](docs/DETAILED_GUIDE.md)):

```bash
curl -X POST http://localhost:8000/api/v1/ingestion/upload \
  -F "file=@imu.csv" -F "customer_id=demo" -F "device_id=imu_001"
# -> { "ingestion_id": "ing_...", "status": "stored", ... }

curl -X POST http://localhost:8000/api/v1/catalog/rebuild   # index everything indexed so far
curl http://localhost:8000/api/v1/catalog/health            # -> { "status": "healthy", ... }
```

## Tech stack

Python 3.12+ · FastAPI · Pydantic v2 · pytest · stdlib `sqlite3` (no ORM) · local filesystem storage
(pluggable — the storage abstraction is designed for S3/GCS/Azure Blob backends later).

## Project layout

```
app/
    ingestion/ validation/ integrity/ normalization/    Steps 1-4
    synchronization/ cleaning/ transformation/            Steps 5-7
    qc/ packaging/                                        Steps 8-9
    catalog/                                               Step 10 — index, lineage, verification, dataset registry
    storage/         Per-stage immutable artifact stores + SQLite catalog store
    api/routes/       Thin HTTP layer — one router per stage
tests/                878 tests, one or more files per stage
schemas/              Built-in IMU / GPS schema definitions
docs/DETAILED_GUIDE.md   Full technical reference (architecture, API tables, error codes, MVP limitations)
```

## Status

All 10 planned stages are complete, tested, and demoed end-to-end against a live server.
No Step 11 work has started — see [Full Technical Guide § Deliberate MVP limitations](docs/DETAILED_GUIDE.md#deliberate-mvp-limitations)
for what's deliberately out of scope today (no cloud storage backends, no auth/RBAC, no distributed
execution, single-process SQLite only, and more).
