# AI Data Pipeline — Full Technical Guide

> This is the complete, stage-by-stage engineering reference (architecture,
> every API, every error code, every MVP limitation, per-step demos). For a
> quick overview, start with the project README instead:
> [English](../README.md) · [中文](../README.zh-CN.md).
>
> 本文档是完整的分阶段工程参考手册（架构设计、全部 API、错误码、MVP 限制、
> 各步骤演示）。如果只想快速了解项目概况，请先看根目录的 README：
> [English](../README.md) · [中文](../README.zh-CN.md)。

A pipeline platform for robotics / physical AI / multimodal sensor data,
built incrementally, one bounded stage at a time.

## Pipeline status

```
Step 1  Ingestion                  COMPLETE
Step 2  Schema Validation          COMPLETE
Step 3  Data Integrity Checks      COMPLETE
Step 4  Normalization              COMPLETE
Step 5  Multimodal Synchronization COMPLETE
Step 6  Cleaning / Filtering       COMPLETE
Step 7  Transformation / Feature Generation  COMPLETE
Step 8  Dataset QC                 COMPLETE
Step 9  Dataset Packaging          COMPLETE
Step 10 Versioning + Lineage       COMPLETE
v2.1    Crash Safety + Atomic Artifacts (cross-cutting, not a stage) COMPLETE
v2.2    Large-scale Streaming + Resource Bounds (cross-cutting, not a stage) COMPLETE
v2.3    Sensor / Schema Plugin System (cross-cutting, not a stage) COMPLETE
v2.4    Multiprocess Concurrency + SQLite Safety (cross-cutting, not a stage) COMPLETE
v2.5    Data Governance + Selective Rebuild (cross-cutting, not a stage) COMPLETE
```

No Step 11 work has been started.

---

## Step 1 — Ingestion Layer

Receives raw customer/device data over HTTP, verifies it, hashes it, and
stores it immutably — nothing more.

```
SOURCE -> RECEIVE -> IDENTIFY -> HASH -> STORE RAW -> WRITE MANIFEST
```

It does **not** validate CSV columns or sensor schemas, check for missing
data/duplicates/outliers, synchronize sensors, normalize units, or generate
features. Supported inputs: `.csv`, `.json`, `.jsonl`, `.zip`.

### Step 1 architecture

```
app/
    main.py                  FastAPI app assembly, /health
    api/routes/ingestion.py  HTTP layer: request parsing, error -> HTTP status mapping
    ingestion/service.py     Business logic: validation, orchestration, logging
    ingestion/models.py      Pydantic schemas (API response + manifest)
    storage/base.py          RawStorage abstraction (save/exists/get_path/write_manifest/
                              find_manifest/open_raw)
    storage/local.py         LocalRawStorage: filesystem implementation
    utils/hashing.py         Chunked SHA-256 (never loads a full file into memory)
    utils/ids.py             ID generation, isolated so UUID7 can replace UUID4 later
    utils/filenames.py       Filename sanitization / path traversal prevention
    core/config.py           Settings (env-var overridable)
    core/logging.py          Structured stdlib logging setup
```

**Storage is abstracted behind `RawStorage`.** `IngestionService` depends
only on that interface, never on filesystem details directly. Adding
`S3RawStorage`, `GCSRawStorage`, or `AzureBlobRawStorage` later means writing
a new class that implements the interface — the API and ingestion logic do
not change.

### Step 1 data flow

1. Client `POST`s a file + optional metadata to `/api/v1/ingestion/upload`.
2. The route layer extracts the multipart fields and hands them to
   `IngestionService.ingest(...)` as an `UploadRequest`.
3. The service sanitizes the filename, validates the extension, generates
   `ingestion_id` (always) and `session_id` (if not supplied), and resolves
   `customer_id` (defaulting to `anonymous`).
4. The service peeks the first chunk to reject empty uploads before touching
   storage, then streams the rest through `RawStorage.save()`, which hashes
   and writes in fixed-size chunks and enforces immutability (a repeat
   `ingestion_id` fails rather than overwrites).
5. A `manifest.json` is written next to the artifact via
   `RawStorage.write_manifest()`.
6. The API returns a JSON summary of the stored artifact.

### On-disk layout — raw storage

```
data/raw/
  <customer_id>/            "anonymous" if not provided
    <session_id>/
      <ingestion_id>/
        original/
          <sanitized_filename>
        manifest.json
```

### Ingestion example

```bash
curl -X POST http://localhost:8000/api/v1/ingestion/upload \
  -F "file=@sample.csv" \
  -F "customer_id=customer_001" \
  -F "device_id=imu_01"
```

```json
{
  "ingestion_id": "ing_e9d7679f-68a5-48b9-a494-7d4c3f329278",
  "session_id": "sess_1a212ed6-9243-43e5-8218-386595493bfb",
  "customer_id": "customer_001",
  "device_id": "imu_01",
  "source_type": null,
  "original_filename": "sample.csv",
  "content_type": "text/csv",
  "size_bytes": 52,
  "sha256": "fb3e51fc07184571e212cbad7e3f602910dd5570cf22d72217f21d0577fb08a7",
  "storage_uri": "file:///.../data/raw/customer_001/.../original/sample.csv",
  "status": "stored"
}
```

`GET /health` returns `{"status": "ok"}`.

---

## Step 2 — Schema Validation Engine

Validates the **structural** correctness of an already-ingested raw file
against a declared, versioned schema — without ever touching the raw file.

```
RAW IMMUTABLE DATA -> SCHEMA VALIDATION -> VALIDATION REPORT
```

Supported formats: `.csv`, `.json`, `.jsonl`. `.zip` archive contents are
**not** inspected — validating a ZIP-backed ingestion returns an explicit
`415 Unsupported Media Type` rather than silently doing nothing.

### Structural vs. semantic validation — an important boundary

Step 2 checks that fields exist, have the declared type, and (for
timestamps) parse as ISO-8601 with timezone. It does **not** judge whether a
value is physically or statistically reasonable:

| Value              | Step 2 (structural)      | Step 3 (semantic)     |
|---------------------|---------------------------|----------------------------------------|
| `latitude = 300`    | `float` → **valid**       | impossible GPS coordinate → `GPS_LATITUDE_OUT_OF_RANGE` |
| `accel_x = 999999.0`| `float` → **valid**       | implausible magnitude → `IMU_ACCELERATION_EXTREME` (warning) |

Range checks, physical plausibility, and outlier detection belong to Step 3
(Data Integrity Checks) — see below.

### Step 2 architecture

```
app/validation/
    models.py                 ValidationIssue / ValidationReport / API request-response models
    service.py                 ValidationService — resolves ingestion, retrieves schema,
                                selects validator, builds + persists the report
    registry.py                 ValidatorRegistry — maps file extension -> Validator
    validators/
        base.py                  RecordEvaluator, ErrorAccumulator, type-checking primitives
        csv_validator.py         Header-level + streaming per-row validation
        json_validator.py        Single object or top-level array
        jsonl_validator.py       One record per line, streaming
    schemas/
        base.py                  SchemaDefinition / FieldDefinition (the schema format)
        registry.py              SchemaRegistry — loads schemas/*.json, lookup by name+version

schemas/                      Built-in schema definitions (imu_v1.json, gps_v1.json)
data/validation/               Persisted validation reports (separate from data/raw/)
```

**The engine is schema-driven, not CSV-shaped.** No validator hardcodes a
field name — `RecordEvaluator` (in `validators/base.py`) checks any
`SchemaDefinition` against any record, and `csv_validator.py` /
`json_validator.py` / `jsonl_validator.py` differ only in how they turn a
file into `(index, record)` pairs. Adding a new schema (GPS, camera
metadata, force/torque, joint-state, a customer-defined schema) means
dropping a new JSON file into `schemas/` — no engine code changes.

`RawStorage` gained two read-only methods to support this without touching
any write path: `find_manifest(ingestion_id)` (locate an ingestion by ID
alone) and `open_raw(...)` (open the immutable artifact for reading).
Validation reports are persisted through a separate, small
`LocalValidationReportStore` — not `RawStorage` — so raw data structurally
cannot be reached by anything Step 2 does.

### Step 2 data flow

1. Client `POST`s `{"schema_name", "schema_version"}` to
   `/api/v1/validation/{ingestion_id}`.
2. `ValidationService` looks up the ingestion's manifest by `ingestion_id`
   (404 if not found), retrieves the requested `SchemaDefinition` from the
   `SchemaRegistry` (404 if not found), and picks a `Validator` from the
   `ValidatorRegistry` based on the raw file's extension (415 if
   unsupported, e.g. `.zip`).
3. The raw file is opened **read-only** via `RawStorage.open_raw()` and
   streamed through the validator, which checks each record against the
   schema and accumulates issues in an `ErrorAccumulator` (capped at
   `MAX_VALIDATION_ERRORS`).
4. A `ValidationReport` is built — including `raw_sha256` copied from the
   ingestion manifest, establishing the first lineage link — and persisted
   via `LocalValidationReportStore`.
5. The API returns a summary; the full report (including all captured
   errors) lives at `report_uri`.

A structurally invalid dataset is **not** a server error: the endpoint
returns `HTTP 200` with `"status": "failed"`. `4xx`/`5xx` is reserved for
request-level problems (ingestion not found, schema not found, unsupported
file type) and genuine system failures.

### On-disk layout — validation reports

```
data/validation/
  <ingestion_id>/
    <validation_id>/
      report.json
```

### Schema definition format

A small JSON format owned by this application — five field types, a flat
field map, no nested-object validation (kept intentionally simple; extend
`FieldType` / `SchemaDefinition` in `app/validation/schemas/base.py` as real
needs arise).

```json
{
  "schema_name": "imu",
  "schema_version": "1.0.0",
  "record_type": "tabular",
  "fields": {
    "timestamp": { "type": "datetime", "required": true, "nullable": false, "format": "iso8601" },
    "accel_x":   { "type": "float",    "required": true,  "nullable": false },
    "device_id": { "type": "string",   "required": false, "nullable": true }
  },
  "allow_extra_fields": false,
  "metadata_requirements": { "sensor_type": "imu" }
}
```

Field types: `string`, `integer`, `float`, `boolean`, `datetime`.

- **required** — the field must be present (its absence is `MISSING_REQUIRED_FIELD`).
- **nullable** — a present-but-empty/`null` value is allowed (violation is `NULL_NOT_ALLOWED`).
  These are checked independently, matching the required-but-null vs.
  missing-entirely distinction.
- **allow_extra_fields** — `false` means any field not declared in the
  schema is `UNEXPECTED_FIELD`; `true` means extras are ignored (never
  deleted or transformed — they're just not checked).
- **metadata_requirements** — checked against the *ingestion manifest*, not
  per-record data. `"sensor_type": "imu"` is checked against the
  manifest's `source_type` field, **only when that field was actually
  provided at ingestion time**. If `source_type` was never supplied, the
  check is skipped rather than treated as a violation — Step 1 doesn't
  require `source_type`, and Step 2 must not retroactively demand it. An
  explicit mismatch (e.g. validating GPS-sourced data against the IMU
  schema) is flagged as `METADATA_REQUIREMENT_NOT_MET`.

### Built-in schemas

**`schemas/imu_v1.json`** — `imu` v`1.0.0`
Required: `timestamp`, `accel_x`, `accel_y`, `accel_z`.
Optional: `gyro_x`, `gyro_y`, `gyro_z`, `device_id`.

**`schemas/gps_v1.json`** — `gps` v`1.0.0`
Required: `timestamp`, `latitude`, `longitude`.
Optional: `altitude`, `speed`, `device_id`.
`latitude`/`longitude` are **type-checked only** here (`float`) — range
validation (`[-90, 90]`, etc.) is semantic and is what Step 3 checks.

Both require ISO-8601 timestamps **with timezone info**. Naive timestamps
(no UTC offset, no `Z`) are rejected as `INVALID_TIMESTAMP`:

| Timestamp                          | Valid? |
|--------------------------------------|--------|
| `2026-08-29T18:34:22Z`                | yes    |
| `2026-08-29T18:34:22+00:00`           | yes    |
| `2026-08-29T11:34:22-07:00`           | yes    |
| `2026-08-29 18:34:22`                 | no — no timezone |

### CSV type-conversion rules

CSV cells are always strings — conversion is explicit and deterministic,
never a silent coercion:

| Type      | `"123"` | `"123.4"` | `"abc"` | `""` (empty cell) |
|-----------|---------|-----------|---------|-------------------|
| integer   | valid   | **invalid** | invalid | treated as null |
| float     | valid   | valid     | invalid | treated as null |
| boolean   | only the literal strings `"true"` / `"false"`, case-insensitive — nothing else (`"1"`, `"yes"` are rejected) | | | treated as null |

### Validation error codes

`MISSING_REQUIRED_FIELD`, `INVALID_TYPE`, `NULL_NOT_ALLOWED`,
`INVALID_TIMESTAMP`, `UNEXPECTED_FIELD`, `INVALID_RECORD`, `EMPTY_DATASET`,
`SCHEMA_NOT_FOUND`, `UNSUPPORTED_FILE_TYPE`, and
`METADATA_REQUIREMENT_NOT_MET` (an addition beyond the minimum set, for the
`metadata_requirements` check above).

### Validation endpoint

```
POST /api/v1/validation/{ingestion_id}
Content-Type: application/json

{ "schema_name": "imu", "schema_version": "1.0.0" }
```

```bash
curl -X POST http://localhost:8000/api/v1/validation/<INGESTION_ID> \
  -H "Content-Type: application/json" \
  -d '{"schema_name": "imu", "schema_version": "1.0.0"}'
```

Passed:

```json
{
  "validation_id": "val_5ef0d5bb-2357-45b3-b2d0-260195d1edf6",
  "ingestion_id": "ing_3a0caa85-8d41-4008-822c-7e0995a70c1e",
  "schema": { "name": "imu", "version": "1.0.0" },
  "status": "passed",
  "summary": {
    "records_checked": 3,
    "valid_records": 3,
    "invalid_records": 0,
    "error_count": 0,
    "warning_count": 0
  },
  "report_uri": "file:///.../data/validation/<ingestion_id>/<validation_id>/report.json"
}
```

Failed:

```json
{
  "validation_id": "val_270d3ce6-10fd-4058-a6b0-d423d92b0f80",
  "ingestion_id": "ing_3f1c2452-4622-4733-b300-875a6ec0037f",
  "schema": { "name": "imu", "version": "1.0.0" },
  "status": "failed",
  "summary": {
    "records_checked": 3,
    "valid_records": 1,
    "invalid_records": 2,
    "error_count": 4,
    "warning_count": 0
  },
  "report_uri": "file:///.../data/validation/<ingestion_id>/<validation_id>/report.json"
}
```

The persisted `report.json` additionally includes `validated_at`,
`raw_sha256` (copied from the ingestion manifest — the first lineage link:
`validation report -> raw ingestion checksum`), and the full `errors` /
`warnings` arrays, e.g.:

```json
{
  "errors": [
    { "record": null, "field": "pressure", "code": "UNEXPECTED_FIELD", "message": "Unexpected field 'pressure' is not defined in schema" },
    { "record": 2, "field": "timestamp", "code": "INVALID_TIMESTAMP", "message": "Expected ISO-8601 datetime with timezone but received '2026-08-29 18:34:23'" },
    { "record": 2, "field": "accel_x", "code": "INVALID_TYPE", "message": "Expected float but received 'abc'" },
    { "record": 3, "field": "accel_y", "code": "NULL_NOT_ALLOWED", "message": "Field 'accel_y' is null but nullable=false" }
  ],
  "errors_truncated": false
}
```

Note `record: null` for the `UNEXPECTED_FIELD` issue: missing-required and
unexpected-column checks for CSV run once against the header, not once per
row — repeating the same header-level finding for every row would just
produce noise.

---

## Step 3 — Data Integrity Checks

Runs **after** a raw file has already passed Step 2 schema validation, and
answers a different question than Step 2 does:

> **Schema Validation:** "Is this record structurally valid?"
> **Integrity Checks:** "Are structurally valid values plausible and
> internally consistent?"

```
VALIDATION REPORT -> INTEGRITY CHECKS -> INTEGRITY REPORT
```

Step 3 is **not** data cleaning. It never mutates raw files, repairs or
transforms records, interpolates missing values, normalizes units,
deduplicates, resamples, or synchronizes across files/sensors — it only
reports. Those all belong to later pipeline stages.

### Validation dependency (lineage gate)

Integrity checks cannot run against just any ingestion — they require an
existing, **passing** Step 2 validation report that matches the *current*
raw file exactly:

```
raw ingestion  ->  validation report  ->  integrity report
```

Before running any check, `IntegrityService` looks up validation reports
for the ingestion (via `ValidationReportStore.find_reports`, isolated
filesystem lookup — see MVP limitations) and requires one where
`schema_name`, `schema_version`, **and** `raw_sha256` all match the
ingestion's current manifest. If the raw file's checksum ever diverged from
what was validated, no report will match, and the run is rejected — this is
what prevents integrity checks from ever running against a stale
validation. The most recent matching report (by `validated_at`) is used,
and its `status` must be `"passed"`.

### Integrity endpoint

```
POST /api/v1/integrity/{ingestion_id}
Content-Type: application/json

{ "schema_name": "imu", "schema_version": "1.0.0" }
```

| Condition                                                    | Status |
|----------------------------------------------------------------|--------|
| Ingestion not found                                             | 404    |
| Schema not found                                                 | 404    |
| No validation report matches this schema + the current raw checksum | 409 |
| A matching validation report exists but did not pass             | 409    |
| Unsupported file type (e.g. `.zip`) or no checker for this schema | 415    |
| Integrity checks executed                                        | 200    |

Exactly like Step 2, a **failed** integrity run is a normal, successful API
call — `HTTP 200` with `"status": "failed"`. 4xx/5xx is reserved for
request-level problems, not data-quality outcomes.

```bash
curl -X POST http://localhost:8000/api/v1/integrity/<INGESTION_ID> \
  -H "Content-Type: application/json" \
  -d '{"schema_name": "gps", "schema_version": "1.0.0"}'
```

```json
{
  "integrity_id": "integ_48f2fe9e-73a3-4a68-ae8d-5df4da8ba850",
  "ingestion_id": "ing_0818da5c-5ab4-4139-9339-34f75dbbd8dc",
  "validation_id": "val_ce6b28a3-925e-407f-bb27-99fcebd1c0c7",
  "schema": { "name": "gps", "version": "1.0.0" },
  "status": "passed",
  "total_records": 3,
  "checked_records": 3,
  "passed_records": 3,
  "failed_records": 0,
  "warning_count": 0,
  "error_count": 0,
  "report_uri": "file:///.../data/integrity/<ingestion_id>/<integrity_id>/report.json"
}
```

### Status model

- **`passed`** — no errors, no warnings.
- **`passed_with_warnings`** — no errors, but at least one warning (e.g. an
  extreme-but-plausible sensor reading, or a duplicate timestamp).
- **`failed`** — at least one error.

Only **errors** fail a record and fail the run; **warnings** never do.

### Built-in GPS checks (`gps` schema)

| Check                | Code                        | Severity |
|-----------------------|------------------------------|----------|
| Latitude in `[-90, 90]`    | `GPS_LATITUDE_OUT_OF_RANGE`   | error |
| Longitude in `[-180, 180]` | `GPS_LONGITUDE_OUT_OF_RANGE`  | error |
| `speed >= 0` (if present)  | `GPS_NEGATIVE_SPEED`          | error |
| Non-decreasing timestamps  | `TIMESTAMP_OUT_OF_ORDER`      | error |
| Adjacent duplicate timestamp | `DUPLICATE_TIMESTAMP`       | warning |
| Finite lat/lon/speed/altitude | `NON_FINITE_VALUE`         | error |

Latitude/longitude bounds are physical facts (`GpsLimits` in
`app/integrity/checks/gps.py`), not tunable heuristics.

### Built-in IMU checks (`imu` schema)

| Check                         | Code                       | Severity |
|---------------------------------|-----------------------------|----------|
| Non-decreasing timestamps        | `TIMESTAMP_OUT_OF_ORDER`    | error |
| Adjacent duplicate timestamp      | `DUPLICATE_TIMESTAMP`       | warning |
| Finite accel/gyro values          | `NON_FINITE_VALUE`          | error |
| `\|accel\| <= 200 m/s²` (default)    | `IMU_ACCELERATION_EXTREME`  | warning |
| `\|gyro\| <= 50 rad/s` (default)     | `IMU_GYRO_EXTREME`          | warning |

**The 200 m/s² / 50 rad/s thresholds are plausibility defaults, not
universal physical guarantees.** A real IMU on a rocket sled or in a crash
test could legitimately exceed them — that's exactly why exceeding them is
a *warning*, not an error, and why `ImuThresholds` (in
`app/integrity/checks/imu.py`) is a constructor parameter rather than a
literal buried in the check logic. Tune per fleet/use-case as needed.

`NON_FINITE_VALUE` exists because Step 2 cannot catch it: Python's `float()`
happily parses `"nan"`/`"inf"`/`"-inf"` from a CSV cell, and `json.loads`
accepts bare `NaN`/`Infinity` tokens by default, so a non-finite sensor
value passes Step 2's `INVALID_TYPE` check (it *is* a float) yet is not
usable data. Step 3 explicitly rejects it.

### Architecture

```
app/integrity/
    models.py                IntegrityIssue / IntegrityReport / API request-response models
                              (SchemaRef is reused from app.validation.models)
    service.py                 IntegrityService — verifies the validation-lineage gate,
                                selects a checker, builds + persists the report
    registry.py                 IntegrityCheckerRegistry — maps schema_name -> IntegrityChecker
    records.py                  Streaming (record_number, record) readers, keyed by file
                                 extension — independent of Step 2's validators
    checks/
        base.py                  IntegrityChecker interface, IntegrityIssueAccumulator,
                                  to_float()/parse_timestamp() (CSV-string vs. JSON-native
                                  values normalized through one path)
        common.py                Reusable, schema-agnostic checks: TimestampSequenceChecker
                                  (ordering + duplicates, O(1) state) and check_finite()
        gps.py                   GpsIntegrityChecker + GpsLimits
        imu.py                   ImuIntegrityChecker + ImuThresholds

app/storage/integrity_store.py   IntegrityReportStore / LocalIntegrityReportStore
data/integrity/                   Persisted integrity reports (separate from data/raw/
                                   and data/validation/)
```

Two orthogonal registries mirror Step 2's shape but split along a different
axis: `ValidatorRegistry` (Step 2) is keyed by **file extension** (how to
read a file); `IntegrityCheckerRegistry` (Step 3) is keyed by **schema_name**
(what semantics to check), because integrity checks are inherently
schema-specific (GPS bounds mean nothing to an IMU record) while *reading*
a file is not. File reading for Step 3 is handled once, in
`app.integrity.records`, independent of which checker runs afterward.

`app.integrity.records` is deliberately **not** a reuse of Step 2's
`CsvValidator`/`JsonValidator`/`JsonlValidator` — those are tightly coupled
to structural validation (`ErrorAccumulator`, `RecordEvaluator`, header-vs-
row presence checks) and produce validation issues, not raw field values.
Integrity checkers need the actual parsed values, so Step 3 owns its own
thin per-format iteration instead of retrofitting Step 2's classes to serve
two different jobs.

`ValidationReportStore` gained one read-only method to support the lineage
gate without touching its write path: `find_reports(ingestion_id)`.
Filesystem globbing stays inside the store; `IntegrityService` only filters
the already-loaded report dicts by schema + checksum — it never globs the
filesystem itself.

### On-disk layout — integrity reports

```
data/integrity/
  <ingestion_id>/
    <integrity_id>/
      report.json
```

### Streaming behavior

CSV and JSONL integrity checking are streaming — `app.integrity.records`
yields one `(record_number, record)` pair at a time via a generator, and
`TimestampSequenceChecker` needs only the *previous* record's timestamp
(O(1) state) to detect both ordering violations and duplicates, regardless
of file size. JSON arrays are still parsed fully into memory, matching the
same documented Step 2 MVP limitation.

### Error accumulation

`IntegrityIssueAccumulator` (`app/integrity/checks/base.py`) mirrors Step
2's `ErrorAccumulator`, capped at `MAX_INTEGRITY_ISSUES` (default `1000`).
Unlike Step 2 — which keeps separate `errors`/`warnings` lists — Step 3
persists one unified `issues` array with a per-issue `severity`, so both
severities share the same truncation budget. Once the cap is reached,
`error_count`/`warning_count` keep incrementing accurately, but no further
detailed issue objects are stored, and `issues_truncated: true` is set on
the persisted report.

### Integrity report

Persisted `report.json` includes the full lineage chain and per-issue
detail (never a full raw record — only the single offending scalar, if
any):

```json
{
  "integrity_id": "integ_18ef0592-3c8e-4946-8c2b-ca28868a44fe",
  "ingestion_id": "ing_486294a5-4dbe-42a3-9dc5-70ea357207cd",
  "validation_id": "val_c9390e74-00d3-4bb3-a2d6-3a70ce15eff6",
  "customer_id": "demo_customer",
  "device_id": "gps_02",
  "schema_name": "gps",
  "schema_version": "1.0.0",
  "source_filename": "gps_invalid.csv",
  "raw_sha256": "677292bb652153bc584e0bcf3a00fdd9f7f58fd2d07ba27910f1321a406301dc",
  "status": "failed",
  "total_records": 2,
  "checked_records": 2,
  "passed_records": 1,
  "failed_records": 1,
  "warning_count": 0,
  "error_count": 3,
  "issues": [
    { "record_number": 2, "field": "latitude", "code": "GPS_LATITUDE_OUT_OF_RANGE", "severity": "error", "message": "Latitude 120.0 is outside the valid range [-90.0, 90.0]", "value": 120.0 },
    { "record_number": 2, "field": "speed", "code": "GPS_NEGATIVE_SPEED", "severity": "error", "message": "Speed -3.0 is negative", "value": -3.0 },
    { "record_number": 2, "field": "timestamp", "code": "TIMESTAMP_OUT_OF_ORDER", "severity": "error", "message": "Timestamp '2026-08-29T18:00:01Z' is earlier than the previous record's timestamp", "value": "2026-08-29T18:00:01Z" }
  ],
  "issues_truncated": false,
  "created_at": "2026-08-30T08:04:33.808641Z"
}
```

### Logging

`INTEGRITY_STARTED` / `INTEGRITY_COMPLETED` / `INTEGRITY_FAILED`, with
`integrity_id`, `ingestion_id`, `validation_id`, `schema_name`,
`schema_version`, and (on completion) `status` + summary counts — never raw
rows, raw sensor values, or file contents.

---

## Step 4 — Normalization Engine

Transforms structurally-valid, integrity-checked data into a **canonical
representation** — consistent units, consistent field names, consistent
timestamp format — for downstream multimodal synchronization and ML
processing. Unlike Steps 2-3, Step 4 produces a genuinely new artifact.

```
RAW IMMUTABLE DATA -> SCHEMA VALIDATION -> DATA INTEGRITY -> NORMALIZATION -> CANONICAL NORMALIZED DATA
```

### Normalization vs. cleaning — an important boundary

> **Schema Validation:** "Is this record structurally valid?"
> **Integrity Checks:** "Are structurally valid values plausible and internally consistent?"
> **Normalization:** "Is the data *represented* consistently?"

Step 4 is **not** cleaning. It never removes outliers, repairs or imputes
missing values, interpolates missing samples, deduplicates, smooths or
resamples signals, synchronizes modalities, drops sessions, infers labels,
or splits datasets. It only re-represents values that are already
structurally and semantically acceptable — units, timezone, field names,
numeric formatting. A record that can't be *deterministically* normalized
under the given config fails the whole run rather than being silently
skipped or imputed (see "Error behavior" below).

### Lineage gate

Normalization requires **both** an accepted validation report and an
accepted integrity report matching the *current* raw file exactly —
stricter than Step 3's single-report gate, because there are now two
upstream stages to trust:

```
raw ingestion -> validation report -> integrity report -> normalized artifact
```

`NormalizationService.normalize()` checks, in order:

1. resolve the ingestion manifest (404 if not found),
2. retrieve the requested schema (404) and normalization profile (404),
3. find the most recent validation report matching `schema_name` +
   `schema_version` + the manifest's current `sha256` (409 if none — this
   is what makes a stale raw checksum impossible to sneak past: if the raw
   bytes ever diverged from what was validated, nothing matches), and
   require its `status == "passed"` (409 otherwise),
4. find the most recent integrity report matching the same three fields
   (409 if none), and cross-check that **its `validation_id` equals the
   validation report found in step 3** (409 on mismatch — this catches an
   integrity report that references a different, unrelated validation run
   even if both happen to reference the same raw checksum),
