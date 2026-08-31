<img width="2172" height="724" alt="Forge Data — robotics and Physical AI data infrastructure" src="https://github.com/user-attachments/assets/e93c3302-50d0-4067-827f-da2755580e69" />


# Forge Data

**Reproducible data infrastructure for robotics and Physical AI.**

**v1.0** · English · [中文](README.zh-CN.md) · [Full Technical Guide](docs/DETAILED_GUIDE.md)

![Python](https://img.shields.io/badge/python-3.12%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-009688)
![Tests](https://img.shields.io/badge/tests-878%20passing-brightgreen)

<!-- No banner asset is committed to this repository yet. -->

Forge Data turns raw, heterogeneous sensor streams into validated, synchronized,
quality-controlled, leakage-safe, lineage-tracked datasets for machine learning. It is built
for robotics and Physical AI workflows, where independently-clocked streams like IMU and GPS
have to move through deterministic, auditable preprocessing before they're trainable — and
where you need to be able to answer "where did this exact dataset come from?" months later.

## Forge Data v1.0

v1.0 is the first complete release of the local core pipeline — a single, coherent chain
from raw upload to a versioned, lineage-tracked dataset:

```
Ingestion → Validation → Integrity → Normalization → Synchronization
   → Cleaning → Transformation → Dataset QC → Packaging
   → Global Lineage & Dataset Registry
```

Every stage in that chain is implemented, tested, and wired together end to end — see
[Status](#status) for what that guarantees today and what's intentionally out of scope.

## Why Forge Data?

Robotics and multimodal sensor data has a set of problems that generic ML data tooling
doesn't address well:

- **Heterogeneous formats and inconsistent units** — one file in `g`, another in `m/s²`, a third with no unit recorded at all.
- **Independent clocks** — IMU and GPS streams drift relative to each other and need explicit temporal alignment, not a naive join.
- **Missing modalities and quality issues** — a sensor drops out mid-session; a bad batch shouldn't silently poison a dataset.
- **Overlapping windows and leakage** — feature-extraction windows that share source rows must never land on both sides of a train/test split.
- **Reproducibility and lineage** — "which raw files, which config, which code version produced this exact package?" needs a real answer, not a guess.

Forge Data's approach:

- **Immutable artifacts** — every stage writes once; a changed input produces a new artifact, never an in-place edit.
- **SHA-256 lineage everywhere** — every artifact and manifest is checksummed, and every stage records its upstream parent explicitly.
- **Deterministic transformations** — normalization, windowing, and splitting are configuration- and seed-driven, not incidental.
- **Explicit stage boundaries** — validation, integrity, normalization, synchronization, cleaning, and QC are separate, independently testable services.
- **Leakage-safe dataset packaging** — samples are grouped by source overlap *before* any split decision is made.
- **A reconstructible metadata catalog** — a SQLite index over the pipeline's own manifests, never a second source of truth.

## Pipeline overview

```mermaid
flowchart TD
    subgraph imu["IMU stream (built-in example)"]
        A1[Ingestion] --> A2[Schema Validation] --> A3[Integrity] --> A4[Normalization]
    end
    subgraph gps["GPS stream (built-in example)"]
        B1[Ingestion] --> B2[Schema Validation] --> B3[Integrity] --> B4[Normalization]
    end
    A4 --> SYNC[Synchronization]
    B4 --> SYNC
    SYNC --> CLEAN[Cleaning]
    CLEAN --> XFORM[Transformation]
    XFORM --> QC[Dataset QC]
    QC --> PKG[Packaging]
    PKG --> CAT[Catalog · Dataset Registry · Global Lineage]
```

IMU and GPS are the schemas and normalization profiles shipped with the repository today;
the ingestion → validation → integrity → normalization chain, synchronization's multi-stream
alignment, and the catalog's lineage graph are all schema-agnostic and designed to take
additional sensor types without changes to earlier stages.

## Core capabilities

| Stage | Responsibility | Key guarantees / output |
|---|---|---|
| **Ingestion** | Immutable raw upload | Streamed SHA-256 hashing, write-once storage, manifest per upload |
| **Schema Validation** | Per-record schema conformance | Structured error/warning reports; built-in IMU and GPS schemas |
| **Integrity** | Semantic/range/consistency checks | Deeper than schema shape — extreme values, ordering, per-schema checkers |
| **Normalization** | Canonical units and UTC timestamps | Deterministic derived artifact; pluggable per-schema profiles |
| **Synchronization** | Temporal alignment across streams | Nearest / linear-interpolation alignment, configurable tolerance, explicit clock correction |
| **Cleaning** | Filtering and redaction | Deterministic drop/redact policies, coverage and duplicate rules |
| **Transformation** | Feature extraction | Deterministic count/time windowing, handcrafted statistical + derived features |
| **Dataset QC** | Dataset-level quality control | Modality coverage, feature completeness, variance, and drift checks |
| **Packaging** | Train/validation/test generation | Group-aware, leakage-safe deterministic splitting; JSONL (+ optional Parquet) export |
| **Catalog** | Global lineage and dataset registry | SQLite index, rebuildable from filesystem manifests; dataset registry with immutable SemVer versions |

## Data flow example

```
Input:            imu.csv, gps.csv

Pipeline:         upload → validate → integrity → normalize → synchronize
                   → clean → transform → QC → package

Output:           train.jsonl
                   validation.jsonl
                   test.jsonl
                   split_index.jsonl
                   manifest.json          (per stage, at every step)
                   + lineage recorded in the catalog, traceable back to imu.csv / gps.csv
```

The full curl-by-curl walkthrough lives in the [Full Technical Guide](docs/DETAILED_GUIDE.md#end-to-end-demo).

## Design guarantees

**Immutable artifacts** — stages never rewrite upstream outputs. A raw upload, a validation
report, a normalized artifact, a package — once written, none of them are ever edited or
overwritten by a later stage.

**Deterministic execution** — normalization, windowing, and dataset splitting are driven by
explicit configuration and seeds, not incidental runtime state. Two independent runs over the
same bytes and configuration produce the same derived artifacts and the same reproducibility
fingerprint.

**Explicit lineage** — every artifact carries the ID and SHA-256 of its upstream parent(s).
The catalog turns this into an explicit parent → child DAG rather than an implied ordering.

**Separation of concerns** — schema validation, integrity checking, normalization,
synchronization, cleaning, and QC are deliberately separate services with their own storage
roots and their own test suites, not phases of one monolithic job.

**Leakage-safe packaging** — Step 7's overlapping feature windows are grouped by source-row
overlap *before* Step 9 makes any train/validation/test split decision, so no split can ever
divide a group of overlapping samples across partitions.

**Rebuildable catalog** — the SQLite catalog is an index, not a source of truth. It can be
deleted and fully reconstructed from the filesystem manifests any stage already writes.

## Quick start

```bash
git clone https://github.com/uudam42/forge-data.git
cd forge-data
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

uvicorn app.main:app --reload
pytest
```

Interactive API docs (Swagger UI) are served at **http://localhost:8000/docs** — every
endpoint can be explored and called from the browser without writing a single curl command.

Minimal example — upload a raw file:

```bash
curl -X POST http://localhost:8000/api/v1/ingestion/upload \
  -F "file=@imu.csv" -F "customer_id=demo" -F "device_id=imu_001"
# -> { "ingestion_id": "ing_...", "status": "stored", "sha256": "...", ... }
```

The full 10-stage curl walkthrough is in the [Full Technical Guide](docs/DETAILED_GUIDE.md).

## API surface

| Group | Prefix | Purpose |
|---|---|---|
| Ingestion | `/api/v1/ingestion` | Raw upload, immutable storage |
| Validation | `/api/v1/validation` | Per-record schema validation |
| Integrity | `/api/v1/integrity` | Semantic/range/consistency checks |
| Normalization | `/api/v1/normalization` | Canonical units and timestamps |
| Synchronization | `/api/v1/synchronization` | Multi-stream temporal alignment |
| Cleaning | `/api/v1/cleaning` | Filtering, deduplication, redaction |
| Transformation | `/api/v1/transformation` | Windowing and feature extraction |
| QC | `/api/v1/qc` | Dataset-level quality control |
| Packaging | `/api/v1/packaging` | Train/validation/test packaging and export |
| Catalog | `/api/v1/catalog` | Scan, rebuild, health, artifact lookup, verification |
| Lineage | `/api/v1/lineage` | Upstream/downstream traversal, impact analysis |
| Datasets | `/api/v1/datasets` | Dataset registry, versions, reproducibility |

Full endpoint reference, request/response shapes, and error codes: [Full Technical Guide](docs/DETAILED_GUIDE.md).

## Output structure

```
data/
  raw/            Immutable original uploads + manifests
  validation/     Schema validation reports
  integrity/      Integrity check reports
  normalized/     Canonical-unit artifacts
  synchronized/   Time-aligned multi-stream artifacts
  cleaned/        Filtered/redacted artifacts
  transformed/    Windowed feature artifacts
  qc/             Dataset QC reports
  packages/       Versioned train/validation/test packages
  catalog/        SQLite metadata catalog (catalog.db)
```

Runtime contents of every directory above are excluded from version control except a
`.gitkeep` placeholder — `data/` is regenerated by running the pipeline, never committed.

## Dataset versioning and lineage

A dataset version is an immutable pointer to exactly one package, with the full upstream
chain reconstructible from the catalog:

```
robotics_demo @ 1.0.0
      └─ package
            ├─ transformation
            │     └─ cleaning
            │           └─ synchronization
            │                 ├─ IMU normalization
            │                 └─ GPS normalization
            │                       └─ raw ingestions
            └─ QC report
```

- Registering a version against a *different* package than the one it already points to is
  rejected outright (`409 DATASET_VERSION_IMMUTABLE`) — a version is a permanent pointer.
- `POST /api/v1/catalog/verify/{type}/{id}?recursive=true` recomputes checksums for an
  artifact and its entire upstream lineage in one call.
- `GET /api/v1/datasets/{name}/versions/{version}/reproducibility` returns every content
  and config hash behind a package plus a single **lineage fingerprint** — a SHA-256 over
  that hash set, excluding execution IDs and timestamps, so two independent runs over
  equivalent data and configuration produce the identical fingerprint.
- `GET /api/v1/lineage/{type}/{id}/impact` reports downstream impact — what breaks, and
  which dataset versions are affected, if a given upstream artifact turns out to be bad.

## Testing

878 tests currently cover per-stage behavior, lineage gates, determinism, artifact
immutability, checksum validation, API contracts, and full end-to-end pipeline runs.

```bash
pytest
```

## Project structure

```
app/
  ingestion/ validation/ integrity/ normalization/   Per-stage services
  synchronization/ cleaning/ transformation/
  qc/ packaging/
  catalog/            Lineage graph, verification, dataset registry, SQLite catalog
  storage/            Immutable artifact stores (one per stage) + catalog store
  api/routes/         FastAPI routers, one per stage
tests/                878 tests
schemas/              Built-in IMU / GPS schema definitions
docs/DETAILED_GUIDE.md   Full architecture, API, and error-code reference
```

## Status

**Current release: Forge Data v1.0**

The v1.0 core pipeline is complete and validated end-to-end, with:

- immutable per-stage artifacts
- deterministic transformations
- dataset-level QC
- leakage-safe packaging
- global lineage
- a dataset version registry

The current implementation is **local-first and single-node**. Cloud storage, orchestration,
authentication, multi-tenancy, and a web dashboard are planned for the next phase — see
[Roadmap](#roadmap).

## Current scope

**Implemented:**
- Local filesystem–backed pipeline, end to end (ingestion through packaging and catalog)
- IMU and GPS as built-in schema/normalization-profile examples
- FastAPI HTTP API with interactive Swagger docs
- Deterministic processing, dataset QC, and leakage-safe packaging
- SQLite-backed metadata catalog, rebuildable from filesystem manifests

**Deliberately not implemented yet:**
- Cloud object storage backends (S3 / GCS / Azure Blob)
- Authentication, authorization, or multi-tenancy
- Distributed or orchestrated execution
- A web dashboard
- Automatic sensor schema inference
- A production-grade (non-SQLite) database deployment

## Roadmap

Realistic next steps, not commitments or dates:

- Pluggable cloud storage backends
- Pipeline job orchestration and run history
- Authentication and workspace isolation
- A web dashboard over the catalog and lineage graph
- Richer robotics connectors (e.g. ROS bag ingestion)
- Production observability (metrics, structured tracing)

## Documentation

- [Full Technical Guide](docs/DETAILED_GUIDE.md) — architecture, every API and error code, per-stage design notes, MVP limitations
- [中文文档](README.zh-CN.md)

## Contributing

This project doesn't yet have a formal contribution process — if you're interested in
contributing, open an issue to discuss what you have in mind before sending a pull request.
