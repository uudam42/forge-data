# Forge Data

**A production-grade data pipeline platform for robotics, physical AI, and multimodal sensor data.**
面向机器人与物理 AI 的多模态传感器数据处理平台。

Raw sensor files in, versioned, reproducible, lineage-tracked ML-ready datasets out —
across 10 independently-testable stages, with a governance layer that ties them all together.
从原始传感器文件到可复现、可追溯血缘的机器学习就绪数据集，共 10 个可独立测试的阶段，并由统一的元数据治理层贯穿始终。

```
Status: 10 / 10 stages complete · 878 tests passing · FastAPI + Pydantic v2 + SQLite
状态：10/10 阶段全部完成 · 878 个测试通过 · FastAPI + Pydantic v2 + SQLite
```

📖 Looking for API details, error codes, and per-stage design docs? See the **[Full Technical Guide](docs/DETAILED_GUIDE.md)**.
需要完整 API 文档、错误码与各阶段设计细节？请查阅 **[完整技术文档](docs/DETAILED_GUIDE.md)**。

---

## Why this exists · 为什么做这个

Robotics and physical-AI teams generate messy, multi-sensor time-series data (IMU, GPS, and more)
that has to survive a long trip before it's trainable: ingest it safely, validate it, check its
integrity, normalize units, synchronize streams that drift out of clock alignment, clean and
filter it, turn it into model-ready features, run dataset-level QC, package it into versioned
train/val/test splits — and, after all that, still be able to answer *"where did this exact
package come from, and can I reproduce it?"*

机器人与物理 AI 团队产生的多传感器时间序列数据（IMU、GPS 等）往往很"脏"，在真正可用于训练之前要经历漫长的流程：
安全接入、模式校验、完整性检查、单位归一化、多流时钟同步、清洗过滤、特征工程、数据集级质量控制、
打包为带版本号的训练/验证/测试集——而且事后仍然要能回答一个问题："这份数据包到底从哪来的，我能不能复现它？"

**Forge Data implements the whole chain as one coherent, incrementally-built platform** — every
stage is a bounded, independently testable service; nothing is a notebook script; every artifact
is immutable, checksummed, and traceable back to its raw source.

**Forge Data 把这整条链路实现为一个连贯、逐步构建的平台**——每个阶段都是边界清晰、可独立测试的服务；
没有一处是笔记本脚本；每个产物都是不可变、带校验和、可追溯到原始来源的。

## The pipeline · 数据流水线

```
 raw upload          schema        data           unit          multi-stream       filter /        feature          dataset            versioned
 (IMU / GPS)   -->   validation -> integrity   -> normalization -> synchronization -> cleaning ->  extraction   ->  QC          -->    package   --+
   Step 1            Step 2        Step 3          Step 4           Step 5            Step 6         Step 7           Step 8           Step 9      |
   接入               模式校验       完整性检查        归一化            多流同步          清洗过滤        特征提取          数据集质控         版本化打包    |
                                                                                                                                                     v
                                            +------------------------------------------------------------------------------------------------------+
                                            |
                                            v
                          Step 10 — Catalog, Global Lineage & Dataset Registry (SQLite index over every manifest above)
                          第 10 步 — 元数据目录、全局血缘与数据集注册中心（对以上所有产物建立索引，而非另一处理阶段）
```

Every stage writes an immutable, checksummed artifact + manifest to its own storage root.
Step 10 never rewrites, reruns, or repairs any of them — it only reads, indexes, and verifies.

每个阶段都会写入一个不可变、带校验和的产物 + 清单（manifest），存放在各自独立的存储目录中。
第 10 步从不改写、重跑或修复任何上游产物——它只负责读取、建立索引和验证。