5. require the integrity report's `status` to be `passed` or
   `passed_with_warnings` (409 if `failed`),
6. confirm the file type is supported (415 for `.zip` or anything else
   unregistered),
7. only then build a `RecordNormalizer` for the profile — which itself
   fails fast (400) if a required source unit is missing or unsupported,
   *before* touching any storage or reading a single record.

As an extra defense-in-depth check (beyond trusting the manifest), the raw
file's SHA-256 is also **recomputed** while streaming it for normalization,
in the same pass used to read records — a mismatch against the manifest's
recorded checksum surfaces as a 500 (a storage-layer invariant violation,
not a normal request condition), never silently ignored.

### Normalization endpoint

```
POST /api/v1/normalization/{ingestion_id}
Content-Type: application/json

{
  "schema_name": "imu",
  "schema_version": "1.0.0",
  "profile_name": "imu_canonical",
  "profile_version": "1.0.0",
  "source_units": { "acceleration": "g", "angular_velocity": "deg/s" }
}
```

| Condition                                                          | Status |
|------------------------------------------------------------------------|--------|
| Ingestion, schema, or normalization profile not found                    | 404 |
| No matching validation/integrity report, a matching report exists but didn't pass, or an integrity report references a different validation run | 409 |
| Unsupported file type (e.g. `.zip`) or no checker for this schema        | 415 |
| Missing/unsupported source unit, ambiguous field mapping, a record can't be deterministically converted, or the request itself is invalid | 400 |
| Normalization completed                                                  | 200 |
| An actual internal failure (including a raw-checksum mismatch against the manifest) | 500 |

There is no `"failed"` normalization status: a run either fully succeeds
and commits (`"status": "completed"`), or fails outright and commits
**nothing** — unlike Steps 2-3, there is no persisted record of a failed
attempt, because nothing partial is ever written where a caller could find it.

```bash
curl -X POST http://localhost:8000/api/v1/normalization/<INGESTION_ID> \
  -H "Content-Type: application/json" \
  -d '{"schema_name": "imu", "schema_version": "1.0.0", "profile_name": "imu_canonical", "profile_version": "1.0.0", "source_units": {"acceleration": "g", "angular_velocity": "deg/s"}}'
```

```json
{
  "normalization_id": "norm_8ee3a092-12f8-431f-b65a-55eef2b122c9",
  "ingestion_id": "ing_ac1fe0f1-cf7f-4624-ae29-c80eb7b382e2",
  "validation_id": "val_fb1965de-e2b5-4135-b9e8-3b9e34ac80ea",
  "integrity_id": "integ_28a0c5f9-2db7-4b28-8772-8784e3cfbe84",
  "schema": { "name": "imu", "version": "1.0.0" },
  "profile": { "name": "imu_canonical", "version": "1.0.0" },
  "status": "completed",
  "records_written": 2,
  "artifact_uri": "file:///.../data/normalized/<ingestion_id>/<normalization_id>/normalized.csv",
  "normalized_sha256": "869169463149b1a9ccbf94bd4aa0cad4a034ad0f3f75e3dd4f46ec260060af88"
}
```

### Architecture

```
app/normalization/
    models.py                 NormalizationManifest / API request-response models
                                (SchemaRef reused from app.validation.models)
    service.py                  NormalizationService — enforces the lineage gate, resolves
                                 the profile, streams source -> normalize -> write, commits
    registry.py                  NormalizationProfileRegistry — (schema_name, schema_version,
                                  profile_name, profile_version) -> NormalizationProfile
    records.py                   Source-record reading (reuses app.integrity.records) +
                                  normalized-record writing (CSV/JSONL streamed, JSON array)
    profiles/
        base.py                   NormalizationProfile (declarative) + RecordNormalizer
                                   (the one engine that interprets any profile)
        imu.py                    IMU_CANONICAL_V1 — aliases, acceleration/angular_velocity dimensions
        gps.py                    GPS_CANONICAL_V1 — aliases, altitude/speed dimensions
    transforms/
        units.py                  UnitDimension — factor-based conversion (g, deg/s, ft, km/h, mph)
        timestamps.py             normalize_timestamp — canonical UTC "Z" representation
        fields.py                 resolve_field_names — explicit alias resolution + ambiguity detection
        common.py                 to_float (reused from app.integrity.checks.base), to_bool, is_finite

app/storage/normalized_store.py   NormalizedArtifactStore / LocalNormalizedArtifactStore
                                   (staging -> atomic commit; never via RawStorage)
data/normalized/                   Persisted normalized artifacts + manifests
```

**Profile lookup is a second, orthogonal registry — schema-driven, not
CSV-shaped.** Just as Step 2/3 split "how to read a file" (extension) from
"what to check" (schema), Step 4 keeps profile resolution completely
explicit: `registry.get(schema_name=..., schema_version=..., profile_name=...,
profile_version=...)` — no implicit "latest," no fallback. Profile
versioning is deliberately independent of schema versioning: a new
`imu_canonical` v`1.1.0` can ship without touching `imu` v`1.0.0`, and vice
versa. A profile is pure declarative data (field aliases, which canonical
fields need unit conversion and to which dimension); `RecordNormalizer` in
`profiles/base.py` is the one engine that interprets *any* profile against
*any* record — `imu.py`/`gps.py` declare data, they implement no logic.
Canonical field order and required/nullable semantics are read directly
from the target `SchemaDefinition` rather than redeclared, since for both
built-in profiles the canonical field set is exactly the schema's field set.

**Source-record reading reuses `app.integrity.records`** rather than a
third reimplementation of the same CSV/JSON/JSONL parsing already written
for Step 2's validators and Step 3 — that logic has no integrity-specific
semantics, so importing it directly (wrapped with a normalization-specific
exception type) avoids duplication. Writing normalized output is new:
`app/normalization/records.py` streams CSV and JSONL, and always emits a
top-level JSON *array* for `.json` output regardless of whether the source
was a single object or an array (a documented MVP simplification).

**Normalized artifacts get their own store**, `LocalNormalizedArtifactStore`
— never `RawStorage`, and not folded into the validation/integrity report
stores either, since a normalization run produces genuinely new *data*, not
a report about existing data.

### Atomic commit

A normalization run writes into a hidden staging directory first —
`data/normalized/<ingestion_id>/.tmp-<normalization_id>/` — and only
becomes discoverable under its final `<normalization_id>` via
`Path.rename()`, which is atomic on POSIX filesystems when source and
destination share a filesystem (true here, since both live under the same
per-ingestion directory). A record-level conversion failure partway through
a large file — the required MVP behavior, since a record that already
passed validation/integrity must not be silently skipped — triggers
`discard()` (best-effort `shutil.rmtree`) on the staging directory and
propagates the error; nothing valid-looking is ever left behind, and an
existing committed run can never be overwritten (`commit()` raises if the
final directory already exists).

### On-disk layout — normalized artifacts

```
data/normalized/
  <ingestion_id>/
    <normalization_id>/
      normalized.csv        (or normalized.json / normalized.jsonl)
      manifest.json
```

### Canonical timestamp format

`YYYY-MM-DDTHH:MM:SS[.ffffff]Z` — UTC, ISO-8601, always `Z`. The fractional
component appears (always at 6-digit/microsecond resolution) only when the
source timestamp carried one — sub-second precision is preserved, never
invented:

| Input                                  | Normalized output              |
|-------------------------------------------|-----------------------------------|
| `2026-08-30T18:00:00-07:00`                | `2026-08-31T01:00:00Z`            |
| `2026-08-30T18:00:00.123456-07:00`         | `2026-08-31T01:00:00.123456Z`     |
| `2026-08-30T18:00:00+00:00`                | `2026-08-30T18:00:00Z`            |

This only *re-represents* an already-valid timestamp (Step 2 already
guarantees timezone-aware ISO-8601) — it never resamples, interpolates, or
changes sampling frequency.

### Canonical IMU representation (`imu_canonical` v`1.0.0`)