| # | Stage 阶段 | What it does 作用 |
|---|---|---|
| 1 | **Ingestion** 数据接入 | Immutable raw upload, streamed SHA-256 hashing, no in-place overwrite |
| 2 | **Schema Validation** 模式校验 | Per-record schema conformance (IMU / GPS), structured error reports |
| 3 | **Data Integrity** 完整性检查 | Deeper semantic/range/consistency checks beyond schema shape |
| 4 | **Normalization** 归一化 | Canonical units + timestamps, pluggable per-schema profiles |
| 5 | **Synchronization** 多流同步 | Aligns independently-clocked streams (IMU + GPS...) onto one timeline |
| 6 | **Cleaning** 清洗过滤 | Deterministic filtering, deduplication, redaction policies |
| 7 | **Transformation** 特征提取 | Deterministic windowing + handcrafted feature generation |
| 8 | **Dataset QC** 数据集质控 | Distribution/coverage/drift checks over a whole transformed dataset |
| 9 | **Packaging** 版本化打包 | Leakage-safe, group-aware train/val/test splits, framework-neutral export |
| 10 | **Catalog & Lineage** 元数据与血缘 | Cross-stage DAG, dataset registry, immutable SemVer versions, reproducibility fingerprint |

## Engineering principles · 工程设计原则

- **Immutable by construction** — every artifact is write-once; a change means a new ID, never an in-place edit.
  产物一律不可变——任何变化都产生新 ID，绝不原地修改。
- **Filesystem is the source of truth** — SQLite (introduced only at Step 10) is a rebuildable *index*, never the record of truth.
  文件系统是唯一的真相来源——SQLite（仅在第 10 步引入）只是可重建的索引，绝非记录本身。
- **Fail loud, never silently repair** — validation, QC, and lineage verification report problems; nothing auto-fixes them.
  只报告问题，绝不静默修复——校验、质控、血缘验证只汇报问题，不做任何自动修补。
- **Deterministic everywhere** — splitting, fingerprinting, and feature generation are seed-driven and reproducible, never random-by-default.
  处处确定性——切分、指纹计算、特征生成均由种子驱动、可复现，绝非默认随机。
- **Every stage independently testable** — 878 tests, no stage's tests depend on another stage's runtime state.
  每个阶段均可独立测试——878 个测试用例，各阶段测试互不依赖运行时状态。

## Quick start · 快速开始

```bash
git clone https://github.com/uudam42/forge-data.git
cd forge-data
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

uvicorn app.main:app --reload   # http://localhost:8000
pytest                          # 878 tests
```

Minimal end-to-end taste (full walkthrough in the [Full Technical Guide](docs/DETAILED_GUIDE.md)):
最简单的端到端体验（完整流程见 [完整技术文档](docs/DETAILED_GUIDE.md)）：

```bash
curl -X POST http://localhost:8000/api/v1/ingestion/upload \
  -F "file=@imu.csv" -F "customer_id=demo" -F "device_id=imu_001"
# -> { "ingestion_id": "ing_...", "status": "stored", ... }

curl -X POST http://localhost:8000/api/v1/catalog/rebuild   # index everything indexed so far
curl http://localhost:8000/api/v1/catalog/health            # -> { "status": "healthy", ... }
```

## Tech stack · 技术栈

Python 3.12+ · FastAPI · Pydantic v2 · pytest · stdlib `sqlite3` (no ORM) · local filesystem storage
(pluggable — the storage abstraction is designed for S3/GCS/Azure Blob backends later).

## Project layout · 项目结构

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

## Status · 当前状态

All 10 planned stages are complete, tested, and demoed end-to-end against a live server.
No Step 11 work has started — see [Full Technical Guide § Deliberate MVP limitations](docs/DETAILED_GUIDE.md#deliberate-mvp-limitations)
for what's deliberately out of scope today (no cloud storage backends, no auth/RBAC, no distributed
execution, single-process SQLite only, and more).

10 个计划阶段已全部完成、测试并在真实服务上完成端到端演示。第 11 步尚未开始——当前刻意未覆盖的范围
（云存储后端、鉴权/RBAC、分布式执行、单进程 SQLite 等）详见
[完整技术文档 · MVP 限制说明](docs/DETAILED_GUIDE.md#deliberate-mvp-limitations)。