Canonical fields (identical to the `imu` schema's field set): `timestamp,
accel_x, accel_y, accel_z, gyro_x, gyro_y, gyro_z, device_id`.

| Quantity          | Canonical unit | Supported source units |
|---------------------|------------------|---------------------------|
| acceleration          | m/s²               | `m/s^2` (identity), `g` (× `9.80665`) |
| angular velocity      | rad/s              | `rad/s` (identity), `deg/s` (× `π/180`) |

Declared aliases (explicit, not fuzzy-matched): `"Accel X"/"acc_x"/"ax"` →
`accel_x` (and the `y`/`z` equivalents), `"Gyro X"/"gx"` → `gyro_x` (and
`y`/`z`). Two aliases resolving to the same canonical field within one
record — e.g. both `"Accel X"` and `"acc_x"` present — raises
`AMBIGUOUS_FIELD_MAPPING` rather than silently picking one.

Worked example — input with a non-UTC timezone, acceleration in `g`, gyro
in `deg/s`:

```
timestamp,Accel X,Accel Y,Accel Z,gyro_x,gyro_y,gyro_z
2026-08-30T18:00:00-07:00,1.0,0.0,-1.0,180,0,-180
```

```
timestamp,accel_x,accel_y,accel_z,gyro_x,gyro_y,gyro_z
2026-08-31T01:00:00Z,9.80665,0.0,-9.80665,3.141592653589793,0.0,-3.141592653589793
```

### Canonical GPS representation (`gps_canonical` v`1.0.0`)

Canonical fields: `timestamp, latitude, longitude, altitude, speed,
device_id`. Latitude/longitude are **decimal-degree passthroughs** — no
coordinate-reference-system conversion is supported in this MVP (no GIS
dependency has been introduced).

| Quantity | Canonical unit | Supported source units |
|------------|-------------------|---------------------------|
| altitude     | meters              | `m` (identity), `ft` (× `0.3048`) |
| speed        | m/s                 | `m/s` (identity), `km/h` (× `1000/3600`), `mph` (× `0.44704`) |

Declared aliases: `lat` → `latitude`, `lon`/`lng` → `longitude`, `alt` →
`altitude`.

Worked example — altitude in feet, speed in km/h:

```
timestamp,latitude,longitude,altitude,speed
2026-08-30T18:00:00-07:00,34.0205,-118.2856,100,36
```

```
timestamp,latitude,longitude,altitude,speed
2026-08-31T01:00:00Z,34.0205,-118.2856,30.48,10.0
```

### Field aliasing and Step 2's `allow_extra_fields: false`

Both built-in schemas set `allow_extra_fields: false`. This means a raw
file whose header uses an aliased name for a *required* field (e.g.
`"Accel X"` instead of `accel_x`) can never itself pass Step 2 validation —
Step 2 has no knowledge of Step 4's alias declarations, so it correctly
reports the alias column as `UNEXPECTED_FIELD` and the canonical column as
`MISSING_REQUIRED_FIELD`. This is expected, not a bug: aliasing is a
normalization-layer concept, and the lineage gate means only files that
already validate under their *own* schema ever reach Step 4. Aliasing is
therefore most meaningfully exercised directly against `RecordNormalizer`
(see `tests/test_normalization_imu.py` /
`tests/test_normalization_gps.py`) and remains fully available for schemas
that declare `allow_extra_fields: true` or for optional fields.

### Configuration hash and transform version

Every normalization run's manifest includes a deterministic
`normalization_config_hash` — a SHA-256 of the effective configuration
(profile name/version, alias mappings, unit dimensions and their
conversion factors, the timestamp policy, and the request's `source_units`),
serialized via `json.dumps(..., sort_keys=True, separators=(",", ":"))` —
**never** Python's `repr()`, so the hash depends only on logical content,
not incidental dict-ordering. `transform_version` (`"1.0.0"` for both
built-in profiles) is a separate, explicit field the profile defines
directly — it is *not* derived from a git commit — so normalization logic
can be versioned independently of both the schema and the source code
revision. Together with `raw_sha256`, `schema`, and `profile`, this is
exactly the information a future caching/dedup layer would need to decide
whether two requests would produce the same output — deliberately not
implemented yet (see MVP limitations).

### Error behavior for record-level failures

If a record that already passed validation and integrity checks cannot be
*deterministically* normalized under the given profile/config (e.g. a
non-finite value slipping through, or an ambiguous alias discovered
mid-file), the entire run fails — the record is never silently skipped, and
no partial artifact is committed (see "Atomic commit"). This is the
opposite policy from Steps 2-3, which accumulate every issue into a report
and still return `200`; Step 4 has no notion of a partially-normalized
dataset.

### Logging

`NORMALIZATION_STARTED` / `NORMALIZATION_COMPLETED` / `NORMALIZATION_FAILED`,
with `normalization_id`, `ingestion_id`, `validation_id`, `integrity_id`,
`schema_name`, `schema_version`, `profile_name`, `profile_version`, and (on
completion) `records_written` + `status` — never source or normalized
record contents.

---

## Step 5 — Multimodal Synchronization Engine

Combines two or more independently normalized streams (IMU, GPS, and — by
architecture, not yet by built-in schema — joint states, force/torque,
camera frame timestamps) into one temporally aligned artifact. It answers
exactly one question:

> "Which observations from different sensors correspond to the same point in time?"

```
normalized stream A ─┐
normalized stream B ─┼──→ SYNCHRONIZATION ENGINE ──→ synchronized artifact
normalized stream C ─┘
```

### Why synchronization happens after normalization, never on raw data

Step 5 consumes **normalization runs**, never ingestion IDs — a
synchronization request identifies each stream by an explicit
`normalization_id`, never an implicit "latest run for this ingestion."
This matters for two reasons: (1) Step 4 already guarantees a single
canonical timestamp representation (UTC, ISO-8601, `Z` suffix) and
canonical units — Step 5 can compare timestamps numerically without
re-parsing arbitrary timezones or re-deriving units, and (2) synchronizing
raw data would mean re-solving unit conversion and timestamp normalization
inside the synchronization engine too, duplicating Step 4's job. Step 5
contains **no unit-conversion logic of any kind** — it trusts Step 4's
output completely and only ever reasons about time.

Step 5 is **not** cleaning. It never removes noisy samples, smooths
signals, infers missing observations, repairs corrupt records, deletes
sessions, performs privacy filtering, feature engineering, label
generation, or dataset splitting. A missing match becomes a `null` value
in the output row — **never** a dropped row. That's Step 6/8's decision to
make, not Step 5's.

### Lineage gate

For every `normalization_id` in the request, `SynchronizationService`:

1. locates the normalization manifest (`NormalizedArtifactStore.find_manifest`),
2. **recomputes** the normalized artifact's SHA-256 from the file on disk
   and verifies it matches `normalization_manifest.normalized_sha256` — if
   it doesn't, the artifact has been modified since normalization, and the
   request is rejected (`409`, `NORMALIZED_ARTIFACT_CHECKSUM_MISMATCH`);
   never synchronize a stale or tampered artifact,
3. retrieves `ingestion_id`, `validation_id`, `integrity_id`,
   `source_raw_sha256`, `schema`, `normalization_profile`,
   `normalization_config_hash`, and `transform_version` from the manifest,
4. resolves the owning ingestion's `session_id` via `RawStorage.find_manifest`
   (the raw ingestion manifest — read-only, the same lookup Steps 2-4 use).

### Session compatibility

If every participating stream's ingestion manifest carries a `session_id`
(Step 1 always generates one), all streams must belong to the **same**
session — a mismatch is rejected with `409 SESSION_MISMATCH` by default.
There is no override in this MVP; the architecture supports adding one
later (an explicit `allow_cross_session: true` request field) without
redesigning the lineage check itself.

### Reference timeline: two modes

**Mode A — `{"mode": "stream", "stream": "imu"}`** (the MVP default). Every
timestamp from the named reference stream becomes an output timestamp,
consumed directly and fully streamed — no pre-pass, no buffering beyond
the reference stream's own forward iteration.

**Mode B — `{"mode": "fixed_rate", "frequency_hz": 10.0}`**. Generates a
synthetic, uniform timeline. The usable interval is the **intersection**
of every participating stream's own (corrected) time range — e.g. stream A
spanning 0s→10s and stream B spanning 2s→8s synchronize over 2s→8s only;
Step 5 never extrapolates outside any stream's observed bounds. Because
the interval must be known before any target can be generated, this mode
(unlike Mode A) reads each stream once up front just to find its range,
then a second time to actually align — a deliberate, documented tradeoff
(see MVP limitations). `frequency_hz` must be positive and at most
`MAX_SYNC_FREQUENCY_HZ` (default `1000`), which exists specifically to
prevent an accidentally enormous generated timeline.

### Alignment strategies

Two are implemented, both behind an `AlignmentStrategy` interface so a
third can be added without touching the engine:

**`nearest`** — chooses whichever bracketing sample (previous or next) has
the smallest absolute time difference. **Ties are broken in favor of the
earlier observation**, deterministically. A match farther than
`max_time_delta_ms` is rejected (no match, not an approximate one) —
recorded as `{"matched": false, "reason": "OUTSIDE_TOLERANCE"}`, never
extrapolated. Valid for **any** stream, including a future discrete stream
(e.g. camera frame references) that has no meaningful notion of
"in-between."

**`linear`** — interpolates strictly between two bracketing samples
(`prev.timestamp <= target <= next.timestamp`); if either side is missing,
the target is outside the stream's observed range and the match fails
(`"reason": "NO_EXTRAPOLATION"`) rather than guessing. An exact timestamp
match on either bracket short-circuits interpolation entirely (`delta_ms:
0.0`) — the real observation is used verbatim. Field-level policy:
**numeric fields (float/integer) interpolate linearly; every other field
(strings, booleans, `device_id`, categorical metadata) uses the nearer of
the two bracketing samples, itself still subject to `max_time_delta_ms`** —
never invented, never averaged. `linear` is rejected for a **discrete**
stream (`SchemaDefinition.record_type != "tabular"` — reusing a field
Step 2 already defines, not a new one) with `400
UNSUPPORTED_ALIGNMENT_METHOD`, so a future camera schema can declare
`"record_type": "discrete"` and get this guard for free, with zero changes
to the synchronization core.

Both strategies share one `StreamCursor` per stream — a forward-only
two-pointer walk (`prev`/`pending`) advanced once per target timestamp.
Because every timeline (reference-stream or fixed-rate) is monotonic
non-decreasing, this never needs to look backward: O(1) buffered samples
per stream regardless of file size.

Per-stream overrides are supported and resolved deterministically:

```json
{
  "alignment": {
    "default_method": "nearest",
    "max_time_delta_ms": 100,
    "streams": { "imu": { "method": "linear" }, "gps": { "method": "nearest" } }
  }
}
```

### Clock correction

Explicit and deterministic — **no offset or drift is ever estimated
automatically**. An affine transform, documented and unit-tested against
known expected values:

```
corrected_time = anchor_time + (original_time - anchor_time) * scale + offset
scale = 1 + drift_ppm / 1_000_000
```

The anchor is always the stream's own **first (uncorrected) timestamp** —
never wall-clock execution time — so the same input always corrects the
same way. `offset_ms` may be positive or negative (a sensor consistently
25ms late is corrected with `offset_ms: -25`). `drift_ppm` models a clock
whose rate differs by `drift_ppm / 1,000,000` from real time; a
`drift_ppm` extreme enough to make `scale <= 0` (i.e. reverse time order)
is rejected outright (`400 CLOCK_CORRECTION_ERROR`). Correction is applied
**before** alignment and exists only inside the synchronization run — it
never touches the normalized source artifact.

### Tolerance and missing-match behavior

`max_time_delta_ms` (falling back to `DEFAULT_SYNC_TOLERANCE_MS`, default
`100`) bounds `nearest` matches and non-numeric `linear` fields. When no
acceptable sample exists, the affected stream's value in that row is
`null` and its `alignment` entry records why (`OUTSIDE_TOLERANCE` /
`NO_EXTRAPOLATION` / `NO_DATA`) — **the row itself is always kept**. Step 5
never decides a row (or a whole session) is unusable; it only reports.

### Synchronization metrics

Per stream, accumulated in O(1) memory (a running sum/count/max, never a
stored list of every delta): `source_records` (from the normalization
manifest's own `records_written` — no re-count needed), `matched_rows`,
`unmatched_rows`, `coverage_ratio` (`matched_rows / output_rows`),
`mean_abs_delta_ms`, `max_abs_delta_ms`, and — only for a stream that used
`linear` — `exact_match_count` / `interpolated_count` (both `null`
otherwise). Low coverage is **reported**, never treated as a reason to
fail the run.

### Output format

One canonical synchronized JSONL artifact:

```json
{
  "timestamp": "2026-08-31T01:00:00Z",
  "streams": {
    "imu": { "accel_x": 0.1, "accel_y": 0.2, "accel_z": 9.79, "gyro_x": 0.01, "gyro_y": 0.02, "gyro_z": 0.0, "device_id": null },
    "gps": { "latitude": 34.0205, "longitude": -118.2856, "altitude": 30.48, "speed": 10.0, "device_id": null }
  },
  "alignment": {
    "imu": { "matched": true, "method": "reference", "delta_ms": 0.0 },
    "gps": { "matched": true, "method": "nearest", "delta_ms": 24.7 }
  }
}
```

The top-level `timestamp` is the synchronized target timeline; it is never
duplicated inside each stream's own payload.

### Architecture

```
app/synchronization/
    models.py                SynchronizationManifest / API request-response models
                              (SchemaRef reused from app.validation.models)
    service.py                 SynchronizationService — lineage gate, session check,
                                timeline + cursor wiring, streaming write, atomic commit
    registry.py                 AlignmentStrategyRegistry — method name -> strategy,
                                 discrete-stream guard (reuses SchemaDefinition.record_type)
    readers.py                  Canonical timestamp <-> integer-microsecond epoch conversion,
                                 typed record casting, monotonicity enforcement
    timeline.py                  fixed_rate timeline generation + intersection-interval policy
    alignment.py                  Per-target-timestamp row assembly across all streams
    metrics.py                    StreamMetricsAccumulator (O(1) memory)
    strategies/
        base.py                    StreamCursor, AlignmentContext, AlignmentStrategy interface
        nearest.py                 NearestAlignmentStrategy
        linear.py                  LinearInterpolationStrategy
    clocks/
        correction.py               apply_clock_correction / apply_stream_correction

app/storage/synchronization_store.py   SynchronizationArtifactStore / LocalSynchronizationArtifactStore
data/synchronized/                      Persisted synchronized artifacts + manifests
```

Reading a normalized artifact's raw records reuses
`app.normalization.records.iter_records` (itself reusing
`app.integrity.records`) — the fourth stage to lean on that same
CSV/JSON/JSONL iteration rather than reimplementing it. Timestamp parsing
is new and Step-5-specific: `parse_canonical_timestamp_us` converts Step
4's canonical string into **integer microseconds since epoch** (never
floating-point, so ordering/equality/arithmetic stay exact — datetime's
own native resolution, matching the canonical format's own precision
ceiling), and `format_epoch_us` is its exact inverse.

`_find_matching_validation_report` / `_find_matching_integrity_report`-style
filtering isn't needed here (Step 5 trusts Step 4's manifest directly
rather than re-deriving lineage from scratch) — but the same "filesystem
globbing stays inside the store, business logic stays in the service"
separation applies: `NormalizedArtifactStore.find_manifest()` does the one
glob, `SynchronizationService` does everything else.

### On-disk layout

```
data/synchronized/
  <synchronization_id>/
    synchronized.jsonl
    manifest.json
```

Same staging → atomic commit strategy as `NormalizedArtifactStore`:
content is written into a hidden `.tmp-<synchronization_id>/` directory
first, then atomically renamed into its final location only once fully
written (`Path.rename()`, atomic on POSIX when source/destination share a
filesystem — true here). A failure at any point — including a per-record
`SYNCHRONIZATION_CONVERSION_ERROR` — discards the staging directory and
leaves nothing committed; an existing synchronization run can never be
overwritten.

### Determinism and the configuration hash

Given the same normalized artifacts (same checksums), the same
synchronization configuration, and the same transform version, the
synchronized artifact is byte-identical — `synchronization_id` and
`created_at` naturally differ between runs, but `synchronized_sha256`
never does. `synchronization_config_hash` is a SHA-256 over the reference
policy, resolved per-stream alignment methods, tolerance, clock
corrections, tie-breaking policy, and timestamp policy — serialized via
`json.dumps(..., sort_keys=True, separators=(",", ":"))`, never Python's
`repr()`.

### API

```
POST /api/v1/synchronization
Content-Type: application/json

{
  "streams": [
    { "name": "imu", "normalization_id": "norm_..." },
    { "name": "gps", "normalization_id": "norm_..." }
  ],
  "reference": { "mode": "stream", "stream": "imu" },
  "alignment": { "default_method": "nearest", "max_time_delta_ms": 100 }
}
```

| Condition                                                          | Status |
|------------------------------------------------------------------------|--------|
| A referenced normalization run (or its schema) doesn't exist              | 404 |
| A normalized artifact's checksum no longer matches its manifest, streams belong to different sessions, or a stream's timestamps are non-monotonic | 409 |
| A normalized artifact's file type isn't readable (unreachable given Step 4's guarantees; kept as defense-in-depth) | 415 |
| Fewer than 2 streams, duplicate stream names, an unresolvable reference stream, an invalid/excessive fixed-rate frequency, an unsupported/mismatched alignment method, a drift that would reverse time order, or a record that can't be deterministically combined | 400 |
| Synchronization executed (alignment gaps are reported, never raised as errors) | 200 |
| An actual internal failure (e.g. a normalized timestamp that fails to parse — a Step 4 guarantee violation) | 500 |

```bash
curl -X POST http://localhost:8000/api/v1/synchronization \
  -H "Content-Type: application/json" \
  -d '{"streams": [{"name": "imu", "normalization_id": "<NORM_ID_1>"}, {"name": "gps", "normalization_id": "<NORM_ID_2>"}], "reference": {"mode": "stream", "stream": "imu"}, "alignment": {"default_method": "nearest", "max_time_delta_ms": 1200}}'
```

```json
{
  "synchronization_id": "sync_df2ebb4a-e1bf-4d6c-bc65-6a1b056e24ec",
  "status": "completed",
  "reference": { "mode": "stream", "stream": "imu", "frequency_hz": null },
  "streams": [
    { "name": "imu", "normalization_id": "norm_..." },
    { "name": "gps", "normalization_id": "norm_..." }
  ],
  "rows_written": 10,
  "coverage": { "gps": 1.0, "imu": 1.0 },
  "artifact_uri": "file:///.../data/synchronized/<synchronization_id>/synchronized.jsonl",
  "synchronized_sha256": "479bcff942043e4a6f3ef4bc77e84b0e518d0ab22c9b230df1d5c76edac3b812"
}
```

### Known architectural limitation: Step 3 is unit-unaware

Step 3's IMU extreme-value thresholds run against **raw** values, before
Step 4's unit conversion — so a raw `180 deg/s` reading can trigger
`IMU_GYRO_EXTREME` as if it were already `180 rad/s` (far past the 50
rad/s default threshold), even though `180 deg/s ≈ 3.14 rad/s` is
completely unremarkable. This is a pre-existing Step 3 limitation, **not
something Step 5 fixes or redesigns** — Step 5 only ever consumes Step 4's
already-canonical values downstream of both, and contains no
unit-conversion logic itself. It's called out here because it's the kind
of warning you'll likely see in the demo below (integrity `status:
passed_with_warnings` on a perfectly normal IMU file) and should not be
mistaken for a Step 5 bug.

### Logging

`SYNCHRONIZATION_STARTED` / `SYNCHRONIZATION_COMPLETED` /
`SYNCHRONIZATION_FAILED`, with `synchronization_id`, `session_id`, stream
names, `normalization_id`s, reference mode, and (on completion)
`rows_written` + `coverage` + `status` — never sensor values or full
synchronized rows.

---

## Step 6 — Cleaning / Filtering Engine

Takes a synchronized multimodal dataset and applies **explicit,
deterministic, configured** rules to decide which rows survive, which
fields get redacted, and why — producing a new, immutable cleaned artifact
plus a full audit trail. It answers a narrower question than it might
sound like:

> "Given this synchronized row, does policy say to keep it, and does
> anything in it need redacting?"

```
synchronized artifact -> cleaning policy -> row evaluation -> cleaned artifact + cleaning report
```

### Cleaning vs. Integrity vs. Dataset QC — three different judgments

| Stage | Question | Scope |
|---------|------------|---------|
| **Step 3 (Integrity)** | "Is this *value* plausible?" | Per-field, per-record |
| **Step 6 (Cleaning)**    | "Should this *row* be kept, and does it need redacting, per an explicit policy?" | Per-row, rule-driven |
| **Step 8 (Dataset QC, future)** | "Is the *dataset's overall distribution* acceptable?" | Whole-dataset, statistical |

Step 6 never repeats Step 3's plausibility judgment (a value that already
passed integrity checks is trusted), and never makes Step 8's kind of
distributional call (e.g. "is 60% GPS coverage good enough for training?"
is a QC question, not a cleaning one — Step 6 only *reports* the resulting
`coverage_ratio`/`retention_ratio`, exactly as Step 5 only reports
alignment coverage without judging it). Step 6 is also **not**
interpolation, resampling, smoothing, feature generation, labeling, or
dataset splitting — it only decides keep/drop/redact per explicit rule.

### Non-destructive architecture

Step 6 never opens `synchronized.jsonl` (or any upstream artifact/report)
for writing. Every cleaning run is a brand-new, immutable artifact under
its own `cleaning_id`:

```
data/cleaned/<synchronization_id>/<cleaning_id>/
    cleaned.jsonl
    report.json
    manifest.json
```

One synchronized artifact can be cleaned repeatedly — different policies,
different configs — without ever overwriting an earlier cleaning run or
touching the synchronized artifact it was cleaned from.

### Lineage gate

Before evaluating a single row, `CleaningService`:

1. locates the synchronization manifest (`SynchronizationArtifactStore.find_manifest`
   — a direct lookup by `synchronization_id`, unlike Step 5's glob search for
   a bare `normalization_id`, since the URL path always supplies it explicitly),
2. verifies the manifest's own `synchronization_id` matches the request
   (defensive — unreachable via the direct lookup, but checked anyway),
3. **recomputes** the synchronized artifact's SHA-256 from the file on disk
   and verifies it matches `manifest.synchronized_sha256` — a mismatch means
   the artifact was modified since synchronization, and the request is
   rejected (`409 SYNCHRONIZED_ARTIFACT_CHECKSUM_MISMATCH`); never clean a
   stale or tampered artifact,
4. retrieves every upstream lineage field already embedded in the
   synchronization manifest (`normalization_id`, `ingestion_id`,
   `session_id` per stream, `synchronization_config_hash`) — Step 6 never
   re-derives lineage from scratch or reaches into any earlier stage's
   storage; it trusts Step 5's manifest completely.

### Policy model

`CleaningPolicy` + `CleaningPolicyRegistry`, looked up explicitly by
`(policy_name, policy_version)` — never an implicit "latest," mirroring
every other registry in this project. A policy decides **which rules run
and in what order**; the request's `config` only ever parameterizes rules
the policy already knows how to build — there is no dynamic code
execution, no `eval()`, no arbitrary expression language. Customer-specific
behavior (e.g. a future `WarehouseRobotCleaningPolicy`) is added by
subclassing `CleaningPolicy` and overriding `build_rules()` — `CleaningService`
never branches on customer identity itself.

The one built-in policy, `default_multimodal` v`1.0.0`, builds rules in
this **fixed, documented order**:

```
1. required streams          (MISSING_REQUIRED_STREAM)
2. minimum present streams   (INSUFFICIENT_MODALITY_COVERAGE)
3. all-optional-missing      (ALL_OPTIONAL_STREAMS_MISSING)
4. duplicate detection       (DUPLICATE_ROW)
5. privacy redaction         (FIELD_REDACTED)
```

Order is significant, not incidental: the first rule that decides to drop
a row short-circuits every rule after it (a dropped row is never
additionally redacted), and **privacy redaction runs last on purpose** —
redacting a distinguishing field *before* duplicate detection could make
two genuinely different rows collide and be misreported as duplicates
(tested explicitly in `tests/test_cleaning_privacy.py`).

### Required streams

```json
{ "required_streams": ["imu"] }
```

If a synchronized row has `"streams": {"imu": null, ...}`, the row is
dropped with `MISSING_REQUIRED_STREAM` (including the stream name). A
stream **not** listed as required — e.g. GPS — being null never triggers
this rule; that's exactly what "optional" means here.

### Modality coverage filtering

```json
{ "min_present_streams": 2 }
```

Counts how many of the *known* streams (every stream declared in the
synchronization manifest, not just the required ones) have a non-null
payload in this row; fewer than the threshold drops the row with
`INSUFFICIENT_MODALITY_COVERAGE`. This is a configured hard filter, not a
statistical judgment about the dataset — that distinction matters (see the
Cleaning vs. Dataset QC table above).

An optional `drop_if_all_optional_streams_missing` (default `false`) drops
a row only when every *required* stream is present but every *optional*
one is null — never inferred automatically, only applied when explicitly
configured `true`.

"Presence" is always a simple non-null check — Step 5 already sets a
stream to `null` whenever nothing matched at that timestamp, so Step 6
never re-derives what "missing" means.

### Duplicate handling

Two rows are duplicates only if their canonical `(timestamp, streams)`
content is identical — **timestamp is part of the identity**: two rows
with identical sensor values but different timestamps are never
duplicates. The `alignment` diagnostic block is deliberately excluded from
the identity hash (two rows describing the same observation are still
duplicates regardless of *how* they were aligned). The first occurrence is
kept; every later exact duplicate is dropped with `DUPLICATE_ROW` and
`duplicate_of_row_index` pointing back to it.

Hashing uses `SHA256(canonical_json({"timestamp": ..., "streams": ...}))`
— **never** Python's built-in `hash()`, which is randomized per-process
and would make cleaned output non-reproducible. State is a
`{content_hash: first_row_index}` dict, so memory is **O(number of unique
rows)**, not O(total rows) — see MVP limitations for what this means at
scale. Only exact duplicates are detected; there is no fuzzy/near-duplicate
detection in this MVP.

**In practice, given Step 5's own guarantee of unique, monotonic reference
timestamps, a real synchronized artifact should never actually trigger
`DUPLICATE_ROW`** — this rule exists for defensive/general-purpose
correctness (a future multi-session merge, or a hand-modified input) and
is fully exercised in tests, but the end-to-end demo below has to
construct an artificial duplicate to show it firing (documented there).

### Privacy redaction

```json
{ "privacy": { "redact_fields": ["streams.gps.latitude", "streams.gps.longitude"] } }
```

Explicit dot-separated paths only — **nothing is inferred, guessed, or
detected via NLP/AI PII heuristics**. A redacted field is set to `null`
(not deleted — the schema key stays present, so downstream consumers see a
stable row shape) and recorded as `FIELD_REDACTED` in the report. A path
that's structurally invalid (empty, or with an empty segment like
`"streams..latitude"`) is rejected up front as
`400 INVALID_REDACTION_PATH`; a *valid* path simply absent from a
*particular* row (e.g. GPS didn't match at that timestamp) is silently
skipped for that row — not every row needs to fail because an optional
field happens to be absent.

### Completed vs. rejected

Every processed run is committed — "rejected" is a policy-level judgment
on an otherwise-successful run, not a processing failure:

- **`completed`** — the run's own configured thresholds weren't violated.
- **`rejected`** — either the synchronized artifact had zero rows
  (`EMPTY_SYNCHRONIZED_DATASET`), or fewer rows survived than a configured
  `minimum_retained_rows` (`INSUFFICIENT_RETAINED_ROWS`). The cleaned
  artifact, report, and manifest are still written and committed — this
  preserves auditability; a rejected run is not "as if it never happened."

Both statuses return `HTTP 200` — a policy rejecting a dataset is not a
server error, exactly as a `"failed"` Step 2/3 result isn't one either.

### Streaming / memory behavior

`synchronized.jsonl` is read one line at a time and `cleaned.jsonl` is
written incrementally as rows are retained — the full dataset is never
held in memory. The only state carried across rows is small and bounded
by design: running counters, the reason-count dict, the capped
dropped/redaction example lists, and the duplicate-detection hash set
(the one piece of state that grows with *unique* row content — see
"Duplicate handling" above). No full row content is retained once written.

### Report size limits

`MAX_CLEANING_ISSUE_DETAILS` (default `1000`) caps `dropped_examples` and
`redaction_examples` **independently** — `reason_counts` keeps counting
every occurrence regardless of the cap; only the detailed example objects
stop accumulating once a list's cap is hit, and `details_truncated: true`
is set. Detailed reports focus on removed/redacted rows only — retained,
un-redacted rows are never individually recorded (that would make
`report.json` proportional to dataset size for the common case).

### Canonical JSON serialization

Every output row — and the duplicate-detection hash's input — goes through
the same one convention:

```python
json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
```

so dict-insertion-order differences (e.g. between two logically-identical
rows built via different code paths) can never change `cleaned.jsonl`'s
bytes, and therefore never change `cleaned_sha256`.

### Configuration hash and determinism

`cleaning_config_hash` is a SHA-256 over policy name/version, transform
version, and the full effective `CleaningConfig` (required streams,
minimum present streams, the all-optional-missing flag, duplicate-policy
settings, privacy field paths, and `minimum_retained_rows`) — serialized
via `sort_keys=True`, never Python's `repr()`. Given the same synchronized
bytes, the same checksum, and the same effective configuration,
`cleaned.jsonl` is **byte-identical** across runs; only `cleaning_id` and
`created_at` differ.

### Lineage chain

```
raw -> validation -> integrity -> normalization -> synchronization -> cleaning
```

The cleaning manifest embeds, per stream, `normalization_id`,
`ingestion_id`, and `session_id` (a trimmed copy — not the entire upstream
manifest wholesale) alongside `source_synchronized_sha256` and
`synchronization_config_hash`, so the exact upstream synchronization run
that produced any cleaned artifact is always unambiguous.

### Architecture

```
app/cleaning/
    models.py                CleaningManifest / CleaningReport / API request-response models
    service.py                 CleaningService — lineage gate, config validation, streaming
                                row-by-row evaluation, atomic commit
    registry.py                 CleaningPolicyRegistry — (policy_name, policy_version) -> CleaningPolicy
    evaluator.py                 RowEvaluator — applies one policy's ordered rules to one row
    metrics.py                   CleaningMetricsAccumulator (O(1) memory; capped detail lists)
    policies/
        base.py                   CleaningPolicy contract + config_hash()
        default.py                 DefaultMultimodalPolicy — the fixed 5-rule order
    rules/
        base.py                    CleaningRule interface, DropReason, RedactionRecord, RuleOutcome
        common.py                   canonical_json, dot-path helpers (path_exists, apply_redactions)
        coverage.py                  RequiredStreamsRule, MinPresentStreamsRule, AllOptionalMissingRule
        duplicates.py                 DuplicateRowRule (canonical_row_key — timestamp+streams only)
        privacy.py                    PrivacyRedactionRule

app/storage/cleaned_store.py   CleanedArtifactStore / LocalCleanedArtifactStore
data/cleaned/                   Persisted cleaned artifacts + reports + manifests
```

A `CleaningRule` is a plain Python class with one `evaluate()` method
returning a `RuleOutcome` (drop reasons and/or redactions) — deliberately
not a generic rule language; adding a new rule means writing a new small
class, not extending an expression grammar.

### API

```
POST /api/v1/cleaning/{synchronization_id}
Content-Type: application/json

{
  "policy_name": "default_multimodal",
  "policy_version": "1.0.0",
  "config": {
    "required_streams": ["imu"],
    "min_present_streams": 1,
    "drop_if_all_optional_streams_missing": false,
    "duplicate_policy": { "enabled": true },
    "privacy": { "redact_fields": ["streams.gps.latitude", "streams.gps.longitude"] }
  }
}
```

| Condition                                                          | Status |
|------------------------------------------------------------------------|--------|
| The synchronization run, or the requested cleaning policy, doesn't exist | 404 |
| The synchronized artifact's checksum no longer matches its manifest        | 409 |
| The synchronized artifact's format isn't readable (JSONL only for this MVP) | 415 |
| A negative `min_present_streams`/`minimum_retained_rows`, or a structurally invalid redaction path | 400 |
| Cleaning executed — whether the result is `completed` or policy-`rejected` | 200 |
| An actual internal failure                                                 | 500 |

```bash
curl -X POST http://localhost:8000/api/v1/cleaning/<SYNCHRONIZATION_ID> \
  -H "Content-Type: application/json" \
  -d '{"policy_name": "default_multimodal", "policy_version": "1.0.0", "config": {"required_streams": ["imu"], "min_present_streams": 1, "duplicate_policy": {"enabled": true}, "privacy": {"redact_fields": ["streams.gps.latitude", "streams.gps.longitude"]}}}'
```

```json
{
  "cleaning_id": "clean_4b018684-89c9-46ff-895c-604ff8ae8fb5",
  "synchronization_id": "sync_09b089de-82dc-47f1-bb54-f039083490a0",
  "status": "completed",
  "policy": { "name": "default_multimodal", "version": "1.0.0" },
  "summary": {
    "input_rows": 9,
    "retained_rows": 7,
    "dropped_rows": 2,
    "redacted_rows": 4,
    "retention_ratio": 0.7777777777777778
  },
  "artifact_uri": "file:///.../data/cleaned/<synchronization_id>/<cleaning_id>/cleaned.jsonl",
  "report_uri": "file:///.../data/cleaned/<synchronization_id>/<cleaning_id>/report.json",
  "cleaned_sha256": "6376c0d0c9ed1f4c6f7a11b05e707082f23e2e37bf2c85617cb52bdb50749e10",
  "rejection_reasons": []
}
```

### Logging

`CLEANING_STARTED` / `CLEANING_COMPLETED` / `CLEANING_REJECTED` /
`CLEANING_FAILED`, with `cleaning_id`, `synchronization_id`, `policy_name`,
`policy_version`, and (on completion) `input_rows` / `retained_rows` /
`dropped_rows` / `redacted_rows` / `status` — never full synchronized rows,
GPS coordinates, or any other sensor values.

---

## Step 7 — Transformation / Feature Generation Engine

Takes a *cleaned* multimodal dataset (Step 6's `cleaned.jsonl`) and turns
it into ML-oriented samples: deterministic segmentation into windows, plus
deterministic handcrafted features per window. It is the first stage
permitted to derive **new** values from the data — everything before it,
including Step 6, only ever selects/redacts/reports on values that already
exist.

```
cleaned artifact -> profile-driven windowing + feature generation -> transformed artifact + report
```

### Transformation vs. Cleaning vs. Dataset QC — three different questions

| Stage | Question | Scope |
|---------|------------|---------|
| **Step 6 (Cleaning)** | "Should this *row* be kept, and does it need redacting?" | Per-row, rule-driven |
| **Step 7 (Transformation)** | "How do we group these rows into ML samples, and what deterministic features describe each one?" | Per-window, feature-driven |
| **Step 8 (Dataset QC, future)** | "Is the *dataset's overall distribution* acceptable?" | Whole-dataset, statistical |

Step 7 never repeats Step 6's keep/drop judgment (every row it sees is
already "kept"), and never makes Step 8's kind of distributional call — it
computes deterministic features and reports coverage; it never decides
train/val/test splits, never rejects a dataset for imbalance, and never
infers a label.

### Step 7 architecture

```
app/transformation/
    models.py        Pydantic request/response/manifest/report models
    service.py        TransformationService — lineage gate + orchestration
    registry.py        TransformationProfileRegistry — explicit (name, version) lookup
    windowing.py        Streaming count/time windowing generators
    feature_engine.py    Per-window orchestration: extractors, modality mask/coverage
    metrics.py        Bounded report-level accumulation
    serialization.py    canonical_json (allow_nan=False) + deterministic sample_id
    profiles/
        base.py            TransformationProfile contract
        multimodal_window.py  Built-in multimodal_window_v1 profile
    features/
        base.py        FeatureExtractor contract, WindowRow/StreamFeatureResult
        statistics.py    mean/std/min/max/median/first/last/delta (population std, ddof=0)
        common.py        require_finite() — NaN/Infinity fail loudly, never silently propagate
        imu.py        Raw sequences, per-axis statistics, accel/gyro magnitude
        gps.py        Raw sequences, per-field statistics, Haversine displacement
app/storage/transformed_store.py   Staging + atomic commit, mirrors every other artifact store
app/api/routes/transformation.py   Thin HTTP layer — request/response + error mapping only
```

No arbitrary transformation logic lives in the API route: everything
config-shaped is mediated through a `TransformationProfile`
(`multimodal_window_v1` v`1.0.0` is the only one shipped in this MVP).

### Count vs. time windowing

Two windowing modes, both streaming (bounded memory, never O(dataset size)):

- **`count`** — `{"mode": "count", "size": 20, "stride": 10, "drop_incomplete": true}`.
  A classic overlapping sliding window over row *count*: window `k` covers
  rows `[k*stride, k*stride + size)`. Buffered via a `collections.deque` of
  at most `size` rows — memory is `O(size)`, never `O(dataset size)`.
- **`time`** — `{"mode": "time", "duration_ms": 1000, "stride_ms": 500}`.
  Windows are defined over the *canonical timestamps already present* in
  the cleaned rows (no resynchronization, no interpolation — Step 5 already
  owns that). Window `k` covers the half-open interval
  `[first_ts + k*stride_ms, first_ts + k*stride_ms + duration_ms)`. A row
  can belong to multiple overlapping windows if `duration_ms > stride_ms`.
  Memory is bounded by how many rows fall within one window's time span,
  not by a fixed row count and not by dataset size.

Both modes require monotonically non-decreasing input order (guaranteed by
every upstream stage) and raise a clear error rather than silently
reordering or producing wrong windows otherwise.

### Incomplete-window behavior

- **count mode**: `drop_incomplete=true` (default) omits a final partial
  window that never reached `size` rows. `drop_incomplete=false` emits it,
  however small.
- **time mode**: a window closed *during* streaming (because a later row's
  timestamp reached or passed its nominal end) is always complete and
  always emitted. A window still open when the input stream ends is, by
  construction, "incomplete" — its nominal end was never actually reached
  by data. `drop_incomplete` controls only these trailing, naturally-
  bounded windows.

### Statistical features

`mean`, `std`, `min`, `max`, `median`, `first`, `last`, `delta` — all
implemented. **Standard deviation is population standard deviation
(`ddof=0`), never sample std** — a fixed, documented choice
(`app/transformation/features/statistics.py`). An unrecognized statistic
name fails configuration validation (`400 UNKNOWN_FEATURE`) before any row
is processed — it is never silently ignored.

"Start position"/"end position" for GPS are not a separate feature:
requesting `statistics=["first","last"]` already yields
`latitude_first`/`latitude_last`/`longitude_first`/`longitude_last` via the
same generic per-field mechanism used for every other field.

### IMU magnitude features

`accel_magnitude = sqrt(accel_x^2+accel_y^2+accel_z^2)` and
`gyro_magnitude = sqrt(gyro_x^2+gyro_y^2+gyro_z^2)`, computed **per row**
(a row missing any one of the three axes contributes no magnitude sample
for that row, rather than a value computed from partial data), then
treated exactly like any other axis for raw-sequence inclusion and
statistics. Deliberately MVP-scoped: no gravity subtraction, no
orientation estimation, no FFT/spectral features.

### Missing modality handling

A window is **never dropped** because a modality is absent from it. GPS
statistics for a window with zero GPS rows retain their full key
structure with `null` values (`{"speed_mean": null, ...}`), not an omitted
`gps` block. Values are never invented — a `null` statistic means "no
data," not "zero."

### Modality masks and coverage

`include_modality_mask=true` adds a window-level
`{"imu": true, "gps": false}` (`true` = at least one non-null observation
in the window) to each sample. `modality_coverage` (e.g.
`{"imu": 1.0, "gps": 0.2}`) is always included, per-window, and is purely
informational — it never rejects a window (that would be a Step 8
judgment).

### Deterministic sample IDs

`sample_id` is **not** a random UUID — it's
`sha256(cleaned_sha256 : transformation_config_hash : window_index : start_epoch_us : end_epoch_us)`,
shortened to 32 hex characters and prefixed `sample_`. The same cleaned
bytes, effective config, and window position always produce the same
`sample_id`, proven by `tests/test_transformation_determinism.py`.

### Config hash

`transformation_config_hash` is a SHA-256 over the profile identity,
transform version, and the full effective config (window
mode/size/stride/duration/`drop_incomplete`, feature selection, raw
inclusion, derived features, statistics, modality mask, relative time),
serialized via `canonical_json` (`sort_keys=True`, compact separators,
**`allow_nan=False`**) — never Python's `repr()`.

`allow_nan=False` is a deliberate divergence from Step 6's own
`canonical_json` (which lacks it): Step 6 only ever passes pre-validated
values through unchanged, but Step 7 performs real numeric computation
(statistics, derived magnitudes) that could, in principle, yield a
non-finite result. `allow_nan=False` makes that fail loudly — a
`ValueError` from `json.dumps`, caught and surfaced as
`400 INVALID_NUMERIC_VALUE` — instead of silently emitting invalid JSON.

### Transformed artifact format

One JSONL line per window in `transformed.jsonl`:

```json
{
  "sample_id": "sample_6c3bb8bd9b75ed644d92701b64ad043f",
  "window": {"index": 1, "start_timestamp": "2026-08-30T18:00:05Z", "end_timestamp": "2026-08-30T18:00:14Z", "row_count": 10},
  "features": {
    "imu": {
      "raw": {"accel_x": [...], "accel_magnitude": [...]},
      "statistics": {"accel_x_mean": 0.41, "accel_x_std": 0.31, "accel_magnitude_mean": 9.82}
    },
    "gps": {"statistics": {"speed_mean": 9.8}}
  },
  "modality_mask": {"imu": true, "gps": true},
  "modality_coverage": {"imu": 1.0, "gps": 0.3},
  "metadata": {"source_row_start": 5, "source_row_end": 14, "relative_time_ms": [0.0, 1000.0, ...]}
}
```

Nested JSON is kept as-is — Step 7 deliberately does **not** flatten into
flat CSV-style columns; that's Dataset Packaging's job later (Step 9), for
CSV/Parquet/NumPy/PyTorch/TF/HF formats.

### Streaming / memory behavior

Count-mode windowing buffers at most `size` rows via a
`collections.deque`; time-mode windowing buffers at most the rows falling
within one window's time span. Neither mode materializes the whole cleaned
dataset in memory, and both hash + write `transformed.jsonl` in a single
streaming pass (`ChunkedSha256`), exactly like every prior stage.

### Lineage (gate before transforming)

1. Locate the cleaning manifest by `cleaning_id` alone (a new, additive
   `CleanedArtifactStore.find_manifest_by_cleaning_id()` glob lookup — the
   existing `find_manifest()` needs both `synchronization_id` and
   `cleaning_id`, but Step 7's route only has the latter).
2. Locate the cleaned artifact and recompute its SHA-256.
3. Compare against `manifest["cleaned_sha256"]` — mismatch is `409
   CLEANED_ARTIFACT_CHECKSUM_MISMATCH` (tampered/stale).
4. Require `cleaning status == "completed"` — a `"rejected"` cleaning run
   is `409 CLEANING_NOT_ACCEPTED` by default.

The transformation manifest preserves upstream references
(`cleaning_id`, `synchronization_id`, cleaning policy name/version,
cleaning config hash, session IDs, normalization IDs) without copying the
entire upstream manifest wholesale.

### Transformation endpoint

`POST /api/v1/transformation/{cleaning_id}` — operates on an explicit
`cleaning_id`, never an implicit "latest".

| Condition | Status |
|---------|--------|
| The cleaning run, or the requested transformation profile, doesn't exist | 404 |
| The cleaning run is not `"completed"` (e.g. `"rejected"`) | 409 |
| The cleaned artifact's checksum no longer matches its manifest | 409 |
| The cleaned artifact's format isn't readable (JSONL only for this MVP) | 415 |
| Invalid window mode/size/stride/duration, unknown feature/statistic name, a non-finite computed value, or an unparseable timestamp | 400 |
| Transformation executed — including the zero-sample case for an empty cleaned dataset | 200 |
| An actual internal failure | 500 |

```bash
curl -X POST http://localhost:8000/api/v1/transformation/<CLEANING_ID> \
  -H "Content-Type: application/json" \
  -d '{"profile_name": "multimodal_window_v1", "profile_version": "1.0.0", "config": {"window": {"mode": "count", "size": 10, "stride": 5, "drop_incomplete": true}, "features": {"imu": {"include_raw": true, "statistics": ["mean", "std", "min", "max"], "derived": ["accel_magnitude"]}, "gps": {"statistics": ["mean"]}, "include_modality_mask": true, "include_relative_time": true}}}'
```

```json
{
  "transformation_id": "xform_2cb48e40-8c1f-4c74-9ae1-0859fc5bc5eb",
  "cleaning_id": "clean_be35d28e-5af9-4b85-b98c-132b503a12fd",
  "status": "completed",
  "profile": { "name": "multimodal_window_v1", "version": "1.0.0" },
  "summary": {
    "input_rows": 30,
    "samples_written": 5,
    "window_mode": "count",
    "average_rows_per_window": 10.0
  },
  "artifact_uri": "file:///.../data/transformed/<cleaning_id>/<transformation_id>/transformed.jsonl",
  "report_uri": "file:///.../data/transformed/<cleaning_id>/<transformation_id>/report.json",
  "transformed_sha256": "f5f3c46198b5543a004d6ed9690ba6afb4f1be64208f3a18fad5c29436c6d959"
}
```

### Logging

`TRANSFORMATION_STARTED` / `TRANSFORMATION_COMPLETED` /
`TRANSFORMATION_FAILED`, with `transformation_id`, `cleaning_id`,
`profile_name`, `profile_version`, `window_mode`, and (on completion)
`input_rows` / `samples_written` / `status` — never sensor values or full
feature vectors.

---

## Step 8 — Dataset Quality Control Engine

Takes a *transformed* dataset (Step 7's `transformed.jsonl`) and asks a
question no earlier stage asks: **"is this dataset, as a whole, healthy
enough for ML use?"** It analyzes the dataset in aggregate — sample
counts, modality coverage, feature completeness, distributions, variance,
temporal coverage — and produces a report and a status. It never mutates,
filters, imputes, resynchronizes, or regenerates anything; QC *reports*
problems, it does not repair them.

```
transformed artifact -> dataset-level metric collection -> profile-driven check evaluation -> qc report + manifest
```

### Dataset QC vs. Integrity vs. Cleaning — three different judgments

| Stage | Question | Scope |
|---------|------------|---------|
| **Step 3 (Integrity)** | "Is this *value* plausible?" (e.g. `latitude = 300` is impossible) | Per-field, per-record |
| **Step 6 (Cleaning)** | "Should this *row* be kept, per an explicit policy?" | Per-row, rule-driven |
| **Step 7 (Transformation)** | "Turn N rows into one feature window." | Per-window, feature-driven |
| **Step 8 (Dataset QC)** | "Is 31% of samples missing GPS acceptable? Is `gyro_z_std` constant across the whole dataset?" | Whole-dataset, statistical |

Step 8 never repeats Step 3's per-value plausibility check, Step 6's
per-row keep/drop judgment, or Step 7's per-window feature computation —
it only reports on the transformed dataset that already exists.

### Step 8 architecture

```
app/qc/
    models.py        Pydantic request/response/manifest/report models
    service.py        QCService — lineage gate + orchestration
    registry.py        QCProfileRegistry — explicit (name, version) lookup
    accumulator.py        WelfordAccumulator (streaming mean/variance/min/max), PercentileBuffer
    metrics.py        DatasetMetricsCollector — single-pass metric collection
    selectors.py        Deterministic scalar-feature discovery
    profiles/
        base.py            QCProfile contract
        default.py            Built-in default_dataset_qc profile
    checks/
        base.py            QCCheck contract: (DatasetMetrics, QCConfig) -> list[QCIssue]
        dataset_size.py        DATASET_TOO_SMALL / EMPTY_DATASET
        modality_coverage.py    LOW_MODALITY_COVERAGE
        feature_completeness.py    LOW_FEATURE_COMPLETENESS
        variance.py        LOW_FEATURE_VARIANCE
        distributions.py    NON_FINITE_FEATURE_VALUE / FEATURE_RANGE_VIOLATION
        identifiers.py        DUPLICATE_SAMPLE_ID
        temporal.py        NON_MONOTONIC_SAMPLE_TIME
        group_distribution.py    GROUP_IMBALANCE (session/group metadata only)
        drift.py        Baseline drift comparison (FEATURE_DISTRIBUTION_DRIFT)
app/storage/qc_store.py    Staging + atomic commit, mirrors every other artifact store
app/api/routes/qc.py    Thin HTTP layer — request/response + error mapping only
```

Metric collection is deliberately decoupled from check evaluation:
`DatasetMetricsCollector` only observes and aggregates a stream of
transformed samples into a `DatasetMetrics` object; every `QCCheck` is a
pure function of `(DatasetMetrics, QCConfig) -> list[QCIssue]` that never
touches storage or the raw sample stream. `group_distribution.py` and
`drift.py` are the two exceptions — they need lineage/storage access
(session IDs from the transformation manifest; a baseline report from
another QC run) that a plain `QCCheck` deliberately doesn't have, so the
service calls them directly rather than through `profile.build_checks()`.

### Dataset-level metrics

`DatasetMetricsCollector` reads `transformed.jsonl` exactly once and
accumulates, per dataset:

- sample count, duplicate `sample_id` occurrences (first-index tracked)
- per-modality present-count and the full distribution of Step 7's
  per-window `modality_coverage` ratios (mean/median/min/max — richer
  than the boolean `modality_mask` alone)
- per scalar feature path: present/missing/null/non-finite counts plus
  streaming mean/std/min/max and a bounded percentile buffer
- window `row_count` distribution (min/max/mean/std)
- earliest/latest window timestamp, dataset duration, and any
  non-monotonic window-start regression

### Feature discovery

`app.qc.selectors.discover_scalar_feature_paths` recursively walks a
sample's `features` object and yields only genuine numeric scalars and
explicit nulls — never raw arrays, strings, sample IDs, timestamps, or
nested metadata objects. `bool` is explicitly excluded even though
`isinstance(True, int)` is `True` in Python — the bool check runs before
the numeric check, or `True`/`False` would silently become numeric
observations.

Completeness is defined over the **union of feature paths across the
whole dataset**, never just the first sample: if a path first appears at
sample 20, samples 0-19 count as missing for that path. This is
implemented in a single streaming pass (no second file read) via
retroactive backfill — when a path is first discovered at sample index
`i`, its accumulator starts with `missing_count=i`, since every prior
sample, by construction, didn't have it.

`completeness_ratio = numeric_present_count / total_samples` — a
structurally-absent path, an explicit `null`, and a non-finite value all
count against completeness, since none of them is a usable numeric
observation, but they are tracked as three separate counters
(`missing_count` / `null_count` / `non_finite_count`) in the report.

### Streaming Welford statistics

Mean/variance/min/max are computed via Welford's online algorithm
(`app.qc.accumulator.WelfordAccumulator`) — numerically stable, single
pass, and it never stores the underlying values. **Variance is population
variance (`ddof=0`)**, matching Step 7's own documented convention.

Percentiles need retained values, so `PercentileBuffer` keeps up to
`MAX_QC_VALUES_PER_FEATURE` (default 100,000) raw scalars per feature, in
first-encountered order — **never randomly sampled**. Beyond the cap,
later values are dropped and `percentiles_truncated=true` is set; mean/
std/min/max stay exact regardless, since those never depend on the
buffer. Percentiles use linear interpolation between closest ranks (the
same method as NumPy's default `interpolation="linear"`).

Memory is `O(number_of_features × MAX_QC_VALUES_PER_FEATURE)`, never
`O(number_of_samples × complete_sample_size)` — Step 8 never stores a
whole transformed sample, only the scalar values it needs.

### Modality coverage, variance, and range checks

`LOW_MODALITY_COVERAGE` compares each modality's dataset-wide present
ratio against a per-modality configured `minimum_ratio` — it never
rejects an individual sample, only reports the aggregate.

`LOW_FEATURE_VARIANCE` flags a scalar feature whose population variance
falls below a configured `minimum_variance` (default `1e-12`) — this
catches both perfectly constant features (`variance == 0`, e.g. a
`gyro_z_std` that's zero across every sample) and near-constant ones,
deliberately unified into a single threshold-based code for the MVP
rather than splitting `CONSTANT_FEATURE` / `NEAR_CONSTANT_FEATURE`.

`FEATURE_RANGE_VIOLATION` checks a *derived* Step 7 feature (e.g.
`features.imu.statistics.accel_x_mean`) against a configured `[min, max]`
— this is deliberately distinct from Step 3's raw-sensor-value
plausibility checks, which run on ingested values, not derived features.

`NON_FINITE_FEATURE_VALUE` is defensive: Step 7 already prohibits NaN/
Infinity in its output, so this should normally never fire. One issue is
emitted per affected feature *path* (an aggregate count), not per
occurrence, keeping issue volume bounded by feature count rather than
sample count; the offending values are excluded from mean/std/min/max so
one bad value never poisons the whole feature's statistics.

### Temporal coverage and duplicate sample IDs

The report always includes `earliest_timestamp` / `latest_timestamp` /
`duration_seconds` (from Step 7's window timestamps) and the window
`row_count` distribution — useful whenever `drop_incomplete=false` or
time-based windows produce uneven window sizes.

`NON_MONOTONIC_SAMPLE_TIME` fires (severity fixed at `error`, not
configurable — a structural invariant, not a quality preference) if a
later sample's window start precedes an earlier one's. Step 8 only
reports the regression; it never reorders samples.

`DUPLICATE_SAMPLE_ID` verifies the uniqueness Step 7's deterministic
sample IDs are supposed to guarantee — one issue per duplicate
occurrence, referencing the first index it was seen at. Step 8 never
deduplicates.

### Session/group imbalance — a deliberately limited check

Transformed samples carry no per-sample session field — only the
transformation manifest's `upstream.session_ids` (dataset-wide) does. The
current pipeline transforms one cleaning run from one synchronized
session, so `session_ids` almost always has exactly one entry. When it
does, `session_distribution` honestly reports `{session_id:
sample_count}`; `GROUP_IMBALANCE` is a no-op whenever fewer than two
groups are known, so a single-session dataset — which is, trivially,
"100% one group" — never manufactures a spurious imbalance finding. If
`upstream.session_ids` ever contains more than one ID, per-sample
attribution isn't possible from lineage alone, so `session_distribution`
is reported as `null` rather than fabricated. This is a real limitation,
not a placeholder: multi-session packaging would need per-sample group
metadata Step 7 doesn't currently emit.

### Baseline drift comparison — explicit, never automatic

A baseline is **never auto-selected** — the request must supply an
explicit `baseline_qc_id` (a `qc_id` from an earlier QC run, possibly
against a different transformation). This is deliberate for
reproducibility: "the previous dataset" is not a well-defined, stable
concept across arbitrary reruns.

When supplied, Step 8 loads the baseline's manifest + report, compares
profile identity (flagging `BASELINE_INCOMPATIBLE` — a warning, not a
block — on mismatch), and computes a **standardized mean difference**
per shared feature:

```
smd = (current_mean - baseline_mean) / baseline_std
```

Drift scores are always computed and reported when a baseline is given;
`FEATURE_DISTRIBUTION_DRIFT` issues are only emitted when
`drift.enabled=true` and `|smd|` exceeds
`max_abs_standardized_mean_difference`. `baseline_std == 0` is handled
safely: equal means yield `smd=0.0`; unequal means yield
`standardized_mean_difference: null` with
`reason: "baseline_std_zero_mean_shifted"`, and — since any shift away
from a perfectly constant baseline is real even though it can't be
standardized — this is still treated as drifted when drift checking is
enabled. A feature missing from either side is reported as
`compared: false` with a `reason`, never fabricated.

### QC statuses

- **`passed`** — no configured warning or failure threshold was violated.
- **`passed_with_warnings`** — one or more warnings, zero errors.
- **`failed`** — one or more error-severity issues (including the
  built-in `EMPTY_DATASET` case, which is always `error` regardless of
  configuration).

**A QC failure is never an HTTP failure** — `status: "failed"` returns
HTTP 200, since QC executed successfully and reported a real finding.
HTTP errors are reserved for invalid requests or genuine system failures.

### Report / manifest structure and determinism

`report.json` carries `summary` (counts), `dataset` (size/temporal),
`modality_coverage`, `features` (per-path distribution summaries),
`window_size`, `session_distribution`, `drift` (if a baseline was given),
and the bounded `issues` list. Issue detail is capped at
`MAX_QC_ISSUE_DETAILS` (default 1000); `issues_truncated=true` marks a
capped list, but `summary.issue_count` / `warning_count` / `error_count`
always reflect the **true total**, never just the truncated detail count.

`manifest.json` carries `qc_config_hash` (profile identity + QC engine
version + the full effective config, canonical-JSON-hashed —
`baseline_qc_id` and every threshold are included, so an equivalent
config always hashes identically), `report_sha256` (the report never
hashes itself — no circular hashing), and trimmed upstream lineage
(`transformation_id`, `cleaning_id`, `synchronization_id`,
`transformation_config_hash`, `session_ids`) without copying the entire
upstream manifest wholesale.

Given the same transformed bytes, profile, effective config, and baseline
report, the report's analytical content is deterministic — proven by
stripping only the volatile `qc_id` and comparing two independent runs
byte-for-byte equal.

### Lineage (gate before QC)

1. Locate the transformation manifest by `transformation_id` alone (a
   new, additive `TransformedArtifactStore.find_manifest_by_transformation_id()`
   glob lookup, mirroring Step 7/8's own bare-ID lookup precedent).
2. Locate the transformed artifact and recompute its SHA-256.
3. Compare against `manifest["transformed_sha256"]` — mismatch is `409
   TRANSFORMED_ARTIFACT_CHECKSUM_MISMATCH`.
4. Step 7 has no "rejected" concept (unlike Step 6's cleaning manifest) —
   it only ever commits a manifest for a successfully completed run, so a
   found, self-consistent manifest **is** the proof of an acceptable
   status.

### QC endpoint

`POST /api/v1/qc/{transformation_id}` — operates on an explicit
`transformation_id`, never an implicit "latest".

| Condition | Status |
|---------|--------|
| The transformation run, the requested QC profile, or an explicit `baseline_qc_id` doesn't exist | 404 |
| The transformed artifact's checksum no longer matches its manifest | 409 |
| The transformed artifact's format isn't readable (JSONL only for this MVP) | 415 |
| Invalid QC configuration | 400 |
| QC executed — including `status: "failed"`, which is a finding, not a server error | 200 |
| An actual internal failure | 500 |

```bash
curl -X POST http://localhost:8000/api/v1/qc/<TRANSFORMATION_ID> \
  -H "Content-Type: application/json" \
  -d '{"profile_name": "default_dataset_qc", "profile_version": "1.0.0", "config": {"minimum_samples": 5, "modality_coverage": {"imu": {"minimum_ratio": 0.95, "severity": "error"}, "gps": {"minimum_ratio": 0.80, "severity": "warning"}}, "feature_completeness": {"maximum_missing_ratio": 0.20}, "variance": {"enabled": true, "minimum_variance": 1e-12}}}'
```

```json
{
  "qc_id": "qc_bfc20cec-923f-476a-9bcd-e50348a2891d",
  "transformation_id": "xform_cbfa0881-a8b8-4973-8b71-9325e0466ce7",
  "status": "passed_with_warnings",
  "profile": { "name": "default_dataset_qc", "version": "1.0.0" },
  "summary": { "samples_checked": 7, "issue_count": 1, "warning_count": 1, "error_count": 0 },
  "report_uri": "file:///.../data/qc/<transformation_id>/<qc_id>/report.json"
}
```

### Logging

`QC_STARTED` / `QC_COMPLETED` / `QC_FAILED`, with `qc_id`,
`transformation_id`, `profile_name`, `profile_version`, and (on
completion) `samples_checked` / `warning_count` / `error_count` /
`status` — never individual feature values. `QC_FAILED` means the QC
*process* crashed (e.g. a malformed transformed artifact); a normal run
that concludes `status: "failed"` logs `QC_COMPLETED status=failed`, not
`QC_FAILED`.

---

## Step 9 — Dataset Packaging & Export Engine

Takes a *transformed* dataset with an *accepted QC report* (Step 8's
`passed`/`passed_with_warnings`) and converts it into a reproducible,
leakage-safe, versioned train/validation/test package a model can
actually be trained on. It reorganizes existing ML samples into split
files — it never changes their semantic content, generates new features,
imputes values, normalizes data, or re-runs QC.

```
transformed artifact + accepted QC report -> group-aware leakage-safe split assignment -> train/validation/test package + split index + report
```

### Why packaging is separate from transformation

Step 7 decides *what a sample is* (windowing + features). Step 9 decides
*which partition a sample belongs to* for training. Conflating the two
would mean every new packaging experiment (a different split ratio, a
different seed, a different grouping mode) requires re-deriving features
from scratch — wasteful, and it would blur "this sample's content" with
"this sample's role in an experiment," which are genuinely different
concerns with different reproducibility requirements.

### The leakage risk from overlapping windows

Step 7's overlapping count windows (e.g. `size=10, stride=5`) produce
samples that share source rows — window 0 covers rows 0-19, window 1
covers rows 10-29. If these were split independently between train and
test, the model would effectively see (nearly) the same underlying data
in both, inflating test performance without generalizing. **Step 9
implements group-aware splitting specifically to prevent this**: every
sample is first assigned to a leakage-prevention *group*, and every
sample in a group always goes to the same split, no matter what.

### Step 9 architecture

```
app/packaging/
    models.py        Pydantic request/response/manifest/report models
    service.py        PackagingService — QC gate + two-pass orchestration
    registry.py        PackagingProfileRegistry — explicit (name, version) lookup
    grouping.py        Group assignment: source_overlap (connected-component) + session
    splitter.py        Deterministic group -> split assignment: group_hash + sequential
    leakage.py        Post-assignment leakage verification (independent audit pass)
    metrics.py        Requested vs. actual split-ratio aggregation
    serialization.py    canonical_json (allow_nan=False), config/group-id hashing
    profiles/
        base.py            PackagingProfile contract
        default.py            Built-in default_ml_package profile
    exporters/
        base.py            DatasetExporter contracts (streaming-line vs. post-process)
        jsonl.py            Mandatory JSONL export
        parquet.py            Optional Parquet export (pyarrow, [parquet] extra)
app/storage/package_store.py    Staging + atomic commit, mirrors every other artifact store
app/api/routes/packaging.py    Thin HTTP layer — request/response + error mapping only
```

### QC gate

Before packaging:

1. Locate the transformation manifest by `transformation_id`, locate the
   transformed artifact, recompute its SHA-256, and compare against
   `manifest["transformed_sha256"]`.
2. Locate the QC run by the **exact `qc_id` supplied in the request** — by
   bare `qc_id` lookup, not the compound `(transformation_id, qc_id)` key,
   so a `qc_id` that genuinely belongs to a *different* transformation is
   still found and reported precisely as `409 QC_TRANSFORMATION_MISMATCH`
   rather than a generic `404`.
3. Verify the QC manifest's own `source_transformed_sha256` matches the
   freshly recomputed transformed checksum (the QC run must have been
   computed against these exact bytes).
4. Recompute the QC report's SHA-256 and compare against the QC
   manifest's `report_sha256` — a tampered QC report is never trusted.
5. Require QC status `passed` or `passed_with_warnings`. `failed` is
   rejected with `409 QC_NOT_ACCEPTED` — there is no `force=true` override
   in this MVP.

Step 9 never opens any artifact for writing along this chain — it only
reads the transformed artifact and the QC report/manifest, and writes
exclusively to its own package store.

### Grouping abstraction

Every sample is assigned to exactly one leakage-prevention *group* before
any split decision is made — grouping and splitting are deliberately
separate abstractions (`grouping.py` vs. `splitter.py`) so a splitting
strategy structurally cannot break a group apart; a splitter only ever
sees whole groups, never individual samples.

**`source_overlap`** (the primary mode, purpose-built for Step 7's
overlapping windows): a streaming connected-component algorithm over
inclusive `metadata.source_row_start`/`source_row_end` ranges. Samples are
assumed to arrive already ordered by source range (true for every Step 7
windowing mode), so this needs only `O(1)` *group* state — the current
group's running maximum end row:

```
current group maximum source_row_end
if next.start <= current_group_max_end: same group; update max_end
else: start a new group
```

`A=[0,19]`, `B=[10,29]`, `C=[25,44]`: A overlaps B, B overlaps C, so **all
three land in one connected group** even though A and C never directly
overlap (transitive closure). Boundary semantics are inclusive on both
ends: a window ending at row 19 and one starting at row 20 do *not*
overlap; if both include row 19, they do.

**A mathematical consequence worth knowing**: because Step 7's window
`size`/`stride` are constant for an entire transformation run, whether
consecutive windows overlap is a *constant* property of the run, not a
per-window one. This means uniform count-windowing produces one of two
extremes — never something in between:

- `stride < size` (overlapping): **every** window transitively chains
  into **one single group** spanning the whole dataset, however large.
- `stride >= size` (non-overlapping): every window is its own independent
  singleton group.

There is no config that produces "some overlapping clusters and some
independent groups" from a single uniform Step 7 windowing run — that
would require non-uniform window parameters, which Step 7 doesn't
support. See the end-to-end demo below for what each extreme looks like
in practice — and why a fully-overlapping dataset requesting a three-way
split is *correctly* rejected rather than forced.

**`session`** groups all samples by session. Transformed samples carry no
per-sample session field — only the transformation manifest's dataset-
wide `upstream.session_ids` does — so this only works when that list has
exactly one entry (the overwhelmingly common case: one cleaning run from
one synchronized session), producing exactly one group for the whole
run. With more than one session ID, per-sample attribution isn't possible
from lineage alone, and this mode explicitly refuses (`400
MISSING_GROUP_METADATA`) rather than fabricating a breakdown — mirroring
Step 8's identical limitation for `session_distribution`. A single-group
session-mode package correctly gets rejected if more than one non-zero
split is requested, since a session must never be split across
partitions.

### Deterministic group_hash splitting

Each group's split is decided by mapping a stable hash into `[0, 1)`:

```
smd_input = f"{group_id}:{seed}:{profile_name}:{profile_version}"
fraction  = int(sha256(smd_input).hexdigest()[:8], 16) / 2**32

[0, train_ratio)                        -> train
[train_ratio, train_ratio+validation_ratio) -> validation
remaining                               -> test
```

**Never Python's `hash()`** (randomized per-process for `str` by
default) and never runtime RNG state — SHA-256 only, so the same
group + seed + profile always produces the same fraction. Crucially, a
group's fraction depends **only on its own identity**, never on any other
group or the total group count — this is what makes assignments *stable
under dataset growth*: packaging groups `{A, B, C}` and later packaging
`{A, B, C, D}` with the same config never reshuffles A/B/C (see
`test_existing_groups_stable_when_unrelated_group_added`).

Group IDs are themselves content-derived and deterministic —
`sha256(transformed_sha256 : group_min_row : group_max_row)` for
source-overlap groups (using `transformed_sha256`, not
`transformation_id`, ties reproducibility to exact bytes), shortened to
`grp_<16 hex chars>`.

### Seed

`seed` (default `0` if omitted — a fixed, documented default, never an
arbitrary/random one) is mixed directly into every group's hash input and
is always part of `packaging_config_hash`. Changing the seed generally
changes at least some assignments for a sufficiently large dataset; the
same seed always reproduces the same assignments.

### Leakage verification — an independent audit pass

After assignment, `leakage.run_leakage_checks()` re-derives the
invariants Step 9 exists to guarantee, independently of the assignment
logic that produced them — a deliberate "trust but verify" step:

1. every `sample_id` appears in exactly one split
2. every group appears in exactly one split (`cross_split_groups`)
3. for `source_overlap` grouping specifically, a **second, independent**
   check re-derives connected overlap-chains directly from
   `(source_row_start, source_row_end)` ranges — not from the `group_id`
   column — and verifies no chain spans multiple splits
   (`cross_split_overlaps`)
4. total packaged samples equals the source transformed sample count

Any violation here means an internal engine bug, not a data problem —
the package is never committed in that case (`500
LEAKAGE_INVARIANT_VIOLATION` / `SAMPLE_COUNT_MISMATCH`).

### Requested vs. actual ratios

Hash-based splitting cannot guarantee exact ratios for a small number of
unevenly-sized groups — the report always shows both **sample-level** and
**group-level** ratios (a few large groups can skew sample ratios even
when group ratios look balanced), and never pretends the requested ratio
was hit exactly. Grouping is never violated just to force closer ratios.

### Completed vs. rejected packages

- **`completed`**: every requested non-zero split received at least one
  sample without breaking any group.
- **`rejected`**: policy wasn't satisfiable without violating grouping —
  `INSUFFICIENT_GROUPS_FOR_SPLIT` (fewer groups exist than non-zero
  requested splits) and/or `EMPTY_REQUESTED_SPLIT` (a specific non-zero
  split ended up with zero samples), or `EMPTY_SOURCE_DATASET` (zero
  transformed samples — normally already blocked by an accepted QC gate,
  handled defensively regardless).

A rejected package is **not** a server error — it's still `HTTP 200`,
and a complete, auditable artifact set (including deterministic
zero-byte split files) is still committed for reproducibility, exactly
like a `rejected` Step 6 cleaning run.

### Two-pass streaming design

Pass 1 reads `transformed.jsonl` once, extracting only lightweight
per-sample identity (`sample_id`, source row range) — never full feature
payloads — to compute group assignment, verify `sample_id`
presence/uniqueness, and decide split assignment. This keeps this phase's
memory at `O(number_of_samples)` of small records, not
`O(total feature payload size)`. Pass 2 re-reads the source once more and
writes each already-assigned sample directly into its split file, one
sample in memory at a time. Optional exports (Parquet) run as a *third*,
per-split step afterward, reading back the just-written JSONL file rather
than holding samples in memory a second time.

### JSONL export (mandatory)

`train.jsonl` / `validation.jsonl` / `test.jsonl`, one canonical-JSON
object per line (`sort_keys=True`, compact separators, `allow_nan=False`)
— the exact same convention used by every prior stage. Source order is
preserved *within* each split (hash determines partition only, never
reordering); no field is added, removed, rounded, or renamed relative to
Step 7's original sample.

### Optional Parquet export

Enabled via `"exports": ["parquet"]`, requires the optional `pyarrow`
dependency (`pip install .[parquet]`) — the base install works fully
without it, and requesting Parquet without pyarrow installed returns a
clear `415` rather than crashing. Since transformed samples are nested
and Step 7's feature schema varies by configured profile, Parquet
deliberately does **not** flatten every feature into its own column
(hundreds of unstable columns tied to whatever features happened to be
configured). Instead: a handful of stable index columns
(`sample_id`, `window_index`, `start_timestamp`, `end_timestamp`) plus
the full canonical sample JSON as a `sample_json` string column —
generic, schema-agnostic, and easy to explode later in pandas/Polars/
Hugging Face `datasets` if desired.

### split_index.jsonl

A side index — never merged into the model-facing JSONL — answering "why
is this sample in train?" and letting leakage constraints be verified
independently:

```json
{"sample_id": "...", "group_id": "grp_...", "split": "train", "source_row_start": 0, "source_row_end": 9}
```

Written in the same source order as `transformed.jsonl`, across all three
splits (not grouped by split). Its own SHA-256 is included in the package
manifest alongside every split file's.

### Package manifest and report

`manifest.json` carries `packaging_config_hash` (profile identity +
engine version + the full effective config — split strategy/ratios/seed,
grouping mode, export formats — canonical-JSON-hashed; **deliberately
excludes** `dataset_name`/`dataset_version`/`description`, since that
metadata never affects assignment or file bytes), `source_qc_status`
(preserved verbatim — a `passed_with_warnings` QC result is never
silently upgraded to `passed` just because packaging succeeded), per-split
SHA-256/size/URI, `split_index_sha256`, `report_sha256`, and trimmed
upstream lineage (`cleaning_id`, `synchronization_id`,
`transformation_config_hash`, `qc_config_hash`, `session_ids`,
`normalization_ids`) without copying either upstream manifest wholesale.

`report.json` carries only bounded aggregate metrics — summary, requested
vs. actual ratios (sample- and group-level), leakage-check counters, and
a compact `source_qc` reference (`status`/`warning_count`/`error_count`,
not every QC issue). Per-sample assignment belongs in `split_index.jsonl`,
never in `report.json`.

### Dataset name / version metadata

Optional `dataset_name` and `dataset_version` (validated as basic
`MAJOR.MINOR.PATCH` SemVer if provided) are stored in the manifest for
human reference — storage itself always stays keyed by IDs
(`transformation_id`/`package_id`), never by name. Versions are never
auto-incremented in this MVP; that belongs to future catalog/version-
management work (Step 10).

### Full lineage

```
raw ingestion -> schema validation -> integrity -> normalization
  -> synchronization -> cleaning -> transformation -> dataset QC
  -> dataset package (train / validation / test)
```

Every package is traceable back to exact transformed bytes
(`source_transformed_sha256`) and an exact QC decision (`qc_id` +
`source_qc_report_sha256` + `source_qc_status`).

### Packaging endpoint

`POST /api/v1/packaging/{transformation_id}` — operates on an explicit
`transformation_id` and an explicit `qc_id`, never an implicit "latest."

| Condition | Status |
|---------|--------|
| The transformation run, the exact QC run, or the requested packaging profile doesn't exist | 404 |
| The transformed checksum, QC-transformation match, or QC report checksum fails, or QC status isn't accepted | 409 |
| The transformed artifact's format isn't readable, or a requested export's dependency is missing | 415 |
| Invalid split ratios, unsupported strategy/grouping mode/export format | 400 |
| Packaging executed — including a policy-`rejected` package | 200 |
| An actual internal invariant/I-O failure (leakage violation, missing/duplicate `sample_id`) | 500 |

```bash
curl -X POST http://localhost:8000/api/v1/packaging/<TRANSFORMATION_ID> \
  -H "Content-Type: application/json" \
  -d '{"qc_id": "<QC_ID>", "profile_name": "default_ml_package", "profile_version": "1.0.0", "config": {"split": {"strategy": "group_hash", "train_ratio": 0.7, "validation_ratio": 0.15, "test_ratio": 0.15, "seed": 42}, "grouping": {"mode": "source_overlap"}, "exports": ["jsonl"]}, "dataset_name": "warehouse_robot_imu_gps", "dataset_version": "1.0.0"}'
```

```json
{
  "package_id": "pkg_1d37431b-0658-4892-bf99-6fa9edcaf70b",
  "transformation_id": "xform_2d91a812-9e5f-4e39-9f09-c9dc600d0757",
  "qc_id": "qc_1983ead2-4455-45b3-8ed3-b6ea956c12f9",
  "status": "completed",
  "profile": { "name": "default_ml_package", "version": "1.0.0" },
  "summary": { "source_samples": 30, "packaged_samples": 30, "group_count": 30 },
  "report_uri": "file:///.../data/packages/<transformation_id>/<package_id>/report.json",
  "rejection_reasons": []
}
```

### Logging

`PACKAGING_STARTED` / `PACKAGING_COMPLETED` / `PACKAGING_REJECTED` /
`PACKAGING_FAILED`, with `package_id`, `transformation_id`, `qc_id`,
`profile_name`/`profile_version`, split strategy, grouping mode, source
sample/group counts, per-split sample counts, and `status` — never
feature values or full samples. `PACKAGING_FAILED` means the packaging
*process* crashed; a normal run that concludes `status: "rejected"` logs
`PACKAGING_REJECTED`, not `PACKAGING_FAILED`.

---

## Step 10 — Dataset Registry, Versioning & Global Lineage

Not another transformation stage. Steps 1-9 each produce artifacts and
record lineage *within their own manifest* (a transformation manifest
knows its own cleaning parent; a package manifest knows its own
transformation and QC parents). Step 10 is a platform-level metadata,
catalog, and governance layer that indexes every one of those manifests
into a single queryable, cross-stage graph and answers questions no
single stage's manifest can answer alone: "where did this package
ultimately come from?", "what downstream artifacts depend on this raw
ingestion?", "can I reproduce the exact configuration that produced
dataset version 1.0.0?", "does every artifact still match what was
recorded?"

```
filesystem manifests (Steps 1-9, unchanged) -> catalog scan/rebuild -> SQLite index (artifacts + lineage edges) -> lineage/verification/dataset APIs
```

Step 10 never rewrites a Step 1-9 artifact, never reruns QC or a
transformation, and never auto-repairs broken lineage. It only reads.

### Filesystem manifests remain the source of truth

This is the load-bearing architectural decision of Step 10. `catalog.db`
is an **index**, not a database of record — every fact in it is derived
from a manifest already written by an earlier stage. Concretely:

- `rebuild()` can fully reconstruct the artifact/lineage tables from
  nothing but the 9 storage roots — proven by
  `tests/test_catalog_rebuild.py::test_rebuild_works_after_db_deleted`,
  which deletes `catalog.db` outright and rebuilds it from disk.
- The catalog is never consulted by Steps 1-9 themselves — deleting
  `data/catalog/` entirely does not affect ingestion, validation,
  normalization, or any other stage's ability to run.
- The one exception is genuinely new information the filesystem has no
  place for: user-registered **datasets** and **dataset versions** (see
  below). Those live only in SQLite and are deliberately preserved
  across every rebuild.

### Step 10 architecture

```
app/catalog/
    models.py        ArtifactType/RelationshipType enums, STAGE_RANK, all API request/response models
    errors.py        CatalogErrorCode enum + one exception subclass per code
    serialization.py    canonical_json, compute_manifest_sha256, compute_lineage_fingerprint
    repository.py    CatalogRepository — the only module that speaks raw SQL
    scanner.py        CatalogScanner — walks the 9 storage roots, parses manifests, upserts artifacts+edges
    graph.py        Cycle detection, upstream/downstream/both traversal, impact analysis
    verifier.py        ArtifactVerifier — recomputes checksums, never repairs
    versioning.py    Dataset name / SemVer validation, semantic version sorting
    service.py        CatalogService — orchestrates all of the above; one method per API route
app/storage/catalog_store.py    SQLite schema DDL + get_connection() (manual transactions, no ORM)
app/api/routes/catalog.py    /api/v1/catalog/* — scan, rebuild, health, artifact lookup, verify
app/api/routes/lineage.py    /api/v1/lineage/* — upstream/downstream traversal, impact analysis
app/api/routes/datasets.py    /api/v1/datasets/* — dataset/version registry, reproducibility
```

### Why SQLite, and only now

Steps 1-9 deliberately avoided any database — every stage's own lookups
(`find_manifest_by_id`, glob-by-ID) are cheap, local, single-artifact
operations a filesystem handles well. Step 10's query patterns are
different in kind, not degree: "every artifact downstream of this raw
ingestion" or "every dataset version affected if this package is
corrupted" requires traversing a graph that spans the *entire* storage
tree, repeatedly, on demand. Doing that with repeated filesystem globs
would mean re-parsing every manifest on every request. SQLite gives
indexed lookups and join-friendly traversal over that graph — it stores
metadata only (IDs, checksums, small JSON blobs), **never raw sensor
payloads or feature values**, so it stays small and is always
byte-for-byte reconstructible from source.

`data/catalog/catalog.db` is opened via stdlib `sqlite3` directly (no
ORM), with **explicit manual transactions** — `isolation_level=None` plus
hand-written `BEGIN`/`COMMIT`/`ROLLBACK` in `CatalogRepository.transaction()`
— rather than relying on Python's implicit transaction handling, so a
multi-statement registration (an artifact plus its lineage edges, or an
entire rebuild) commits or rolls back atomically as one unit. The
connection is opened with `check_same_thread=False`: FastAPI resolves a
sync dependency (which opens the connection) and the `async def` route
body that uses it on potentially different threadpool threads within one
request, and each request gets its own short-lived connection — never
shared across requests.

### Artifact model

Every artifact registered in the catalog shares the same core fields
(`artifact_id`, `artifact_type`, `pipeline_stage`, `status`,
`storage_uri`, `content_sha256`, `manifest_uri`, `manifest_sha256`,
`created_at`, `session_id`, `registered_at`) plus a `metadata_json`
column holding the **full parsed manifest**, stored as canonical JSON
text (never pickled) — this is a deliberate choice: cherry-picking
fields upfront would mean re-deriving the scanner every time a new
reproducibility field turns out to be needed later. The primary key is
the compound `(artifact_type, artifact_id)` — Step 10 never invents new
IDs, it only ever reuses the ID a stage already generated
(`ingestion_id`, `normalization_id`, `package_id`, ...).

Registration is idempotent: re-scanning the same manifest with identical
content is a no-op. If any immutable field (`content_sha256`,
`manifest_uri`, `manifest_sha256`, `storage_uri`, `metadata_json`) would
change on a re-scan, that's treated as a hard conflict —
`ARTIFACT_REGISTRY_CONFLICT` — never a silent overwrite, since under
every earlier stage's own immutability guarantees a manifest changing
after indexing is only possible if something tampered with it on disk.

### Lineage: an explicit parent -> child DAG, not a fake chain

Lineage edges use a constrained relationship vocabulary — `validated_from`,
`checked_from`, `normalized_from`, `synchronized_from`, `cleaned_from`,
`transformed_from`, `qc_of`, `packaged_from`, `approved_by_qc` — and the
scanner records only **direct semantic parent edges**, never every
transitive edge a full closure would imply. The graph genuinely branches:

```
ingestion(imu) -> validation(imu) -> integrity(imu) -> normalization(imu) --\
                                                                              +--> synchronization -> cleaning -> transformation --+--> package
ingestion(gps) -> validation(gps) -> integrity(gps) -> normalization(gps) --/                                                     |
                                                                          qc <-------------------------------------------------------+
                                                                          |
                                                                          +--> package  (approved_by_qc)
```

A package has **two** parents — `transformed_from` its transformation and
`approved_by_qc` its QC report — exactly mirroring the real dependency
(Step 9's packaging gate genuinely requires both). Multiple independent
normalization streams merge into one synchronization node. Nothing here
is forced into a linear chain.

Every edge insertion runs a **defensive cycle check** first
(`graph.would_create_cycle` — BFS forward from the would-be child,
checking whether the would-be parent is already reachable) and raises
`LINEAGE_CYCLE_DETECTED` if the new edge would close a loop. Normal
stage-ordered scanning can never naturally produce a cycle, but nothing
about the DAG structure is *assumed* acyclic without checking.

### CatalogScanner

`CatalogScanner.scan(repo, strict=...)` walks each of the 9 configured
storage roots in stage order (ingestion first, package last), skipping
`.tmp-*` staging directories and `.gitkeep` placeholders, parsing each
stage's real manifest schema, and upserting an artifact row plus its
direct parent edges. It reuses each stage's own storage class
(`LocalRawStorage.get_path(...)`, `LocalTransformedArtifactStore.find_manifest(...)`,
etc.) for every path it needs — the scanner never guesses a path layout
independently, and **it never trusts a manifest's own embedded
`storage_uri`/`artifact_uri` field as something to open**. That field is
stored purely as opaque metadata; the only paths ever opened are built
from the configured root plus a validated stage-generated ID via the
storage class's own public path-building method.

**Scan** (`POST /api/v1/catalog/scan`) is non-strict and incremental: a
parent that can't be found (e.g. mid-pipeline state, or a deliberately
orphaned artifact) is recorded as a `MISSING_LINEAGE_PARENT` issue and
scanning continues. **Rebuild** (`POST /api/v1/catalog/rebuild`) is
strict by default: it clears the artifact/edge tables first, then aborts
the whole operation — rolling back to the prior catalog state, changing
nothing — the moment it finds broken lineage, rather than silently
producing a half-populated index (`CATALOG_REBUILD_FAILED`).

### Rebuild preserves the dataset registry

`clear_artifact_index()` deletes only `artifacts`, `lineage_edges`, and
`lineage_issues` — it never touches `datasets` or `dataset_versions`,
because those are user-registered facts the filesystem has no manifest
for and rebuild cannot reconstruct them. `CatalogService.rebuild()`
asserts `datasets`/`dataset_versions` counts are identical before and
after every rebuild. If a rebuild (or an external filesystem change)
leaves a dataset version pointing at a `package_id` that no longer
resolves, `GET /api/v1/catalog/health` reports it as
`BROKEN_DATASET_VERSION_REFERENCE` — the version is never silently
deleted; a human decides what that means.

### Dataset registry & immutable versions

A `Dataset` is just a name/description/metadata container
(`dataset_name` validated against `^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$`).
A `DatasetVersion` binds one dataset name + one strict SemVer
(`MAJOR.MINOR.PATCH`, no pre-release/build metadata) to **exactly one**
package_id, permanently:

- Before a package can become a version, it must pass a gate: the
  package's own status must be `completed`, and its recorded
  `source_qc_status` must be `passed` or `passed_with_warnings` —
  otherwise `409 PACKAGE_NOT_ACCEPTED`. A rejected package can never
  become a dataset version, exactly as a rejected package can never be
  QC-overridden in Step 9.
- Re-registering the identical `(dataset_name, version, package_id)` is
  idempotent (`201` the first time, `200` after).
- Attempting to point an *existing* version at a *different* package_id
  is refused outright — `409 DATASET_VERSION_IMMUTABLE`. A dataset
  version is a permanent pointer; a changed package means a new version
  number, never an in-place reassignment.
- `"latest"` (`GET /api/v1/datasets/{name}/latest`) means the
  **highest SemVer**, explicitly not the most-recently-created version —
  registering `1.0.0` after `1.2.0` does not make `1.0.0` latest.

### ArtifactVerifier — checks, never repairs

`ArtifactVerifier.verify(repo, artifact_type, artifact_id)` recomputes
the artifact's content checksum and its manifest checksum from the
actual files on disk (via the same per-stage storage classes the
scanner uses) and compares them against what's registered:

| Result | Meaning |
|---|---|
| `verified` | content + manifest checksums both match |
| `missing` | not registered in the catalog, or the file no longer exists on disk |
| `checksum_mismatch` | the artifact's content bytes no longer match its recorded hash |
| `manifest_mismatch` | the manifest bytes no longer match their recorded hash |

`?recursive=true` walks the *entire* upstream lineage and verifies every
ancestor exactly once (branching parents, e.g. two normalization
streams, are never double-visited or looped). A verification run that
*finds* a real problem is still a normal, successfully-executed
operation — it returns `200`, mirroring the QC philosophy from Step 8
that a report of a genuine finding is not a server error.

### Reproducibility metadata & the lineage fingerprint

`GET /api/v1/datasets/{name}/versions/{version}/reproducibility` walks
the full upstream graph from a package and surfaces every
reproducibility-relevant value actually present in the indexed
manifests — raw `sha256` values, schema versions, per-stage config
hashes (normalization/synchronization/cleaning/transformation/qc/package),
split checksums, and whatever engine/transform version strings each
stage recorded. It never fabricates a value: a field with nothing
recorded (e.g. no git commit) comes back `null` rather than guessed.

The same response carries a **`lineage_fingerprint`** — a SHA-256 over a
canonical JSON payload built from a strict whitelist: raw checksums,
schema versions, and every stage's config hash and split checksums.
It deliberately **excludes** every execution ID (`ingestion_id`,
`transformation_id`, `package_id`, `qc_id`), every `created_at`
timestamp, and every filesystem path. This is proven, not just
documented: `tests/test_catalog_determinism.py::test_identical_pipeline_runs_produce_identical_fingerprint`
runs two **completely independent** pipeline executions over identical
source data and identical configuration — different session IDs,
different ingestion/normalization/transformation/qc/package IDs
throughout — and asserts the two resulting `lineage_fingerprint` values
are identical, while a differing seed (folded into the packaging config
hash) does change it. The fingerprint is a **provenance digest**, not a
cryptographic signature — it proves "these two runs used equivalent
content and configuration," not authorship or tamper-proof integrity.

### Catalog API

| Method & path | Purpose |
|---|---|
| `POST /api/v1/catalog/scan` | Non-strict incremental scan; records `MISSING_LINEAGE_PARENT` issues rather than failing |
| `POST /api/v1/catalog/rebuild` | Strict full reconstruction from the filesystem; aborts safely (prior catalog intact) on broken lineage |
| `GET /api/v1/catalog/health` | Artifact/edge/dataset/version counts, orphan detection, schema version, all outstanding issues |
| `GET /api/v1/catalog/artifacts` | List artifacts, filterable by `artifact_type`, `stage`, `status`, `session_id` |
| `GET /api/v1/catalog/artifacts/{type}/{id}` | Full artifact detail: metadata, direct parents, direct children |
| `GET /api/v1/lineage/{type}/{id}` | Traverse the DAG (`direction=upstream\|downstream\|both`, optional `max_depth`) |
| `GET /api/v1/lineage/{type}/{id}/impact` | Downstream impact analysis, counted per stage plus affected dataset versions |
| `POST /api/v1/catalog/verify/{type}/{id}` | Recompute + compare checksums (`?recursive=true` for full upstream) |
| `POST /api/v1/datasets` | Create a dataset (idempotent) |
| `GET /api/v1/datasets` | List datasets |
| `POST /api/v1/datasets/{name}/versions` | Register an immutable version pointing at one package |
| `GET /api/v1/datasets/{name}/versions` | List versions, sorted by SemVer |
| `GET /api/v1/datasets/{name}/versions/{version}` | One version's detail |
| `GET /api/v1/datasets/{name}/latest` | Highest-SemVer version |
| `GET /api/v1/datasets/{name}/versions/{version}/reproducibility` | Full reproducibility metadata + lineage fingerprint |

| Condition | Status |
|---|---|
| Scan, rebuild, health, artifact list/detail, lineage, impact, or a verification that ran successfully (including one that *finds* a mismatch) | 200 |
| Dataset or dataset version created | 201 (200 on idempotent re-registration) |
| Unknown `artifact_type`, invalid dataset name/SemVer | 400 |
| Artifact/dataset/version not found | 404 |
| Registry conflict, lineage cycle, version reassignment, package not accepted | 409 |
| Unexpected scan/rebuild failure | 500 |

### Logging

`CATALOG_SCAN_STARTED` / `CATALOG_SCAN_COMPLETED` / `CATALOG_SCAN_FAILED`,
`CATALOG_REBUILD_STARTED` / `CATALOG_REBUILD_COMPLETED` /
`CATALOG_REBUILD_FAILED`, `ARTIFACT_VERIFICATION_COMPLETED`,
`DATASET_CREATED`, `DATASET_VERSION_REGISTERED` — always artifact
type/ID, counts, and status; **never** full metadata blobs or sensor
values.

### Security: path safety

The scanner and verifier build every filesystem path exclusively from
the *configured* storage roots plus a validated, stage-generated ID via
each storage class's own public path-building method. A manifest's own
`storage_uri` is stored and returned as opaque text — it is never
parsed back into a `Path` and opened. `tests/test_catalog_service.py`
proves this directly: a hand-crafted ingestion manifest claiming a
`storage_uri` far outside every configured root is scanned successfully
(the field is indexed as plain metadata) without the target file ever
being touched, and a hostile `../../../etc/passwd`-style artifact ID
passed to the verifier resolves to `missing` via the catalog lookup —
it never reaches a filesystem call at all.

---

## Crash consistency and atomic artifacts (v2.1)

Not a new pipeline stage — a cross-cutting reliability upgrade applied to
every stage's storage layer. The question it answers: **what happens if
the process dies while writing an artifact** (SIGKILL, power loss, OOM,
an uncaught exception, a filesystem error, termination immediately
before or after the atomic rename)? The invariant this section
describes and tests: **no partially written artifact may ever appear at
a finalized storage location.**

### What v1.0 already had, and what was missing

Before v2.1, 6 of the 9 write-producing stores (normalization,
synchronization, cleaning, transformation, QC, packaging) already staged
their output into a hidden `.tmp-<id>` directory next to its eventual
final location and published it with one atomic `Path.rename()` — every
service wrapping that write phase in `try/except Exception:
store.discard(staging_dir)` and writing `manifest.json` **last**, right
before committing. Packaging's multi-file package directory (`train/
validation/test/split_index/manifest/report[/optional parquet]`) was
therefore already published as a single atomic unit — a reader could
never see `train.jsonl` without `test.jsonl`.

The gap was the other 3 stores: ingestion (`app/storage/local.py`),
validation reports, and integrity reports each created their *final*
directory first (`mkdir(exist_ok=False)`) and wrote into it directly —
so a crash mid-write left a partial, permanently-visible directory at
the final location. v2.1 closes this gap and, at the same time,
consolidates all 9 stores onto one shared primitive
(`app.storage.atomic`) instead of leaving the fsync/durability/fault-
injection work duplicated six times.

### Staging model

Two on-disk staging conventions coexist deliberately — both routed
through the same commit primitive, both equally invisible to discovery:

- **`.staging/<operation_id>/`** — a dedicated staging subtree, used by
  ingestion, validation, and integrity (stores that had no prior staging
  convention to preserve).
- **`.tmp-<artifact_id>/`** — sibling-of-final staging, used by the six
  stores that already had it. Kept as-is rather than migrated to the
  convention above: dozens of existing tests assert this exact directory
  name (`staging.name == ".tmp-norm_a"`), and renaming a working,
  already-invisible convention for six stores would have been pure
  churn with no safety benefit.

Both conventions are invisible to every `find_manifest`/`find_reports`
lookup and to the catalog scanner for the same two independent reasons:
`Path.glob()` skips dot-prefixed path components by default (so neither
`.staging` nor `.tmp-*` ever matches a `*` wildcard in a lookup glob),
and the scanner's `_is_staging_path()` guard checks for both prefixes
explicitly as defense-in-depth. `tests/test_staging_invisibility.py`
proves this directly — including a staging directory containing a
completely valid-looking, well-formed manifest, which is still never
returned by any lookup or indexed by a scan.

### Atomic publish lifecycle

Every store's commit path goes through exactly two functions in
`app/storage/atomic.py`:

```
writer creates staging_dir via create_staging_dir(...)
    -> writes ordinary files into staging_dir with ordinary file I/O
    -> (optional) write_manifest_file(...) for the manifest/report, last
    -> commit_staging_dir(staging_dir, final_dir)
         1. reject if final_dir already exists (no silent overwrite)
         2. ensure final_dir's parent chain exists
         3. optional verify(staging_dir) callback — raise to abort
         4. fsync every staged data file, then the manifest/report file
         5. remove the staging_state.json run-state journal
         6. fsync the staging directory itself
         7. Path.rename(staging_dir, final_dir)  <- the atomic step
         8. best-effort fsync of final_dir's parent
```

If anything raises before step 7, `final_dir` never exists. If step 7
succeeds, `final_dir` is exactly what was staged — there is no
in-between state a reader can observe.

### Durability guarantee — be precise about what is and isn't promised

- **Atomic visibility is mandatory**: `Path.rename()` on the same
  filesystem is atomic, so a reader only ever sees an artifact directory
  in its pre-existing state or its fully-committed state.
- **Full power-loss durability is best-effort**: this module fsyncs
  written files, the staging directory, and the destination's parent
  directory, but true durability also depends on the underlying
  filesystem and storage hardware actually honoring fsync — something
  this module cannot verify. Directory fsync is skipped, not fatal, on
  platforms that don't support it. `FSYNC_ENABLED=false` keeps atomic
  visibility but drops even the best-effort durability work (test-speed
  escape hatch only).

### Finalized artifact vs. run state

A finalized artifact's manifest only ever declares `completed` or
`rejected` (or each stage's equivalent terminal status) — never
`running`. In-progress run state lives entirely in a separate
`staging_state.json` journal inside the staging directory
(`operation_id`, `artifact_id`, `stage`, `started_at`, `pid`, `state` —
`writing` / `verifying` / `committing`, `final_destination`), and that
journal is deleted before the final rename, so it never becomes part of
a finalized artifact. A crash landing in the narrow window between that
deletion and the rename is classified `INVALID_STAGING_ENTRY` by the
recovery scanner rather than `STALE` — the safety invariant is
unaffected either way, since the directory is still under `.staging`/
`.tmp-` and invisible to every lookup.

### Stale staging detection and recovery

`app.storage.recovery.RecoveryService` scans every configured storage
root's `.staging/`/`.tmp-*` entries and classifies each one:

| Classification | Meaning |
|---|---|
| `ACTIVE` | `started_at` is within `STALE_STAGING_AFTER_SECONDS` — likely a real in-flight write |
| `STALE` | older than the threshold — almost certainly abandoned by a crashed process |
| `INVALID_STAGING_ENTRY` | `staging_state.json` is missing or unparseable — reported, never guessed at |

Liveness is **never** inferred from a bare PID (PIDs are reused, and
this scanner has no reliable, portable way to check whether a given PID
still refers to the same process) — classification is purely
time-based, which is conservative and fully portable. `scan()` is
read-only; `cleanup_stale()` only ever removes `STALE` entries — never
`ACTIVE`, and never `INVALID_STAGING_ENTRY`, since a directory this
module can't confidently date is reported for a human to look at, not
guessed at. Minimal endpoints: `GET /api/v1/recovery/scan`,
`POST /api/v1/recovery/cleanup?dry_run=`.

v2.1 does **not** implement record-level resume. Be precise about the
guarantee: **a failed or interrupted stage is safely rerunnable from the
beginning** — it is not true that a failed stage resumes from the exact
record where it stopped.

### Idempotency infrastructure (not wired into any live service)

`app.storage.idempotency.execution_key()` computes a deterministic
SHA-256 over `(stage, upstream_identity, upstream_content_sha256,
config_hash, implementation_version)` — never over a randomly generated
artifact ID, so two equivalent requests produce the same key. This is
infrastructure only: no v2.1 service calls it to deduplicate a request.
Wiring it in would change v1.0's existing, tested behavior (today, two
identical requests intentionally produce two distinct artifacts), so
that decision is left to a future, explicit opt-in per stage rather than
forced here.

### Catalog rebuild crash behavior

The catalog was not restructured for v2.1 — and deliberately so.
`CatalogService.rebuild()` already runs `clear_artifact_index()` +
`CatalogScanner.scan(strict=True)` inside one real SQLite transaction
(`CatalogRepository.transaction()`, manual `BEGIN`/`COMMIT`/`ROLLBACK`).
SQLite's own rollback-journal recovery already guarantees that a process
killed mid-transaction leaves the on-disk `catalog.db` in its
**pre-rebuild** state the moment any connection reopens it — no bespoke
crash-safety code is needed here, and introducing a temporary-database-
plus-swap strategy on top of that would add risk without adding safety.
`tests/test_crash_safety_subprocess.py` proves this with a real SIGKILL:
a child process is killed mid-transaction, and a fresh connection
afterward sees exactly the pre-kill artifact count.

### Fault injection

`app.storage.atomic.fault_injector` exposes named checkpoints
(`AFTER_STAGING_CREATED`, `AFTER_MANIFEST_WRITE`, `AFTER_DATA_FSYNC`,
`AFTER_MANIFEST_FSYNC`, `BEFORE_RENAME`, `AFTER_RENAME`,
`BEFORE_PARENT_FSYNC`) that are no-ops in production and raise
test-installed exceptions in `tests/test_atomic_commit.py` and
`tests/test_crash_safety_fault_injection.py`. Two real subprocess-kill
tests (`tests/test_crash_safety_subprocess.py`) additionally prove the
same guarantees against an actual `SIGKILL`, not just a simulated
Python exception, for both a filesystem store and the SQLite catalog.

---

## Large-data execution and resource model (v2.2)

Forge Data is **designed for large single-machine workloads** — long
multimodal sensor sessions on one developer laptop or one server, not
distributed cluster scale. This section documents, per stage, exactly
what "large" means: how memory scales, how many passes a stage makes
over its input, and — just as importantly — where a structure is
honestly *not* bounded, with a documented reason rather than a false
claim.

### Resource complexity by stage

| Stage | Memory model | Passes | Notes |
|---|---|---|---|
| Ingestion | O(chunk) — `STREAM_CHUNK_BYTES` (default 1 MiB) | 1 | Hashes while streaming; never buffers the full upload |
| Validation (CSV/JSONL) | O(1) + O(max_issues) | 1 | `csv.DictReader`/line-by-line; issues capped by `MAX_VALIDATION_ERRORS` |
| Validation (JSON array) | **O(dataset)** — documented limitation | 1 | `json.load()` requires the whole array; use CSV/JSONL for large files |
| Integrity (CSV/JSONL) | O(1) + O(max_issues) | 1 | Same shape as validation; capped by `MAX_INTEGRITY_ISSUES` |
| Integrity (JSON array) | **O(dataset)** — documented limitation | 1 | Same underlying reader as validation's JSON path |
| Normalization (CSV/JSONL) | O(chunk) | 1 | Streamed read → transform → streamed write |
| Normalization (JSON array output) | **O(dataset)** — documented limitation | 1 | A JSON array must be fully assembled before its closing `]`; CSV/JSONL output has no such constraint |
| Synchronization (reference-stream mode) | O(streams + bounded cursor state) | 1 | `StreamCursor` holds only `prev`/`pending` per stream — see below |
| Synchronization (fixed-rate mode) | O(streams + bounded cursor state) | 2 (documented) | Pass 1: streams each stream once for its (first, last) timestamp range only (no buffering). Pass 2: the real streaming alignment pass. The synthetic timeline itself is a lazy generator, never materialized |
| Cleaning (structural rules) | O(1) per row | 1 | Required-streams/coverage/privacy-redaction rules hold no cross-row state |
| Cleaning (duplicate detection, `backend=memory`, default) | **O(unique_rows)** — documented limitation | 1 | A `{content_hash: first_index}` dict; unchanged v1.0/v2.1 default |
| Cleaning (duplicate detection, `backend=sqlite`, v2.2) | O(1) process memory | 1 | Same exact semantics, seen-set spilled to a temporary on-disk SQLite index instead |
| Transformation (count windows) | O(window_size) | 1 | `collections.deque`, evicted as soon as no in-flight window needs a row |
| Transformation (time windows) | O(rows within the widest currently-open window span) | 1 | Windows close (and their rows are yielded/freed) as soon as the stream passes their end time |
| QC (count/mean/variance/min/max) | O(feature_count) | 1 | Welford's online algorithm — never stores raw values |
| QC (percentiles) | O(feature_count × min(n, `MAX_QC_VALUES_PER_FEATURE`)) | 1 | Capped, first-encountered-order retention — see "QC percentile behavior" below |
| Packaging (pass 1: grouping) | O(samples) — lightweight metadata only | 1 | `SampleRecord` holds 4 scalar fields per sample, never the feature payload |
| Packaging (pass 2: writers) | O(writer buffers), JSONL exporter | 1 | Streamed per-sample writes to split files |
| Packaging (Parquet exporter, optional) | **O(split size)** — documented limitation | 1 | pyarrow's table-building API accumulates full columns before writing; only reached when `exports` includes `"parquet"` |
| Catalog | O(metadata records), not raw dataset rows | — | SQLite indexes manifests/checksums only, never sensor payloads |

This table was produced by reading the actual implementation of every
row listed (`app/*/service.py`, `app/*/records.py`,
`app/synchronization/{timeline,readers,strategies,clocks}.py`,
`app/transformation/windowing.py`, `app/qc/accumulator.py`,
`app/packaging/grouping.py`, `app/packaging/exporters/*.py`) — not
assumed from prior documentation. Most of this codebase was already
disciplined about streaming before v2.2; the genuinely new work was the
sqlite dedup backend and the disk-preflight infrastructure below.

### Streaming guarantees

- Every reader that can be a generator is one: `iter_records`,
  `iter_typed_records`, `apply_stream_correction`, `fixed_rate_timeline`,
  `iter_count_windows`, `iter_time_windows` all `yield` rather than
  return a list.
- `StreamCursor` (`app/synchronization/strategies/base.py`) is the
  concrete mechanism behind synchronization's bounded memory: it holds
  exactly `prev` and `pending` per stream — never the whole stream —
  because alignment targets are always visited in non-decreasing order.
- Count-window transformation uses a `deque`, popped the moment a row
  can no longer belong to any in-flight window (`app/transformation/
  windowing.py`). Time-window transformation closes (and frees) a window
  as soon as the stream passes its end time.
- None of the above required a code change for v2.2 — they were already
  correct. What changed is that this is now measured and documented
  (`tests/load/`), not merely asserted.

### Known non-bounded structures (documented, not hidden)

Three structures are genuinely O(dataset), by design trade-off, not
oversight:

1. **JSON array input/output** (validation, integrity, normalization) —
   a top-level JSON array requires the whole array in memory to parse or
   to close with `]`. CSV and JSONL remain fully streaming for every
   stage. Adding a streaming JSON parser (e.g. `ijson`) was considered
   and rejected for v2.2: it's a new dependency for a format this
   project's primary robotics data paths (CSV/JSONL) don't need.
2. **The in-memory cleaning dedup backend** (`duplicate_policy.backend
   = "memory"`, the default) — O(unique_rows). This is exactly why the
   `sqlite` backend exists (see below) as an explicit, opt-in
   alternative with identical semantics.
3. **The optional Parquet exporter** — accumulates full columns before
   writing a row group. Parquet export is optional
   (`pip install .[parquet]`, only reached when a request's `exports`
   includes `"parquet"`); JSONL, the mandatory export, is fully
   streamed.

### Dedup backend behavior

```
duplicate_policy:
  enabled: true
  backend: memory | sqlite   # default: memory (unchanged v1.0/v2.1 behavior)
```

Both backends give **byte-identical, first-occurrence-retained exact
matches** — never an approximation (no Bloom filter: a false positive
would silently change which rows get dropped, which this project will
never accept for an exact-dedup guarantee). `backend="sqlite"` puts the
seen-set in a temporary on-disk index
(`app/cleaning/rules/duplicates.py::_SqliteSeenIndex`) inside the
cleaning run's own v2.1 staging directory instead of a process-memory
dict:

- Removed via `close()` (called in a `finally` around row processing,
  before `commit()`) on success, and via `discard_staging_dir()` on any
  failure — either way, it never becomes part of a finalized artifact
  and is never visible to catalog scans or artifact discovery.
- `tests/load/test_load_cleaning_dedup.py` measured both backends at
  50,000 and 1,000,000 unique rows on this project's reference machine:
  the memory backend grew from 40.5 MB to 227.4 MB (confirming the
  documented O(unique_rows) limitation is real); the sqlite backend
  stayed at 33.3 MB → 33.5 MB.
- No automatic memory→disk spillover threshold was implemented — an
  explicit per-request `backend` choice is simpler, has no surprising
  behavior change mid-run, and was judged sufficient for v2.2's stated
  goal ("Introduce a scalable exact-dedup backend without changing
  default semantic correctness" — not "auto-detect scale").

### QC percentile behavior

`WelfordAccumulator` (count/mean/variance/min/max) is **exact for any
dataset size** — a single-pass, numerically stable online algorithm
that never stores a raw value. `PercentileBuffer` is different and this
must be stated precisely: it retains up to `MAX_QC_VALUES_PER_FEATURE`
raw values **in first-encountered order** (never a random or reservoir
sample) and computes percentiles from that retained subset.

- Below the cap: percentiles are **exact**.
- Above the cap: percentiles are computed from a **first-N-values
  sample, not a statistically representative one** — if the underlying
  metric trends over time (e.g. a sensor's baseline drifting across a
  long session), a first-N sample is systematically biased toward the
  early part of the run. This was true before v2.2 and is now stated
  explicitly rather than left implicit.
- The report always exposes `percentiles_truncated: bool` — a caller
  can and should check it before treating a large dataset's percentiles
  as authoritative.
- No bounded-memory quantile sketch (e.g. t-digest, GK) was introduced
  for v2.2: the existing capped-buffer approach, once accurately
  labeled, was judged sufficient — this project would rather ship an
  honestly-labeled approximation than add complexity for a marginal
  accuracy gain nothing in this milestone's scope required.

### Disk preflight

`app/storage/disk_preflight.py` — `require_disk_space(path, stage=,
estimated_required_bytes=, reserve_bytes=, safety_factor=)` checks free
space on the target filesystem *before* a stage starts an expensive
write, raising `InsufficientDiskSpaceError` (mapped to HTTP `507`) with
structured `{code, stage, available_bytes, estimated_required_bytes,
reserve_bytes}` detail if space looks obviously insufficient.

- **Estimates, not measurements**: `estimate_required_bytes(input_bytes,
  ratio=)` multiplies input size by a documented, stage-chosen ratio
  (e.g. packaging uses 1.5× the transformed input, accounting for
  train/validation/test/split_index/report/manifest). This is a
  heuristic; free space can also change between the check and the
  actual write from unrelated activity on the same disk.
- **Wired in for v2.2**: ingestion (against `MAX_UPLOAD_SIZE_MB` as the
  worst case, since actual upload size isn't known upfront for a
  streamed multipart body) and packaging (against the transformed
  input's actual on-disk size). Normalization, synchronization,
  cleaning, transformation, and QC do **not** yet call this — they
  remain a documented gap, not silently assumed safe; the same
  `require_disk_space()` helper is ready for them to adopt.
- **Conservative defaults** (`DISK_RESERVE_BYTES=100 MiB`,
  `DISK_SAFETY_FACTOR=1.2`, `MIN_FREE_DISK_BYTES=50 MiB`) are
  deliberately small so a normal developer laptop or CI runner never
  trips them on a tiny test fixture — `tests/test_disk_preflight.py`
  and `tests/test_packaging_api.py::
  test_disk_preflight_rejects_impossible_request_before_writing` prove
  both the pass and the (intentionally astronomical, test-only) reject
  path.

### Temporary storage

Every piece of v2.2 temporary state lives inside a v2.1-recognized
staging location, never in `/tmp` or another untracked path:

- The sqlite dedup index lives inside the cleaning run's own staging
  directory (`.staging/<cleaning_id>/.dedup_index.sqlite3` in spirit —
  concretely wherever that store's existing `.tmp-<id>` staging
  directory is) and is removed before that directory is ever renamed
  into its final location.
- This means it is automatically covered by every v2.1 guarantee for
  free: invisible to catalog scans and artifact discovery, cleaned up by
  `discard_staging_dir()` on any failure, and never present in a
  finalized artifact — verified explicitly by
  `tests/load/test_load_crash_safety.py`, which injects a crash during a
  50,000-row cleaning run using the sqlite backend and confirms no
  leaked `.dedup_index.sqlite3*` file anywhere under the cleaned root.

### Load test methodology

`tests/load/` — deselected by default (`pytest -m "not load"` is the
project's default via `pyproject.toml`'s `addopts`); run explicitly with
`pytest -m load`. Peak memory is measured by
`tests/load/memory_utils.measure_peak_rss()`, which runs the workload
under test in a **fresh subprocess** (`multiprocessing`, "spawn" start
method) and reads that subprocess's own
`resource.getrusage(RUSAGE_SELF).ru_maxrss` after it exits — deliberate,
not incidental: `ru_maxrss` is a running historical maximum for a
process's entire lifetime, so measuring it inside the long-lived pytest
process itself would be contaminated by every previous test. A fresh
subprocess starts that counter at (near) zero.

Covered today: ingestion (byte-size scaling), CSV validation (1M rows,
bounded-issue accumulation, 100k/500k/1M growth comparison), the
cleaning dedup backends (50k vs. 1M unique rows), count-window
transformation (1M rows, all three stride/size relationships, and
window-size scaling), and one combined large-scale-plus-crash-injection
test. Synchronization, QC, and packaging are exercised at 100k/500k/1M
scale via the benchmark script (below) rather than as permanent
`load`-marked pytest files — their bounded-memory behavior was verified
by code audit (the table above) and by v1.0/v2.1's existing determinism
test suites; adding dedicated multi-hundred-line load-test files for
already-audited, already-bounded stages was judged lower value than the
stages that either changed in v2.2 (cleaning dedup) or are the most
data-size-sensitive by construction (validation, ingestion,
transformation).

### Benchmark instructions

```bash
python scripts/benchmark_large_pipeline.py                          # 100k / 500k / 1M rows
python scripts/benchmark_large_pipeline.py --sizes 10000             # a quick smoke run
python scripts/benchmark_large_pipeline.py --sizes 1000000 --keep-data
```

Generates synthetic IMU (+ 1/10th-rate GPS) CSVs, runs the full
ingestion → packaging pipeline via FastAPI's `TestClient`, and reports
per-stage duration/throughput/bytes plus one whole-run peak RSS per
dataset size (measured the same subprocess-isolated way as the load
tests). Results are specific to the machine that ran it — never copy a
number from this script into documentation as a guarantee. Numbers from
one real run are reported below in "Live verification results" for
context, not as a performance commitment.

### Live verification results

One real run of `python scripts/benchmark_large_pipeline.py --sizes
100000 500000 1000000`, macOS (darwin), Python 3.12.2, this project's
reference development machine, full ingestion→packaging pipeline
(IMU + 1/10th-rate GPS), default `duplicate_policy.backend="memory"`:

| Rows | Validation | Integrity | Normalization | Synchronization | Cleaning | Transformation | Whole-run peak RSS | Wall time |
|---|---|---|---|---|---|---|---|---|
| 100,000 | 0.18s (552k rec/s) | 0.25s (404k rec/s) | 0.66s (150k rec/s) | 1.11s (90k rec/s) | 1.16s (86k rec/s) | 0.38s (260k rec/s) | 106.7 MB | 4.3s |
| 500,000 | 0.90s (558k rec/s) | 1.21s (412k rec/s) | 3.32s (151k rec/s) | 5.44s (92k rec/s) | 5.83s (86k rec/s) | 1.89s (265k rec/s) | 270.5 MB | 20.3s |
| 1,000,000 | 1.80s (557k rec/s) | 2.43s (412k rec/s) | 6.60s (152k rec/s) | 10.89s (92k rec/s) | 11.67s (86k rec/s) | 3.80s (263k rec/s) | 469.6 MB | 40.4s |

Read this honestly:

- **Validation/integrity/normalization/synchronization/transformation
  throughput is essentially flat** across a 10x row-count increase
  (e.g. validation: 552k → 558k → 557k rec/s) — exactly what an O(1)-
  or O(bounded)-memory, single-pass stage should show; time scales
  linearly with rows, not superlinearly.
- **Whole-run peak RSS is not flat** (107 MB → 271 MB → 470 MB) — and it
  should not be, with the default cleaning dedup backend: this is the
  documented O(unique_rows) in-memory dedup index dominating the
  process's memory footprint, exactly as the resource-complexity table
  above predicts. This is the single biggest, most honest number in
  this whole report: it is not a bug, it is the *default* behavior this
  milestone explicitly measured, documented, and gave an alternative
  for.
- With `duplicate_policy.backend="sqlite"`, `test_load_cleaning_dedup.py`
  measured the same 20x unique-row increase (50,000 → 1,000,000) growing
  peak RSS by roughly 0.2 MB instead of ~187 MB — see "Dedup backend
  behavior" above for the exact figures from that run.
- Disk preflight correctly rejected an intentionally impossible request
  (an astronomical `DISK_RESERVE_BYTES`) with HTTP 507, before any
  package files were written — `tests/test_packaging_api.py::
  test_disk_preflight_rejects_impossible_request_before_writing`.
- A crash injected mid-way through a 50,000-row cleaning run (sqlite
  dedup backend) left no partial finalized artifact, no leaked dedup
  temp file, an unchanged upstream synchronized artifact and raw
  ingestion, and the stage succeeded on immediate retry —
  `tests/load/test_load_crash_safety.py`.

### Limitations

- **Not distributed, not cloud-scale.** Every number and guarantee in
  this section is for a single machine. This milestone deliberately did
  not add Spark/Dask/Ray/Celery/Kafka/Kubernetes/multiprocessing
  production workers — see the v2.2 non-goals.
  `Forge Data is designed for large single-machine workloads`, not
  described as handling arbitrarily large datasets.
- JSON array format, the in-memory dedup backend, and the Parquet
  exporter are O(dataset) — see "Known non-bounded structures" above.
  These are documented trade-offs with a stated reason, not silent gaps.
  Prefer CSV/JSONL and the `sqlite` dedup backend for large runs.
  PercentileBuffer beyond `MAX_QC_VALUES_PER_FEATURE` is an approximate,
  first-encountered-order sample, not a statistically representative one
  — check `percentiles_truncated` before trusting it at scale.
- Disk preflight is wired into ingestion and packaging only; it is a
  heuristic estimate, not a guarantee, and free space can still change
  between the check and the actual write.
- Runtime resource metrics (duration, throughput, bytes) are
  intentionally kept out of every manifest's own hash inputs — see
  `app.catalog.serialization` / the Step 10 lineage fingerprint, which
  is computed only from content/config identity, never from anything
  measured at runtime. A dataset version's `lineage_fingerprint` is
  unaffected by how fast or slow the machine that produced it was.

---

## Sensor plugin architecture (v2.3)

Not a new pipeline stage — a cross-cutting extensibility upgrade. The
question it answers: **if a robotics engineer wants to add a new sensor
type tomorrow, what exactly do they have to implement?** The answer, for
a normal tabular time-series sensor: one plugin package and one
registration line — see `docs/ADDING_SENSOR.md` for the practical
walkthrough. This section documents the architecture behind that answer.

### Pre-refactor coupling audit

Before writing any v2.3 code, every extension point was read directly
(not assumed from prior docs). Finding: this codebase was **already**
far more generic than a first guess would suggest.

| Component | Pre-v2.3 mechanism | Sensor coupling found | Change needed |
|---|---|---|---|
| Validation | format-based (`ValidatorRegistry`, keyed by file extension) | none — validators are schema-driven, not sensor-aware | none |
| Integrity | `IntegrityCheckerRegistry`, a hardcoded `{"imu": ..., "gps": ...}` dict built in `__init__` | closed map — adding a sensor meant editing this file | build the map from the sensor registry instead |
| Normalization engine (`RecordNormalizer`) | one generic engine interpreting any declarative `NormalizationProfile` | none — already fully generic | none |
| Normalization registry | hardcoded `_BUILTIN_PROFILES` tuple | closed map, same shape as integrity's | build from the sensor registry |
| Synchronization (readers, cursors, alignment strategies, clock correction, fixed-rate timeline) | schema-field-type-driven (`FieldType.FLOAT`/`INTEGER` → interpolate, else → nearest) | **none found** | none |
| Cleaning (rules) | operates on generic `row["streams"][name]` payloads | none | none |
| Transformation feature dispatch (`FeatureEngine`) | already takes an `extractors: dict[str, FeatureExtractor]` built by the caller | none in the engine itself | none |
| Transformation profile (`MultimodalWindowProfile`) | hardcoded `_STREAM_EXTRACTORS` dict + `FeaturesConfig` with explicit `imu`/`gps` Pydantic fields | closed map + closed request schema | resolve extractors from the sensor registry; make `FeaturesConfig` accept any registered sensor's stream name |
| QC (feature discovery) | recursive over whatever transformed samples contain | none | none |
| Packaging (grouping/splitting) | operates on sample metadata (`SampleRecord`), never content | none | none |
| Catalog | artifact-type-keyed, never sensor-keyed | none | none |

**The real coupling was narrow and specific**: three registries
(integrity, normalization, transformation-feature-dispatch) each
independently hardcoded which sensors exist, and could in principle
disagree with each other. Everything downstream of normalized records
(synchronization, cleaning, QC, packaging, catalog) was already sensor-
agnostic by construction, well before v2.3 existed. This audit is why
v2.3's actual code changes are much smaller than "a plugin system"
might suggest — see "Extension cost" below.

### The SensorPlugin contract

`app/sensors/base.py` — `SensorPlugin` is a frozen dataclass, not a "god
object": every field is either an instance of an abstraction that
already existed before v2.3 (`IntegrityChecker`, `NormalizationProfile`,
`FeatureExtractor`) or plain declarative metadata.

```python
@dataclass(frozen=True)
class SensorPlugin:
    sensor_type: str              # == schema_name == sync stream name == extractor stream_name
    plugin_version: str
    display_name: str
    schema_version: str
    integrity_checker: IntegrityChecker
    normalization_profile: NormalizationProfile
    feature_extractor: FeatureExtractor | None = None
    timestamp_field: str = "timestamp"
    numeric_fields: tuple[str, ...] = ()
    required_fields: tuple[str, ...] = ()
    canonical_units: dict[str, str] = {}
```

`__post_init__` enforces internal consistency at *registration* time,
never at request time: the normalization profile's `schema_name` and
`schema_version` must match the plugin's own, and a feature extractor's
`stream_name` must equal the plugin's `sensor_type`. A structurally
inconsistent plugin fails to construct, full stop —
`InvalidSensorPluginError` — rather than surfacing as a confusing
mismatch three requests later.

`sensor_type` is deliberately not a new identity: it's the one string
that IMU and GPS already used consistently as their schema name,
integrity-registry key, synchronization stream name, and
feature-extractor `stream_name` — v2.3 names that existing informal
convention, it doesn't invent a sixth one.

### Registry design

`app/sensors/registry.py` — `SensorPluginRegistry`: `register()`
(rejects a duplicate `sensor_type` — `DuplicateSensorPluginError`),
`get()` (raises `SensorPluginNotFoundError` with the requested type and
every available type listed), `list_plugins()` (deterministic,
sorted-by-key ordering), `is_registered()`.

Discovery is explicit and static (Design Requirement 22): no filesystem
scanning, no importlib entry points, no dynamic module execution.
`register_builtin_plugins(registry)` is a plain function that calls
`.register()` once per built-in — adding a sensor is adding one call to
this function, not teaching the registry to find plugins on its own.

**The coordination mechanism**: `IntegrityCheckerRegistry`,
`NormalizationProfileRegistry`, and `MultimodalWindowProfile` each now
build their internal map **from** a `SensorPluginRegistry` instead of
hardcoding their own — e.g.:

```python
# app/integrity/registry.py
self._checkers = {p.sensor_type: p.integrity_checker for p in registry.list_plugins()}
```

Every one of these three classes keeps its **exact pre-v2.3 public
interface** (`IntegrityCheckerRegistry.get()`/`.supports()`,
`NormalizationProfileRegistry.get()`/`.list_profiles()`,
`MultimodalWindowProfile.validate_config()`/`.build_extractors()`) — no
caller of any of them changed. This is what "one sensor registration →
pipeline extension points become available coherently" means in
practice: register once, and all three become aware of it
simultaneously, by construction, never by keeping three lists in sync
by hand.

### IMU / GPS migration

`app/sensors/imu.py` and `app/sensors/gps.py` are pure composition —
each builds one `SensorPlugin` instance out of the pre-existing,
**unchanged** `ImuIntegrityChecker`/`GpsIntegrityChecker`,
`IMU_CANONICAL_V1`/`GPS_CANONICAL_V1`, and
`ImuFeatureExtractor`/`GpsFeatureExtractor` objects. No IMU or GPS
behavior, threshold, alias, artifact format, or determinism changed —
the full pre-existing IMU/GPS test suite (940 tests going into v2.3)
passes unchanged, proving this was metadata-only reorganization, not a
rewrite.

### Force/Torque: the proof sensor

A generic 6-axis force/torque sensor — canonical fields `timestamp`,
`force_x/y/z` (N), `torque_x/y/z` (N·m), `device_id`. Not modeled on any
specific manufacturer.

**Supported unit conversions** (exact, factor-based, in
`app.normalization.transforms.units`):

| Dimension | Source unit | Factor to canonical |
|---|---|---|
| Force | N (canonical) | 1.0 |
| Force | kN | 1000.0 |
| Force | lbf | 4.4482216152605 (exact: 1 lbf = 1 lbm × standard gravity) |
| Torque | N·m / N*m (canonical) | 1.0 |
| Torque | mN·m / mN*m | 0.001 |
| Torque | lbf·ft / lbf*ft | 1.3558179483314004 (lbf factor × 0.3048 m/ft) |

Both an ASCII (`N*m`) and a Unicode middle-dot (`N·m`) spelling are
accepted as equivalent source units — the canonical unit itself is
reported as `N·m`.

**Integrity**: `ForceTorqueIntegrityChecker` — finiteness on every
component, the existing generic `TimestampSequenceChecker` for
ordering/duplicates, and `ForceTorqueThresholds`
(`max_abs_force_n`/`max_abs_torque_nm`) — both `None` (disabled) by
default. Real force/torque sensors span an enormous operating range
(a fingertip sensor vs. a robot base mount); this project asserts no
universal "correct" magnitude, and an exceeded threshold is always a
WARNING, never a hard ERROR.

**Normalization**: `FORCE_TORQUE_CANONICAL_V1` — a purely declarative
`NormalizationProfile` (aliases `fx/fy/fz/tx/ty/tz` → canonical names).
Zero new normalization *engine* code was needed — `RecordNormalizer`
already interprets any profile generically.

**Features**: `ForceTorqueFeatureExtractor` — raw sequences, per-axis
statistics, and two deterministic derived magnitudes:
`force_magnitude = sqrt(force_x² + force_y² + force_z²)`,
`torque_magnitude = sqrt(torque_x² + torque_y² + torque_z²)`. No
contact/grasp inference, no fabricated labels, no learned models —
consistent with this project's existing feature-extraction philosophy
(compare `ImuFeatureExtractor`'s `accel_magnitude`/`gyro_magnitude`).

### Compatibility: `FeaturesConfig`

The one generic-infrastructure change genuinely required to support a
*third* sensor's transformation features:
`app.transformation.models.FeaturesConfig` changed from
`model_config = ConfigDict(extra="forbid")` with only `imu`/`gps`
declared fields to `extra="allow"` plus a `stream_configs()` method that
merges the named fields with any extra (any other registered sensor's)
key. This is additive and backward compatible — every existing
`{"features": {"imu": {...}, "gps": {...}}}` request parses identically
to before. A `FeaturesConfig` block for an unregistered sensor name (or
one that has no feature extractor) still fails configuration validation
— just one layer later, inside `MultimodalWindowProfile.validate_config`
against the live sensor registry, instead of at Pydantic parse time —
preserving the "no unknown feature is ever silently ignored" guarantee.
This was the **only** structural change to `app/transformation/models.py`;
Force/Torque itself required zero further edits to that file.

### Version semantics

Three independent version axes already existed before v2.3 and are
**not** duplicated by plugin versioning:

- **`schema_version`** — the structural contract (`schemas/*.json`).
  Bump when required/optional fields, types, or the schema's own shape
  change.
- **`normalization_profile_version`** — the normalization logic/config
  targeting a schema version. Bump when aliases, unit dimensions, or
  conversion behavior change, independent of the schema.
- **`plugin_version`** (v2.3, new) — the plugin *descriptor* itself
  (which checker/profile/extractor objects are bundled, and their own
  declared metadata). In practice this only needs to move when the
  bundle's composition changes — swapping which profile/checker a
  plugin exposes — not on every profile-internal tweak, which is
  already covered by `normalization_profile_version`.

Lineage already captures `schema_name`/`schema_version` and
`profile_name`/`profile_version` in every normalization manifest (see
Step 4/`NormalizationManifest`) — this is unchanged and is the
provenance axis the reproducibility fingerprint and catalog lineage
already rely on. `plugin_version` is not separately written into
manifests: for v2.3's built-ins, the profile/checker bundle's identity
*is* the plugin's identity, so duplicating a second version field would
track the same fact twice. A future plugin whose descriptor genuinely
diverges from its profile's own versioning should reconsider whether
its profile_version already covers the distinction before adding a new
lineage field.

### API discovery

`GET /api/v1/sensors` and `GET /api/v1/sensors/{sensor_type}`
(`app/api/routes/sensors.py`) — read-only, metadata-only (never an
implementation object). An unknown `sensor_type` returns a structured
`404` naming both the requested type and every available one, mirroring
this project's existing "never a generic KeyError/500" convention (see
`SensorPluginNotFoundError`).

### Compatibility guarantees

- Existing IMU/GPS requests (schema names, profile names, endpoint
  paths) are unchanged — a client written against v1.0/v2.1/v2.2 needs
  no changes.
- No new required request field was introduced; `sensor_type` is
  resolved internally from `schema_name`/stream name, exactly as
  before.
- The full pre-v2.3 test suite (940 tests) passes unchanged, proving
  the migration altered no observable behavior for IMU/GPS.

### Extension cost

After the plugin framework existed, adding Force/Torque touched:

**Force/Torque-specific files (new, 6):**
`app/sensors/force_torque/{__init__.py,plugin.py,integrity.py,normalization.py,features.py}`,
`schemas/force_torque_v1.json`.

**Generic-infrastructure files changed to support pluggability in general
(built once, before Force/Torque existed as a concept — not
Force/Torque-specific):**
`app/sensors/base.py`, `app/sensors/registry.py`, `app/sensors/imu.py`,
`app/sensors/gps.py` (new); `app/integrity/registry.py`,
`app/normalization/registry.py`,
`app/transformation/profiles/multimodal_window.py`,
`app/transformation/models.py` (`FeaturesConfig`),
`app/transformation/service.py` (one line: `feature_configs =
request.config.features.stream_configs()`),
`app/normalization/transforms/units.py` (+`FORCE`/`TORQUE` constants —
data, not logic), `app/integrity/models.py` (+2 enum members),
`app/api/routes/sensors.py`, `app/sensors/models.py` (new).

**Core files that did NOT change:** every file under
`app/synchronization/`, `app/cleaning/`, `app/qc/`, `app/packaging/`,
`app/catalog/`. `tests/sensors/test_static_architecture.py` proves this
directly — a source-text search across every generic-core module for
the literal string `force_torque`, asserting zero matches, run as part
of the normal test suite (not opt-in).

### Limitations

- Discovery is static and explicit only — no filesystem plugin
  scanning, no importlib entry points, no third-party plugin
  installation. This is deliberate (Design Requirement 22); a dynamic
  discovery mechanism is a distinct, later concern with its own
  security surface (arbitrary code execution from a discovered module).
- No plugin sandboxing — a built-in plugin is trusted code in this
  process, same as every other module in this codebase.
- `plugin_version` is not independently recorded in lineage for v2.3's
  built-ins (see "Version semantics" above) — if a future plugin's
  identity genuinely needs to diverge from its profile's own
  versioning, that will need a deliberate lineage-schema decision, not
  an automatic one.
- Camera/image, ROS bag, and point-cloud/LiDAR sensor types are
  explicitly out of scope for v2.3 — this milestone is about the
  extension *architecture* for tabular time-series sensors, not about
  supporting every physical-AI data modality.

See `docs/ADDING_SENSOR.md` for the practical, step-by-step guide.

---

## Multiprocess concurrency model (v2.4)

Every prior milestone assumed one process touching the workspace at a
time. v2.4 makes Forge Data safe when **multiple local processes on the
same machine** — several `uvicorn` workers, concurrent pipeline
requests, independent scripts — share one workspace and one
`catalog.db`. The invariant that governs every decision below:
**concurrency must never weaken immutability, lineage, or atomic
visibility.** If a choice ever traded one of those away for
throughput, it was rejected.

This is explicitly **not** distributed or cross-machine concurrency.
There is no distributed lock, no leader election, no message queue, no
Postgres, no orchestrator, no job scheduler. Everything here is a
single-machine, multi-process problem, solved with what the local
filesystem and stdlib `sqlite3` already provide: OS-level file locking
(`fcntl.flock`) and SQLite's own WAL journal mode.

### SQLite connection policy

- **One connection per process, opened fresh per unit of work** — never
  a shared global, never passed between processes (a `sqlite3.Connection`
  isn't even picklable). `app.api.routes.catalog.get_catalog_service`
  already opened one connection per HTTP request before v2.4; every
  concurrency test and demo below does the same for whatever "process"
  means in that context (an HTTP request, a script, a worker).
- **WAL journaling, verified, not assumed** — `get_connection()`
  (`app/storage/catalog_store.py`) runs `PRAGMA journal_mode = WAL` and
  reads back SQLite's own reply; if it doesn't say `wal` (e.g. a
  filesystem that rejects WAL's shared-memory file), `get_connection`
  raises `JournalModeNotAppliedError` immediately rather than silently
  running without WAL's concurrent-reader guarantee. WAL lets readers
  proceed while a writer holds the write lock — verified live: readers
  in one process see a writer's committed state from another the
  instant it commits, never a partial write.
- **A bounded busy timeout** — `PRAGMA busy_timeout` (default 5000ms,
  `CATALOG_BUSY_TIMEOUT_MS`) makes a writer wait for a concurrent
  writer's lock instead of failing instantly, but only up to that
  bound. Exceeding it raises SQLite's own "database is locked", which
  `CatalogRepository.transaction()` catches and re-raises as a
  structured `CatalogBusyError(operation, timeout_ms, db_path)` — never
  a raw `sqlite3.OperationalError` reaching an API caller.
- **`foreign_keys = ON`** on every connection (SQLite's own default is
  off) — lineage-edge foreign keys are enforced at the database level,
  not just in application code.
- **One one-time exception**: the very first `PRAGMA journal_mode = WAL`
  ever run against a brand-new `catalog.db` briefly needs exclusive
  access to rewrite the file header, and SQLite's busy_timeout does not
  reliably cover that specific statement — confirmed directly with two
  processes opening a fresh database at the same instant. `get_connection`
  wraps only this one call in a small, bounded retry (≤10 attempts,
  capped well under a second) — every connection after the first sees
  WAL already applied and never takes this path.

### Short write transactions and race-safe writes

`CatalogRepository.transaction()` now opens with `BEGIN IMMEDIATE`
(SQLite's default `BEGIN` is deferred — it doesn't actually take the
write lock until the first write statement, which is what let two
processes interleave a check and a write against the same row before
v2.4). `BEGIN IMMEDIATE` takes the write lock up front, so from that
point on exactly one process is ever inside a catalog write transaction
system-wide.

That serialization is what makes the second half of the fix safe: every
write path that used to **check, then act** (`SELECT` for an existing
row, then conditionally `INSERT`) now **acts, then lets the database's
own primary key decide**:

| Operation | Old (racy) pattern | v2.4 pattern | Race outcome |
|---|---|---|---|
| Register artifact | `SELECT` → `INSERT` if absent | `INSERT`, catch `IntegrityError`, re-fetch and compare | Same content → idempotent `"unchanged"`; different content → `ArtifactRegistryConflictError` |
| Register lineage edge | `SELECT` → `INSERT` if absent | `INSERT`, catch `IntegrityError` | Already exists → idempotent `False`; genuine FK violation still propagates |
| Create dataset | `SELECT` → return existing, else `INSERT` | `INSERT`, catch `IntegrityError` | Already exists → idempotent (first writer's description/metadata wins) |
| Register dataset version | `SELECT` → compare `package_id`, else `INSERT` | `INSERT`, catch `IntegrityError`, re-fetch and compare `package_id` | Same package → idempotent `"unchanged"`; different package → `DatasetVersionImmutableError` |
| Seed `catalog_schema_version` | `SELECT` → `INSERT` if absent | Cheap `SELECT` short-circuit (fast, never blocks under WAL) + `INSERT ... ON CONFLICT DO NOTHING` | Two simultaneous first-opens of a fresh `catalog.db` never raise a raw `IntegrityError` |

Every one of these is now correct regardless of which process's
`BEGIN IMMEDIATE` wins the race — the losing process never sees an
unhandled `sqlite3.IntegrityError`, only the specific structured
outcome its use case defines as correct.

### Rebuild: an exclusive maintenance operation

`CatalogService.rebuild()` clears and reconstructs the entire artifact
index from the filesystem in one pass — a maintenance operation, not a
routine write, so at most one process may run it at a time. This is
enforced by `app/catalog/rebuild_lock.py`'s `RebuildLock`, a real
OS-level lock (`fcntl.flock(LOCK_EX | LOCK_NB)`) on
`data/catalog/catalog.rebuild.lock` — deliberately **not** a "does a
lock file exist" check, which is vulnerable to a stale file left behind
by a process that crashed mid-rebuild and would then block every future
rebuild forever. `flock` is released by the kernel the instant the
holding process exits for any reason, including `SIGKILL` — verified
live by killing a lock-holding process outright and confirming the next
rebuild acquires the lock immediately, with no stale-lock cleanup logic
anywhere.

Policy: **non-blocking, fail immediately**
(`CATALOG_REBUILD_LOCK_TIMEOUT_MS = 0`). If another process already
holds the rebuild lock, the caller gets a structured
`CatalogRebuildInProgressError` right away rather than waiting — this
project's explicit choice for being simpler to reason about than a
bounded wait, at the cost of a rebuild call occasionally needing a
manual retry.

The lock file's contents (`pid`, `hostname`, `started_at`,
`operation_id`) are **diagnostic-only** — printed in the conflict error
for a human debugging "who's rebuilding right now" — and are never used
to decide whether the lock is actually held. Only the OS `flock` result
decides that, because PIDs can be reused and a stale PID in a file would
be an unsafe thing to trust.

### Lock acquisition order

Every write path in Forge Data acquires locks in the same fixed order,
so no two code paths can deadlock against each other:

1. **Filesystem work first** (writing to a staging directory, hashing,
   atomic rename into place — v2.1's staging/commit primitive) —
   entirely before any SQLite transaction opens.
2. **Atomic publish** — the staging→destination rename that makes an
   artifact visible, per v2.1. Still no SQLite transaction open.
3. **A short catalog write transaction** (`BEGIN IMMEDIATE` → the
   INSERT(s) → `COMMIT`) registers the already-published artifact.

Rebuild follows the same shape with one lock prepended: **the exclusive
rebuild lock first, then a catalog write transaction** — never the
reverse, which is what keeps rebuild from being able to deadlock against
a normal registration (a registration never takes the rebuild lock at
all).

### The filesystem/catalog non-atomicity boundary

Publishing a filesystem artifact and registering it in the catalog are
**deliberately not one distributed transaction** — there is no
two-phase commit between the filesystem and SQLite. This means a valid,
fully-committed artifact can transiently exist on disk with no matching
catalog row yet (between step 2 and step 3 above, or if a process dies
in between). This is accepted, not a bug: the filesystem manifest
remains the single source of truth (the principle established in Step
10/v2.1), the catalog is a rebuildable index over it, and a later
`scan()` or `rebuild()` reconciles any such gap by discovering and
registering the artifact from its manifest.

The direction of that asymmetry is intentional and load-bearing: **a
catalog registration failure must never delete or roll back a valid,
already-published filesystem artifact.** `CatalogRepository`'s
`transaction()` rolls back only the SQLite side on failure — it never
touches the filesystem. A partially-registered artifact is a temporary,
self-healing inconsistency; a deleted artifact would be permanent data
loss. Given the choice, Forge Data always fails toward "reconcile later"
rather than "delete now."

### Known limitation: scan/rebuild hold a long write transaction

`CatalogScanner.scan()` walks the filesystem lazily — each artifact's
manifest is hashed and parsed on the fly as `CatalogService.scan()`/
`rebuild()` iterate it, and that iteration happens *inside* the open
catalog write transaction (cycle-detection and missing-parent checks
need the DB state built up by earlier artifacts in the same pass, so a
clean "collect everything, then write" split isn't a small change).
This means a slow scan over a very large workspace holds the write lock
for the scan's whole duration — a real, documented trade-off, not an
oversight. It's mitigated, not eliminated: any writer that collides with
an in-progress scan/rebuild waits up to `CATALOG_BUSY_TIMEOUT_MS` and
then gets a structured `CatalogBusyError` rather than hanging forever or
crashing — verified live (see below). Design Requirement 6's framing of
rebuild as a maintenance operation, run occasionally rather than as a
routine request-path write, is the intended mitigation for this in
practice.

### Verification

**Automated**: `tests/concurrency/` (`pytest -m concurrency`, kept
separate from the default suite exactly like v2.2's `load` marker) runs
every scenario against **real OS processes** — `multiprocessing` with
the `spawn` context, never a sequential simulation of concurrency —
covering: WAL/PRAGMA verification, per-process connections, concurrent
distinct-artifact registration, same-artifact races (identical content
idempotent, differing content a structured conflict), concurrent
identical/duplicate lineage-edge insertion, concurrent dataset creation,
same-version-same-package races (idempotent), same-version-different-
package races (structured conflict), competing rebuilds (one lock
owner, one structured conflict), the rebuild lock releasing after both
a normal exit and an exception, the rebuild lock releasing when its
holder is `SIGKILL`ed outright, a writer waiting out a busy_timeout and
succeeding, a writer exceeding its busy_timeout and getting a structured
`CatalogBusyError`, a real process crash (`os._exit`, no cleanup)
mid-write-transaction leaving the catalog fully recoverable
(`PRAGMA integrity_check` = `ok`, `PRAGMA foreign_key_check` = no
violations), and a multi-round mixed-writer stress pass ending in a full
consistency check.

**Live**: run against a real `uvicorn --workers 4` process (genuine
separate OS worker processes, not simulated) with real `curl` requests:
4 concurrent ingestion uploads all succeeded with distinct artifacts; 4
concurrent `/catalog/scan` calls all returned 200 with exactly one
reporting the new registrations and the rest correctly reporting them
as already-registered; a same-version/same-package registration race
returned exactly one 201 and the rest identical 200s; a same-version/
different-package race returned exactly one 201 and the rest a
structured 409 `DATASET_VERSION_IMMUTABLE`; an externally-held rebuild
lock made a concurrent `/catalog/rebuild` call return a structured 409
`CATALOG_REBUILD_IN_PROGRESS` naming the real holder PID, with a normal
200 once released; and `SIGKILL`ing a live worker process mid-burst of
40 concurrent writes still completed all 40 successfully (uvicorn
routed around the dead worker and auto-respawned it), with
`PRAGMA integrity_check` = `ok` and zero `foreign_key_check` violations
afterward.

### Non-goals

No distributed workers, no cloud task queues, no Celery/Redis/Kafka/
Ray/Dask/Kubernetes, no cross-machine coordination, no leader election,
no distributed or remote-filesystem locking, no PostgreSQL, no auth or
multi-tenancy, no web dashboard, no orchestration or job scheduler, no
selective/partial rebuild, no CLI productization. All of that remains
explicitly out of scope — this milestone is single-machine,
multi-process safety only.

---

## Data governance and selective rebuild (v2.5)

Every prior milestone answers "what produced this artifact?" (lineage)
and "what depends on this artifact?" (impact). v2.5 adds the missing
third question: **"this artifact is now known to be bad — what should
happen next?"** The answer is governed by one invariant that shapes
every design choice below:

**Bad data is never repaired in place.** The old artifact stays exactly
as it was; a governance judgment marks it deprecated/invalid; impact
analysis finds everyone who depends on it; a selective rebuild produces
a *new* lineage branch; a corrected dataset version points at the new
branch. The old branch, and the old dataset version, remain fully
intact and inspectable forever.

### Governance state: separate from artifact content

Governance is catalog metadata, never a manifest edit. Filesystem
artifacts stay exactly as immutable as they were in v1.0 — nothing here
opens, rewrites, or annotates a `manifest.json`.

Three states, deliberately not more:

| State | Meaning | New downstream work through it |
|---|---|---|
| **active** (default) | Nothing wrong has been recorded | Allowed |
| **deprecated** | Still historically valid; a better alternative exists | Blocked by default, allowed with `allow_deprecated=true` |
| **invalid** | Known incorrect or untrustworthy | Always blocked — no override |

**No row means active** (Design Requirement 3) — `artifact_governance`
only ever holds rows for artifacts someone has flagged, so the table
stays proportional to actual bad data, not every artifact ever
registered. Reactivating an artifact *deletes* its current-state row
(back to "no row = active") but never touches its event history.

Every state transition is recorded in an **append-only**
`artifact_governance_events` table — reactivating after an invalidation
does not erase the invalidation event; it adds a new one. A transition
always requires a non-empty `reason`; there is no anonymous or silent
state change. An optional `actor` field is recorded verbatim if the
caller supplies one — never invented if it's absent, since this system
has no authentication to derive an identity from.

Allowed transitions (Design Requirement 31): `active → {deprecated,
invalid}`, `deprecated → {active, invalid}`, `invalid → {active,
deprecated}`, and every state to itself (deliberately — it lets a
caller update the reason or attach `superseded_by` without first
reactivating, and still lands as its own honest event).

**Concurrency**: a governance update's read (current state), transition
validation, and write all happen inside one already-open `BEGIN
IMMEDIATE` transaction (see v2.4's `CatalogRepository.transaction()`),
so two processes racing to update the same artifact are fully
serialized by SQLite's own write lock — never a lost event, never a
corrupted current-state row. Verified with real separate OS processes
in `tests/concurrency/test_v25_governance_and_rebuild_locks.py`.

### The downstream-processing gate

Every pipeline stage's create route (`/normalization`, `/synchronization`,
`/cleaning`, `/transformation`, `/qc`, `/packaging`) now checks its
direct input artifact's governance *and the artifact's full upstream
chain* before calling the (otherwise completely unmodified) stage
service:

- **Direct input is invalid**, or **an ancestor is invalid** → always
  rejected (`ARTIFACT_INVALID` / `UPSTREAM_ARTIFACT_INVALID`, HTTP 409).
  There is no override; an invalid artifact can never feed new work.
- **Direct input is deprecated**, or **an ancestor is deprecated** →
  rejected by default (`ARTIFACT_DEPRECATED` /
  `UPSTREAM_ARTIFACT_DEPRECATED`), but a caller can pass
  `?allow_deprecated=true` to proceed anyway.

The transitive check (Design Requirement 8, `verify_governance_chain`)
matters because a direct input can look perfectly active while an
ancestor several stages back is invalid — e.g. an active synchronization
built from an invalid IMU normalization. Registering a new dataset
version goes through the identical gate against the package's full
upstream chain (Design Requirement 33) — `allow_deprecated=true` is
available there too, with the same no-override-for-invalid rule.

**A real, load-bearing limitation**: catalog population in this system
is *scan-driven*, not automatic (established in Step 10/v2.1 — no
pipeline stage has ever auto-registered into the catalog). This means
the gate can only see what a `/catalog/scan` has already discovered. An
artifact created and consumed downstream without an intervening scan
has no governance information to check against yet, and is silently let
through. This is not a v2.5-specific gap to fix — it's the same
"catalog is a rebuildable index, not source of truth for execution"
principle every prior milestone already relies on, just now also true
for governance. Call `/catalog/scan` after marking something bad and
before relying on the gate to block new work through it.

### Enriched impact analysis

`GET /api/v1/lineage/{type}/{id}/impact/enriched` (a new endpoint,
alongside the original `/impact` which is unchanged for backward
compatibility) returns everything `/impact` already did, plus:

- the source artifact's own governance state,
- every affected package,
- every affected dataset version, each with a computed
  **effective status**.

### Dataset-version governance and effective status

The `(dataset_name, version) -> package_id` mapping remains exactly as
immutable as v1.0 made it — nothing in v2.5 ever repoints a version.
Dataset versions get their own `dataset_version_governance` /
`dataset_version_governance_events` tables, identical in shape and
concurrency behavior to artifact governance.

Rather than manually walking every downstream dataset version and
marking each one individually when an upstream artifact turns out bad,
`DatasetVersionResponse` exposes a **computed** `effective_status`,
kept explicitly separate from the version's own explicit governance:

| `effective_status` | Meaning |
|---|---|
| `healthy` | No explicit governance, no invalid upstream ancestor |
| `deprecated` / `invalid` | Explicit governance was set directly on this version (always wins over anything derived) |
| `affected` | No explicit governance on the version itself, but its package's upstream chain contains an INVALID artifact |

A **deprecated** ancestor alone never marks a version `affected` — per
the state semantics above, a deprecated artifact's existing descendants
remain historically intact; only an invalid ancestor propagates this
way. `effective_status` is pure catalog metadata computed at read time;
`lineage_fingerprint` is computed exclusively from content/config
hashes and is never affected by governance state (Design Requirement 28
— verified directly in `tests/test_v25_dataset_version_governance.py`).

### Selective rebuild: planning

`POST /api/v1/rebuild/plan` takes a `{old_type, old_id, new_type,
new_id}` replacement pair — a known-bad artifact and an already-created
replacement for it (built the normal way, through the normal pipeline
API) — and returns an ordered plan of exactly the downstream descendants
that need rebuilding.

**Compatibility checked before planning** (Design Requirement 14):
`old_type` must equal `new_type`; both must already be in the catalog;
they must share a `session_id` where both have one; for normalization
specifically, they must share the same schema name and the same source
`ingestion_id` — a GPS normalization can never replace an IMU
normalization. A mismatch raises `REBUILD_REPLACEMENT_INCOMPATIBLE`.

**Real topological order, not a linear-chain assumption** (Design
Requirement 15): the planner runs Kahn's algorithm over the actual
downstream lineage DAG (with defensive cycle detection, even though the
catalog's own edge-insertion-time check already makes a cycle
unreachable), because the DAG genuinely branches — synchronization can
have several normalization parents, packaging depends on both
transformation and QC.

**Selective reuse, not a full re-run** (Design Requirement 16): for
each affected descendant, every parent that ISN'T on the replaced
lineage is reused completely unchanged. If synchronization depends on
IMU + GPS + Force/Torque normalization and only IMU is replaced, the
plan's synchronization step shows GPS and Force/Torque as `"replaced":
false` with their original IDs, and IMU as `"replaced": true` with
`"effective_id": null` (not known until execution) — this is the literal
meaning of *selective*.

**Honesty about config recoverability** (Design Requirement 17): every
stage's manifest was inspected directly. Synchronization's manifest
embeds its *entire* effective request (`reference`, `alignment_config`,
`clock_corrections`) — fully auto-reconstructable. Cleaning,
transformation, QC, and packaging manifests only ever recorded a
`*_config_hash` plus the profile/policy name+version — never the raw
config dict, because a hash can't be inverted. Every plan step for
those four stages is marked `manual_configuration_required: true` with
an explicit reason, rather than pretending automatic rebuild is
possible where it isn't.

**A plan fingerprint** (Design Requirement 23) — `SHA256` over the old
root, the new root, and every step's stage/old-id/manifest hash/parent
structure — lets execution detect drift: if the catalog changes
materially between planning and executing (a new descendant appears,
for instance), the stored fingerprint no longer matches a freshly
recomputed one, and execution is rejected with `REBUILD_PLAN_STALE`
rather than running against a stale description of the DAG.

Plans are held in a **process-local, in-memory store**
(`app/catalog/rebuild_plan_store.py`) — deliberately not persisted
anywhere. This is a real, documented limitation: under `uvicorn
--workers N` (v2.4), a plan built on one worker is invisible to a
different worker's execute call. No background orchestration, no
persistence layer, and no distributed plan store were in scope for this
milestone (see Non-goals); plan-then-execute against the same worker
(or a single-worker deployment) within a short window is the supported
usage.

### Selective rebuild: execution

`POST /api/v1/rebuild/execute` takes a `plan_id` plus an optional
`configs` map (keyed by `"<stage_artifact_type>/<old_artifact_id>"`)
supplying the raw config for every step the plan flagged
`manual_configuration_required`. It re-validates the plan's fingerprint
first (staleness check), then acquires a per-root exclusive lock
(`selective_rebuild.<old_type>.<old_id>.lock`, the same
`fcntl.flock`-based, non-blocking, fail-fast primitive as v2.4's
catalog-wide rebuild lock, just keyed by replacement root instead of
being global — Design Requirement 24), then runs each step in
topological order.

**Every step reuses the real, existing stage service directly** — no
HTTP round-trip, no duplicated stage logic (Design Requirement 18):
`app/catalog/rebuild_executor.py` constructs
`SynchronizationService`/`CleaningService`/`TransformationService`/
`QCService`/`PackagingService` exactly the way each route's own
`get_X_service` dependency does, and calls their real
`.synchronize()`/`.clean()`/`.transform()`/`.run_qc()`/`.package()`
methods. Every one of those already has v2.1's atomic staging/commit
guarantee, so a crash mid-step inherits that guarantee for free — it's
never reimplemented here.

**Every rebuild produces new artifact IDs; nothing old is ever
overwritten** (Design Requirement 19). Immediately after a step
succeeds, the OLD artifact it replaced is marked `deprecated` with
`superseded_by_type`/`superseded_by_id` pointing at the new one — a
governance relationship, not a causal lineage edge (Design Requirement
20); the real `synchronized_from`/`cleaned_from`/etc. edges from v1.0
are never touched.

**A step that's skipped (missing manual config) or fails cascades
correctly**: every step downstream of a skipped/failed one is reported
as `skipped_upstream_not_rebuilt` rather than being attempted against a
parent that was never produced — this was a real bug caught during
development (an early version attempted every step regardless and
crashed with a confusing `KeyError` deep inside per-stage
reconstruction; `tests/test_v25_rebuild_execution.py` pins the fix).

The executor does **not** itself register new artifacts into the
catalog — exactly like every other stage in this system, that stays
scan-driven. A `/catalog/scan` after `execute` is what makes rebuilt
artifacts queryable, governable, and eligible for a dataset-version
registration.

### Corrected dataset versions

The end-to-end workflow this all serves: `dataset@1.0.0 -> package_old`
is found `affected` by an invalid upstream artifact; a rebuild produces
`package_new`; the user registers `dataset@1.0.1 -> package_new` through
the ordinary (unmodified) `POST /datasets/{name}/versions` endpoint.
`1.0.0` is never touched — it remains registered, immutable, and its
`effective_status` now correctly reads `affected`. v2.5 never auto-picks
or auto-increments the next version number (Design Requirement 21); the
user always chooses and registers it explicitly.

### Catalog rebuild preserves governance

v2.4's `clear_artifact_index()` (used by `POST /catalog/rebuild`, the
filesystem-reconciliation rebuild — not to be confused with the
selective rebuild above) already never touched `datasets`/
`dataset_versions`. v2.5 extends that guarantee explicitly to
`artifact_governance`, `artifact_governance_events`,
`dataset_version_governance`, and `dataset_version_governance_events` —
none of these tables are referenced anywhere in the artifact-index
rebuild path, and `CatalogService.rebuild()` now asserts their row
counts are unchanged before/after, the same way it already asserted for
datasets/versions.

If a governance row's target artifact genuinely disappears from the
index (its manifest is gone from disk), the governance row is **not**
deleted — that would destroy audit history over what might be a
temporary filesystem issue. Instead, `GET /catalog/health` reports a
`BROKEN_GOVERNANCE_REFERENCE` issue. Marking an artifact invalid is
itself never a health issue — that's an intentional, healthy governance
state, kept explicitly distinct from actual catalog integrity problems
(Design Requirement 27).

### Limitations

- The downstream gate only sees artifacts that have been scanned — see
  "The downstream-processing gate" above.
- Rebuild plans are process-local and non-persistent — see "Selective
  rebuild: planning" above.
- Automatic rebuild is only possible for synchronization; cleaning,
  transformation, QC, and packaging always require the caller to supply
  the raw config at execute time, because only a hash of it was ever
  recorded.
- No automatic background rebuild, no job orchestration, no approval
  workflow, no RBAC, no retention/garbage-collection policy, and no
  automatic semantic-version incrementing — all explicitly out of scope
  for this milestone (see the v2.5 Non-goals in the design brief this
  section was built from).
- A plan only ever replaces ONE root artifact at a time; rebuilding
  against multiple independent bad artifacts in one operation isn't
  supported — build and execute one plan per root.

---

## Setup

```bash
cd ai_data_pipeline
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Running the server

```bash
uvicorn app.main:app --reload
```

The API is then available at `http://localhost:8000`.

## End-to-end demo

```bash
curl -X POST http://localhost:8000/api/v1/ingestion/upload \
  -F "file=@imu.csv" \
  -F "customer_id=test_customer" \
  -F "device_id=imu_001"
# -> { "ingestion_id": "ing_...", ... }

curl -X POST http://localhost:8000/api/v1/validation/<INGESTION_ID> \
  -H "Content-Type: application/json" \
  -d '{"schema_name": "imu", "schema_version": "1.0.0"}'
# -> { "status": "passed" | "failed", "summary": {...}, "report_uri": "..." }

curl -X POST http://localhost:8000/api/v1/integrity/<INGESTION_ID> \
  -H "Content-Type: application/json" \
  -d '{"schema_name": "imu", "schema_version": "1.0.0"}'
# -> { "status": "passed" | "passed_with_warnings" | "failed", "error_count": ..., "report_uri": "..." }

curl -X POST http://localhost:8000/api/v1/normalization/<INGESTION_ID> \
  -H "Content-Type: application/json" \
  -d '{"schema_name": "imu", "schema_version": "1.0.0", "profile_name": "imu_canonical", "profile_version": "1.0.0", "source_units": {"acceleration": "g", "angular_velocity": "deg/s"}}'
# -> { "status": "completed", "records_written": ..., "artifact_uri": "...", "normalized_sha256": "..." }

# Repeat ingest -> validate -> integrity -> normalize for a second (e.g. GPS)
# stream using the SAME session_id, then:
curl -X POST http://localhost:8000/api/v1/synchronization \
  -H "Content-Type: application/json" \
  -d '{"streams": [{"name": "imu", "normalization_id": "<NORM_ID_IMU>"}, {"name": "gps", "normalization_id": "<NORM_ID_GPS>"}], "reference": {"mode": "stream", "stream": "imu"}, "alignment": {"default_method": "nearest", "max_time_delta_ms": 100}}'
# -> { "status": "completed", "rows_written": ..., "coverage": {...}, "artifact_uri": "...", "synchronized_sha256": "..." }

curl -X POST http://localhost:8000/api/v1/cleaning/<SYNCHRONIZATION_ID> \
  -H "Content-Type: application/json" \
  -d '{"policy_name": "default_multimodal", "policy_version": "1.0.0", "config": {"required_streams": ["imu"], "min_present_streams": 1, "duplicate_policy": {"enabled": true}, "privacy": {"redact_fields": ["streams.gps.latitude", "streams.gps.longitude"]}}}'
# -> { "status": "completed" | "rejected", "summary": {...}, "artifact_uri": "...", "cleaned_sha256": "..." }

curl -X POST http://localhost:8000/api/v1/transformation/<CLEANING_ID> \
  -H "Content-Type: application/json" \
  -d '{"profile_name": "multimodal_window_v1", "profile_version": "1.0.0", "config": {"window": {"mode": "count", "size": 10, "stride": 5, "drop_incomplete": true}, "features": {"imu": {"include_raw": true, "statistics": ["mean", "std", "min", "max"], "derived": ["accel_magnitude"]}, "gps": {"statistics": ["mean"]}, "include_modality_mask": true, "include_relative_time": true}}}'
# -> { "status": "completed", "summary": {...}, "artifact_uri": "...", "transformed_sha256": "..." }

curl -X POST http://localhost:8000/api/v1/qc/<TRANSFORMATION_ID> \
  -H "Content-Type: application/json" \
  -d '{"profile_name": "default_dataset_qc", "profile_version": "1.0.0", "config": {"minimum_samples": 5, "modality_coverage": {"imu": {"minimum_ratio": 0.95, "severity": "error"}, "gps": {"minimum_ratio": 0.80, "severity": "warning"}}, "feature_completeness": {"maximum_missing_ratio": 0.20}, "variance": {"enabled": true, "minimum_variance": 1e-12}}}'
# -> { "status": "passed" | "passed_with_warnings" | "failed", "summary": {...}, "report_uri": "..." }

curl -X POST http://localhost:8000/api/v1/packaging/<TRANSFORMATION_ID> \
  -H "Content-Type: application/json" \
  -d '{"qc_id": "<QC_ID>", "profile_name": "default_ml_package", "profile_version": "1.0.0", "config": {"split": {"strategy": "group_hash", "train_ratio": 0.7, "validation_ratio": 0.15, "test_ratio": 0.15, "seed": 42}, "grouping": {"mode": "source_overlap"}, "exports": ["jsonl"]}}'
# -> { "status": "completed" | "rejected", "summary": {...}, "report_uri": "..." }
```

## Configuration

Environment variables (all optional, sensible defaults provided):

| Variable                  | Default             | Purpose                                       |
|-----------------------------|----------------------|-------------------------------------------------|
| `RAW_STORAGE_ROOT`          | `data/raw`           | Root directory for immutable raw storage        |
| `MAX_UPLOAD_SIZE_MB`        | `512`                | Maximum accepted upload size                    |
| `SCHEMA_DIR`                | `schemas`            | Directory of schema-definition JSON files       |
| `VALIDATION_STORAGE_ROOT`   | `data/validation`    | Root directory for persisted validation reports |
| `MAX_VALIDATION_ERRORS`     | `1000`               | Cap on detailed error objects stored per report |
| `INTEGRITY_STORAGE_ROOT`    | `data/integrity`     | Root directory for persisted integrity reports  |
| `MAX_INTEGRITY_ISSUES`      | `1000`               | Cap on detailed issue objects stored per report |
| `NORMALIZED_STORAGE_ROOT`   | `data/normalized`    | Root directory for persisted normalized artifacts |
| `SYNCHRONIZED_STORAGE_ROOT` | `data/synchronized`  | Root directory for persisted synchronized artifacts |
| `MAX_SYNC_FREQUENCY_HZ`     | `1000.0`             | Ceiling on fixed_rate synchronization frequency |
| `DEFAULT_SYNC_TOLERANCE_MS` | `100.0`              | Fallback alignment tolerance when a request doesn't specify one |
| `CLEANED_STORAGE_ROOT`      | `data/cleaned`       | Root directory for persisted cleaned artifacts  |
| `MAX_CLEANING_ISSUE_DETAILS`| `1000`               | Cap on dropped/redacted row examples stored per report (independently) |
| `TRANSFORMED_STORAGE_ROOT`  | `data/transformed`   | Root directory for persisted transformed artifacts |
| `MAX_WINDOW_SIZE`           | `100000`             | Ceiling on count-based window `size`            |
| `MAX_TIME_WINDOW_MS`        | `3600000.0`          | Ceiling on time-based window `duration_ms`      |
| `QC_STORAGE_ROOT`           | `data/qc`            | Root directory for persisted QC artifacts       |
| `MAX_QC_ISSUE_DETAILS`      | `1000`               | Cap on detailed issue objects stored per QC report |
| `MAX_QC_VALUES_PER_FEATURE` | `100000`             | Cap on raw scalar values retained per feature for exact percentiles |
| `PACKAGE_STORAGE_ROOT`      | `data/packages`      | Root directory for persisted dataset package artifacts |
| `CATALOG_DB_PATH`           | `data/catalog/catalog.db` | SQLite metadata catalog — an index over the manifests above, never their source of truth |
| `STAGING_DIR_NAME`          | `.staging`           | Staging subtree name for ingestion/validation/integrity (v2.1) |
| `STALE_STAGING_AFTER_SECONDS` | `3600.0`           | Age threshold before the recovery scanner classifies a staging entry STALE |
| `FSYNC_ENABLED`             | `true`               | fsync staged files/directories before/after atomic rename (v2.1); disabling keeps atomic visibility but drops best-effort durability |
| `STREAM_CHUNK_BYTES`        | `1048576` (1 MiB)    | Chunk size for streamed byte-level reads (v2.2) |
| `DISK_RESERVE_BYTES`        | `104857600` (100 MiB) | Headroom kept free beyond a stage's disk-space estimate (v2.2) |
| `DISK_SAFETY_FACTOR`        | `1.2`                | Multiplier applied to a stage's disk-space estimate before comparing (v2.2) |
| `MIN_FREE_DISK_BYTES`       | `52428800` (50 MiB)  | Absolute free-space floor, independent of any estimate (v2.2) |
| `CATALOG_BUSY_TIMEOUT_MS`   | `5000`               | How long a catalog write waits for another process's write lock before a structured `CatalogBusyError` (v2.4) |
| `CATALOG_JOURNAL_MODE`      | `WAL`                | SQLite journal mode, verified (not assumed) at connection time (v2.4) |
| `CATALOG_REBUILD_LOCK_TIMEOUT_MS` | `0`            | `0` = fail immediately if another process holds the rebuild lock; a positive value waits up to that long instead (v2.4) |

## How raw storage works

- Every ingestion event gets its own directory keyed by
  `customer_id/session_id/ingestion_id`.
- The directory is created with `exist_ok=False` — this is the immutability
  guard. A second write attempt at the same path fails with a 409 instead of
  overwriting existing data.
- Files are hashed and written in a single streaming pass, in fixed-size
  chunks, so large files are never fully buffered in memory.
- The raw file is never parsed, cleaned, or rewritten by any later stage —
  Steps 2-6 only ever open it read-only, and never touch `manifest.json`.
  Step 5 never even opens raw files directly — it reads normalized
  artifacts only, and only for lineage does it consult the raw ingestion
  manifest (for `session_id`), read-only. Step 6 doesn't touch raw files,
  the raw manifest, or any report at all — it trusts the synchronization
  manifest's own embedded lineage completely. Step 7 goes one step further
  still: it never opens raw files, validation/integrity reports,
  normalized artifacts, or the synchronized artifact at all — it trusts
  the cleaning manifest's own embedded lineage completely, exactly as
  Step 6 trusts the synchronization manifest's. Step 8 goes one step
  further again: it only ever opens the transformed artifact and its own
  manifest — no cleaned/synchronized/normalized/raw artifact or report of
  any kind — trusting the transformation manifest's embedded lineage
  completely. Step 9 only ever opens the transformed artifact and the QC
  report/manifest it was explicitly pointed at — no cleaned, synchronized,
  normalized, or raw artifact of any kind — trusting the transformation
  and QC manifests' embedded lineage completely.

## Running tests

```bash
pytest
```

878 tests total (23 Step 1 + 49 Step 2 + 44 Step 3 + 56 Step 4 + 87 Step 5
+ 81 Step 6 + 140 Step 7 + 145 Step 8 + 139 Step 9 + 114 Step 10, across
`test_catalog_repository.py`, `test_catalog_graph.py`,
`test_catalog_versioning.py`, `test_catalog_scanner.py`,
`test_catalog_api.py`, `test_catalog_lineage.py`,
`test_catalog_rebuild.py`, `test_catalog_verifier.py`,
`test_catalog_determinism.py`, and `test_catalog_service.py`). Tests use
`tmp_path` fixtures for storage, validation reports, integrity reports,
normalized artifacts, synchronized artifacts, cleaned artifacts,
transformed artifacts, QC artifacts, package artifacts, and the catalog
database, so they never touch the real `data/raw`, `data/validation`,
`data/integrity`, `data/normalized`, `data/synchronized`, `data/cleaned`,
`data/transformed`, `data/qc`, `data/packages`, or `data/catalog`
directories. Step 2/3/4/5 tests do read the real, built-in `schemas/`
directory (read-only) to exercise the actual `imu`/`gps` definitions
end-to-end. Step 9's optional Parquet tests are skipped cleanly
(`pytest.importorskip`) when `pyarrow` isn't installed. Step 10's
`test_catalog_lineage.py` independently verifies that scanning,
rebuilding, and recursive verification never modify a single byte across
any of the 9 upstream storage roots.

v2.1 adds 49 more tests (927 total) across `test_atomic_commit.py`,
`test_staging_invisibility.py`, `test_recovery_service.py`,
`test_crash_safety_fault_injection.py`,
`test_crash_safety_subprocess.py`, and `test_idempotency.py`, plus a
shared `crash_safety_helpers.py` of reusable invariant assertions
(`assert_no_partial_final_artifacts`, `assert_staging_not_discoverable`,
`assert_upstream_unchanged`, `assert_final_artifact_checksums_valid`)
used across several of those files.
`test_crash_safety_subprocess.py`'s two tests use real `SIGKILL` via
`multiprocessing`, synchronized deterministically with
`multiprocessing.Event` (never sleep-based polling) so they aren't
flaky.

v2.2 adds 13 more tests to the default suite (940 total) —
`test_disk_preflight.py`, sqlite-backend additions to
`test_cleaning_duplicates.py` and `test_cleaning_api.py`, and one
disk-preflight-rejection test in `test_packaging_api.py` — plus a
separate, **opt-in** `tests/load/` suite (11 tests, run via
`pytest -m load`, deselected by default) covering real memory
measurement at up to 1,000,000-row scale for ingestion, CSV validation,
cleaning-dedup backends, count-window transformation, and one combined
large-scale-plus-crash-injection scenario. See "Load test methodology"
above for why `load` tests are opt-in and how peak memory is measured.

v2.3 adds 88 more tests to the default suite (1028 total) under
`tests/sensors/` — plugin registration/duplicate/unknown-key/metadata
tests, a shared contract suite (`tests/sensors/contract.py`) run against
all three built-ins (IMU, GPS, Force/Torque), Force/Torque validation/
integrity/normalization/synchronization/cleaning/transformation/QC/
packaging/lineage/reliability tests, the sensor discovery API, and a
static-architecture test asserting zero `force_torque` string matches
across every generic-core module (synchronization, cleaning, QC,
packaging, catalog) — the direct, automated proof behind this
document's "Extension cost" claims. Plus 3 more **opt-in**
`tests/load/` tests (14 total, `pytest -m load`) proving Force/Torque
validation/normalization/transformation stay within v2.2's bounded-
memory contracts at 1,000,000-row scale.

v2.4 leaves the default suite at 1028 (no default-suite behavior
changed, only internal race-safety) and adds a separate, **opt-in**
`tests/concurrency/` suite (18 tests, run via `pytest -m concurrency`,
deselected by default exactly like `load`) under
`tests/concurrency/`: `test_connections_and_wal.py`,
`test_artifact_and_edge_races.py`, `test_dataset_version_races.py`,
`test_rebuild_lock.py`, `test_busy_timeout.py`,
`test_crash_during_contention.py`, and `test_stress.py`, plus a shared
`helpers.py` of real-multiprocess worker functions (every worker is a
plain, picklable, module-level function run inside its own
`multiprocessing.Process` under the `spawn` context, opening its own
SQLite connection — never a sequential-call simulation of concurrency).
See "Multiprocess concurrency model (v2.4)" above for what each test
proves and for the live `uvicorn --workers 4` + `curl` verification
that accompanied this suite.

v2.5 adds 38 more tests to the default suite (1066 total):
`test_v25_governance_model.py` (14 — absent-means-active, transitions,
reason requirement, append-only history, catalog-rebuild preservation,
broken-reference health check), `test_v25_gating_and_impact.py` (4 —
deprecated direct/ancestor blocking plus `allow_deprecated` override,
reactivation, enriched-impact sibling exclusion),
`test_v25_dataset_version_governance.py` (7), `test_v25_rebuild_planner.py`
(8 — topological order, selective reuse, compatibility checks, plan
fingerprint), `test_v25_rebuild_execution.py` (4 — stale plan, cascading
skip on missing config, unknown plan_id), `test_v25_flagship_scenario.py`
(1 — the full bad-IMU-normalization-to-corrected-v1.1.0 workflow end to
end through the real HTTP API), and `test_v25_crash_during_rebuild.py`
(1 — a real `app.storage.atomic.fault_injector`-forced crash partway
through a 5-step rebuild, proving the already-succeeded step stays
valid, the failed step leaves nothing partial, and a retry completes
cleanly). Plus 4 more **opt-in** `tests/concurrency/` tests (22 total)
in `test_v25_governance_and_rebuild_locks.py` — concurrent governance
updates on the same artifact never lose an event, and the per-root
selective-rebuild lock is a real cross-process OS lock. See "Data
governance and selective rebuild (v2.5)" above for the design each test
proves, and `tests/v25_helpers.py` for the shared real-pipeline-via-HTTP
builder these tests share.

## Deliberate MVP limitations

**Step 1:**
- No cloud storage backend yet (local filesystem only) — the `RawStorage`
  abstraction is designed so S3/GCS/Azure Blob backends can be added without
  touching ingestion logic.
- No streaming ingestion (e.g. Kafka) — only synchronous HTTP upload.
- No total-request-body limit enforced ahead of the multipart parser; large
  uploads are rejected mid-stream by `MAX_UPLOAD_SIZE_MB`, not before.
- No authentication/authorization on the upload endpoint.
- No database — all metadata lives in per-ingestion `manifest.json` files.
- `.zip` archive contents are stored as-is and are not inspected.
- IDs are UUID4, not sortable; `utils/ids.py` isolates this for an easy
  UUID7 swap later.

**Step 2:**
- `find_manifest(ingestion_id)` resolves an ingestion by scanning the
  filesystem (`glob`), since only `ingestion_id` is known at the API layer.
  This does not scale — a production system would maintain a lookup index
  (e.g. a database) instead.
- JSON arrays are parsed fully into memory (documented in
  `json_validator.py`); CSV and JSONL are processed incrementally and do
  not have this limitation.
- `.zip` contents are not inspected — validating a ZIP-backed ingestion
  always returns `415 Unsupported Media Type`.
- No nested-object validation — fields are flat, one level deep.
- `metadata_requirements` is checked only against the ingestion manifest's
  top-level fields (currently just `source_type` via the `sensor_type`
  key), and only when that field was actually populated at ingestion time.
- Only one validation report backend exists (`LocalValidationReportStore`);
  unlike `RawStorage`, it isn't split into an ABC + implementation yet,
  since there's no second backend to abstract for today.

**Step 3:**
- No cross-file consistency checks — every check operates on a single
  ingested file in isolation.
- No multimodal synchronization (e.g. aligning IMU and GPS streams by time).
- No gap interpolation and no automatic repairs of any kind — Step 3 only
  reports, it never fixes.
- No deduplication — a duplicate timestamp is flagged, not removed.
- No statistical anomaly detection or learned anomaly models — the extreme-
  value thresholds are fixed, documented constants (`ImuThresholds`), not
  learned from the data.
- No per-device or per-fleet adaptive thresholds — the same `GpsLimits` /
  `ImuThresholds` apply to every record of a given schema.
- `ValidationReportStore.find_reports()` (used for the lineage gate) is a
  filesystem scan, exactly like `RawStorage.find_manifest()` in Step 2 — no
  persistent database/index. Same limitation, same justification.
- JSON arrays are still parsed fully into memory (inherited from Step 2's
  `iter_records` equivalent in `app.integrity.records`); CSV and JSONL are
  streamed.
- `.zip` contents are not inspected — integrity checking a ZIP-backed
  ingestion always returns `415 Unsupported Media Type`.
- Only `imu` and `gps` have registered checkers; requesting integrity
  checks for any other schema returns `415` until a checker is added to
  `IntegrityCheckerRegistry`.

**Step 4:**
- No cross-file consistency checks and no multimodal synchronization —
  each normalization run operates on one ingested file in isolation.
- No gap interpolation, no automatic repairs, no deduplication, no
  resampling — normalization only re-represents values that already exist.
- Extreme-value/range semantics are Step 3's job, not Step 4's — a value
  that already passed integrity checks is trusted to be plausible;
  normalization only converts its units and representation.
- No coordinate-reference-system conversion for GPS beyond decimal-degree
  passthrough — no GIS dependency has been introduced for this MVP.
- Both `_find_matching_validation_report` and
  `_find_matching_integrity_report` in `NormalizationService` are small,
  intentionally-duplicated copies of the same filtering logic Step 3 uses
  internally (not extracted into a shared helper), per the instruction not
  to modify already-complete stages.
- JSON output is always a top-level array regardless of the source's
  original object/array shape — a documented simplification, not a bug.
  JSON arrays (source and output) are still loaded fully into memory; CSV
  and JSONL are streamed both directions.
- `.zip` contents are not inspected — normalizing a ZIP-backed ingestion
  always returns `415 Unsupported Media Type`.
- Only `imu_canonical` v`1.0.0` and `gps_canonical` v`1.0.0` are registered;
  requesting any other profile returns `404` until one is added to
  `NormalizationProfileRegistry`.
- Field aliasing cannot be exercised through the full lineage-gated HTTP
  pipeline against the built-in schemas (both set `allow_extra_fields:
  false`) — see "Field aliasing and Step 2's `allow_extra_fields: false`"
  above. It is fully implemented and tested directly against
  `RecordNormalizer`.
- No caching/deduplication of repeated normalization requests with
  identical `(raw_sha256, schema, profile, config_hash)` — every request
  creates a new `normalization_id`, even if byte-identical output already
  exists. The manifest carries everything a future cache layer would need
  (see "Configuration hash and transform version" above); implementing the
  reuse itself is left for later.
- Only one normalized-artifact backend exists (`LocalNormalizedArtifactStore`);
  like the report stores, it isn't split into an ABC + implementation pair
  yet, since there's no second backend to abstract for today.

**Step 5:**
- No automatic clock offset estimation and no automatic drift estimation —
  both come only from explicit `clock_corrections` request configuration.
- No image/frame interpolation and no signal smoothing — `linear` only
  ever interpolates numeric fields; everything else uses nearest-within-
  tolerance, and a discrete (`record_type != "tabular"`) stream can't use
  `linear` at all.
- No learned synchronization and no advanced event-based alignment —
  `nearest` and `linear` are the only two strategies, both fully
  deterministic.
- No cross-session synchronization by default — a mismatched `session_id`
  is always rejected; the architecture allows for a future explicit
  override, which does not exist yet.
- `fixed_rate` mode reads each participating stream **twice** (once to
  find its corrected time range, once to actually align) — `stream`
  reference mode does not have this limitation (single pass, fully
  streamed). A deliberate, documented tradeoff: the usable interval must
  be known before any target timestamp can be generated.
- JSON arrays (as a normalized source format) remain non-streaming,
  inherited unchanged from Step 4's own documented limitation.
- No distributed processing — a single synchronization run executes
  in-process, single-threaded.
- Local filesystem storage only, exactly like every other stage.
- Synchronization relies entirely on each stream's own canonical,
  already-timestamped records — it has no independent way to verify a
  timestamp is *semantically* correct, only that it's well-formed and
  monotonic (Step 2/3 already own semantic timestamp validation).
- Known interaction with Step 3 (not a Step 5 bug): Step 3's IMU
  extreme-value thresholds run against raw, pre-Step-4-conversion values —
  see "Known architectural limitation: Step 3 is unit-unaware" above.
- Only one synchronized-artifact backend exists
  (`LocalSynchronizationArtifactStore`); same ABC-without-a-second-
  implementation choice made throughout this project's storage layer.

**Step 6:**
- Exact duplicate detection only — no fuzzy/near-duplicate detection, and
  (per Step 5's own uniqueness guarantee) `DUPLICATE_ROW` is primarily a
  defensive rule in practice; see "Duplicate handling" above.
- The duplicate-detection hash set grows with the number of *unique* rows
  processed — memory is O(unique rows), not O(total rows), and there is no
  disk-backed dedupe for very large datasets in this MVP.
- No ML-based cleaning, no learned session-quality scoring, and no
  automated PII detection — every filtering/redaction decision comes from
  an explicit, named policy rule against an explicit, requester-supplied
  path or threshold, never inferred from data.
- No missing-value imputation, no noise smoothing, no resampling — a
  dropped row is dropped, a missing stream stays `null`; Step 6 never
  invents or repairs a value.
- No arbitrary customer scripting — customer-specific behavior is added by
  subclassing `CleaningPolicy` in Python and registering it, never via
  user-supplied expressions or `eval()`.
- JSONL synchronized input only for this MVP — Step 5 currently only ever
  produces JSONL anyway, so this isn't a practical restriction yet, but the
  check is explicit (`415`) rather than assumed.
- No distributed execution — a single cleaning run processes one
  synchronized artifact in-process, single-threaded, local filesystem only.
- Only one cleaned-artifact backend exists (`LocalCleanedArtifactStore`);
  same ABC-without-a-second-implementation choice made throughout this
  project's storage layer.

**Step 7:**
- Deterministic handcrafted features only — no learned embeddings, no
  model-based feature extraction of any kind.
- No FFT/spectral features (deliberately out of scope for this MVP; the
  architecture doesn't preclude adding one as another named derived
  feature later).
- No model-based labeling, and no accidental label generation — speed/GPS
  position/sensor-threshold values are never treated as labels; labeling
  belongs to a later, explicit labeling profile if one is ever added.
- No train/val/test splitting and no dataset-wide QC decisions — both are
  Step 8's job; Step 7 only reports per-window `modality_coverage`, exactly
  as Step 5/6 only ever report their own coverage/retention ratios.
- No Parquet/PyTorch/NumPy/TF/HF output yet — `transformed.jsonl` keeps
  nested JSON structure; flattening to columnar/tensor formats is Dataset
  Packaging's job (Step 9).
- No distributed processing — a single transformation run executes
  in-process, single-threaded, local filesystem only.
- Time windows depend entirely on already-canonical timestamps already
  present in cleaned rows — Step 7 has no independent way to verify a
  timestamp is *semantically* correct (Step 2/3 already own that), and it
  never resynchronizes or interpolates (Step 5's job).
- No automatic feature selection — every raw/statistic/derived feature
  must be named explicitly in the request config; an unrecognized name
  fails configuration validation rather than being silently ignored.
- Only one transformed-artifact backend exists
  (`LocalTransformedArtifactStore`); same ABC-without-a-second-
  implementation choice made throughout this project's storage layer.
- Only `multimodal_window_v1` v`1.0.0` is registered; requesting any other
  profile returns `404` until one is added to
  `TransformationProfileRegistry`.

**Step 8:**
- No learned anomaly detector of any kind — every check is an explicit,
  deterministic, configured threshold; nothing is inferred from the data
  itself.
- No automatic data repair, no automatic sample deletion, no imputation —
  QC reports problems, it never fixes them.
- No advanced statistical hypothesis tests (e.g. KS-test, chi-squared) —
  drift comparison is limited to a simple, fully deterministic
  standardized mean difference, documented explicitly as an MVP-scoped
  choice, not a statistically rigorous test.
- No automatic baseline selection, ever — a baseline QC report must be
  named explicitly via `baseline_qc_id`; "the previous run" is never
  inferred, for reproducibility.
- Percentile storage retains up to `MAX_QC_VALUES_PER_FEATURE` raw scalar
  values per feature (first-encountered order, never randomly sampled);
  beyond that cap, percentiles are marked truncated while mean/std/min/max
  stay exact. Documented memory behavior, not a silent limitation.
- One transformed artifact commonly maps to one synchronized session —
  session/group imbalance analysis is a no-op below two known groups
  rather than fabricating a multi-session breakdown from single-session
  lineage.
- No cross-dataset global catalog or database — each QC run stands alone,
  looked up by explicit `transformation_id`/`qc_id`, never an implicit
  "latest" or a cross-dataset index.
- No distributed processing — a single QC run executes in-process,
  single-threaded, local filesystem only.
- Scalar feature QC only — raw arrays, sample IDs, timestamps, and nested
  metadata are intentionally excluded from distribution analysis; only
  genuine numeric scalars (bool explicitly excluded) are analyzed.
- Only `default_dataset_qc` v`1.0.0` is registered; requesting any other
  profile returns `404` until one is added to `QCProfileRegistry`.

**Step 9:**
- Local filesystem only — no distributed packaging, no remote object-store
  export yet (S3/GCS/Azure Blob), same choice made throughout this
  project's storage layer.
- No dataset catalog/database — each package stands alone, looked up by
  explicit `transformation_id`/`package_id`, never an implicit "latest."
- No automatic semantic-version increment — `dataset_version` is stored
  verbatim if supplied and validated as basic SemVer; bumping it is a
  human/caller decision, not something Step 9 infers.
- No automatic "best split" optimization — hash-based splitting can and
  does deviate from requested ratios on small or group-imbalanced
  datasets; grouping (leakage prevention) always wins over hitting an
  exact ratio.
- Source-overlap grouping depends entirely on correct Step 7 provenance
  (`metadata.source_row_start`/`source_row_end`) — it has no independent
  way to verify those ranges are accurate.
- Session grouping is limited by currently-available metadata: it only
  works when a transformation's lineage has exactly one distinct
  `session_id` (see "Grouping abstraction" above) — multi-session
  attribution isn't fabricated.
- No stratified label-based splitting — Step 7 doesn't generate labels,
  so there's nothing to stratify on; splitting is purely group/hash-based.
- No model framework dependency of any kind — no PyTorch, no TensorFlow,
  no Hugging Face `datasets` library. Packages are framework-neutral
  JSONL (+ optional Parquet); a future loader can consume either.
- Parquet is genuinely optional (`pip install .[parquet]`) — requesting
  it without pyarrow installed returns a clear `415`, never a crash, and
  the base install works fully without it.
- No compression tuning beyond simple deterministic defaults — JSONL is
  plain text, Parquet uses pyarrow's own defaults.
- Accepted QC warnings are propagated (`source_qc_status`,
  `warning_count`) but never repaired — Step 9 packages what QC accepted,
  exactly as QC reported it.
- Packages are immutable once committed — no in-place split rebalancing,
  no adding a fourth split later; a changed requirement means a new
  `package_id`.
- Only `default_ml_package` v`1.0.0` is registered; requesting any other
  profile returns `404` until one is added to `PackagingProfileRegistry`.

**Step 10:**
- SQLite only, single-process — no Postgres/MySQL, no distributed
  metadata store, no concurrent-writer coordination beyond one
  connection per request.
- No authentication/authorization/RBAC of any kind — every catalog and
  dataset endpoint is open, exactly like every earlier stage.
- No automatic schema migrations beyond the current `CATALOG_SCHEMA_VERSION`
  (`"1.0.0"`) — a future schema change needs an explicit migration path;
  today a mismatch is only *detected* (`CATALOG_SCHEMA_MISMATCH`), never
  auto-migrated.
- No event-based auto-indexing or background watcher — the catalog only
  ever reflects the filesystem as of the last explicit `scan`/`rebuild`
  call; nothing indexes a new artifact automatically as it's written.
- No destructive lifecycle management — datasets/versions/artifacts are
  never deleted by the catalog itself, only ever created or read.
- No automatic SemVer increment and no pre-release/build-metadata
  support — `version` must be an explicit, caller-supplied strict
  `MAJOR.MINOR.PATCH`.
- No cryptographic signatures — `lineage_fingerprint` is a provenance
  digest proving content/config equivalence between runs, not a signed
  attestation of authorship or a tamper-proof seal.
- Git commit provenance is opportunistic only — `reproducibility.git_commit`
  is whatever (if anything) an earlier stage's manifest happened to
  record; Step 10 never shells out to git itself.
- No UI — the catalog and dataset registry are HTTP-only, exactly like
  every other stage in this project.
- Filesystem manifests remain the permanent source of truth; SQLite is
  never the sole record of lineage — this is enforced by `rebuild()`'s
  ability to fully reconstruct the artifact/edge tables from nothing but
  the 9 storage roots.

**v2.1:**
- No record-level resume — a failed or interrupted stage is safely
  rerunnable from the beginning, never resumed from the exact record
  where it stopped. This is a deliberate scope boundary, not an
  oversight.
- No PID-based liveness checking — stale-staging classification is
  purely time-based (`STALE_STAGING_AFTER_SECONDS`), since PIDs can be
  reused and there is no portable, reliable way to check whether a given
  PID still refers to the same process.
- No automatic recovery — `cleanup_stale()` must be called explicitly
  (or via `POST /api/v1/recovery/cleanup`); nothing runs it on a
  schedule or at startup in this milestone.
- Idempotency is infrastructure only (`app.storage.idempotency.
  execution_key`) — not wired into any live service, so two identical
  requests still intentionally produce two distinct artifacts, exactly
  as in v1.0.
- The catalog itself was not restructured — its existing SQLite
  transaction already provides crash safety for rebuild (see "Catalog
  rebuild crash behavior" above); this milestone did not introduce
  concurrent-writer coordination, WAL mode, or a temp-database-swap
  strategy.
- No cloud storage, distributed workers, authentication, multi-tenancy,
  a web dashboard, full pipeline orchestration, a CLI, or multiprocessing
  concurrency — all out of scope for this milestone, consistent with
  every prior step's local-first, single-process design.

**v2.2:**
- Not distributed, not cloud-scale — designed for large single-machine
  workloads, not arbitrarily large datasets. No Spark/Dask/Ray/Celery/
  Kafka/Kubernetes/multiprocessing production workers were introduced.
- Disk preflight is wired into ingestion and packaging only —
  normalization, synchronization, cleaning, and transformation don't yet
  call `require_disk_space()`, though the same helper is ready for them.
- No automatic memory→disk spillover for cleaning dedup — the backend is
  an explicit per-request choice (`memory` default, `sqlite` opt-in),
  not an auto-detected threshold.
- JSON array format remains O(dataset) for read and write (validation,
  integrity, normalization) — no streaming JSON parser dependency was
  added; use CSV/JSONL for large files.
- The optional Parquet exporter remains O(split size) — only reached
  when a request's `exports` includes `"parquet"`; the mandatory JSONL
  export is fully streamed.
- QC percentiles beyond `MAX_QC_VALUES_PER_FEATURE` are a
  first-encountered-order sample, not a statistically representative
  one — no bounded-memory quantile sketch was introduced; check
  `percentiles_truncated` before trusting them at scale.
- Runtime resource metrics (duration, throughput, peak RSS) are
  benchmark-only in v2.2 — no per-stage runtime telemetry was added to
  persisted manifests, specifically so a lineage fingerprint never
  depends on how fast the machine that produced it was.

**v2.3:**
- Discovery is static and explicit only — no filesystem plugin
  scanning, no importlib entry points, no dynamic third-party package
  installation, no plugin sandboxing. A future third-party plugin
  mechanism is a distinct, later concern (it has its own security
  surface: arbitrary code execution from a discovered module).
- `plugin_version` is not independently recorded in lineage for the
  three built-ins — their profile/checker bundle identity already
  covers implementation identity; see "Version semantics" above.
- Only tabular time-series sensors are addressed — camera/image
  decoding, video pipelines, ROS bag ingestion, PointCloud2/LiDAR
  processing remain out of scope; this milestone is about the
  extension architecture, not about supporting every physical-AI data
  modality.
- No dynamic sensor-type inference of any kind — a request must always
  name an explicit `schema_name`/`profile_name`/stream name; nothing is
  guessed from file content or column names.

## Status

All 10 planned steps are complete and fully tested. v2.1 (Crash Safety &
Atomic Artifacts) adds crash-safe, atomically-published artifacts and a
staging recovery service across every stage's storage layer. v2.2
(Large-scale Streaming & Resource Bounds) documents a resource contract
for every stage, adds a scalable sqlite-backed exact-dedup option for
cleaning, and adds disk-space preflight checks. v2.3 (Sensor / Schema
Plugin System) introduces a coherent `SensorPlugin` architecture,
migrates IMU and GPS onto it with zero behavior change, and adds a
third built-in Force/Torque sensor as proof — confirmed by a static test
that zero synchronization/cleaning/QC/packaging/catalog file mentions
"force_torque". v2.4 (Multiprocess Concurrency & SQLite Safety) makes
the catalog safe under multiple concurrent local processes sharing one
`catalog.db` — WAL journaling (verified, not assumed), a bounded busy
timeout with a structured `CatalogBusyError`, race-safe (DB-constraint-
authoritative) artifact/edge/dataset/version registration, and an
OS-level exclusive rebuild lock — verified with a real multiprocess test
suite and live `uvicorn --workers 4` + `curl` demos, including a real
`SIGKILL` mid-write and a subsequent clean `PRAGMA integrity_check`.
v2.5 (Data Governance & Selective Rebuild) turns lineage from passive
observability into active governance: artifacts and dataset versions can
be marked deprecated/invalid (append-only history, no manifest ever
touched), a downstream-processing gate blocks new work through an
invalid artifact or ancestor, enriched impact analysis computes each
affected dataset version's effective status, and a selective-rebuild
planner/executor produces a new lineage branch — reusing every
unaffected sibling parent unchanged — while the old branch and old
dataset version remain fully intact. Verified end-to-end with a
flagship bad-normalization-to-corrected-dataset-version scenario, a
real fault-injected crash mid-rebuild, and a live `uvicorn` + `curl`
demo covering the full workflow plus a genuine `SIGKILL` during a live
`/rebuild/execute` call.

1066 tests in the default suite, plus an opt-in `tests/load/` suite (14
tests, `pytest -m load`) exercising real memory measurement at up to
1,000,000-row scale, plus an opt-in `tests/concurrency/` suite (22
tests, `pytest -m concurrency`) exercising real multiprocess contention.
No Step 11 work has been started or planned as part of this build.
