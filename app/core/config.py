"""Application configuration.

All environment-tunable values live here so the rest of the codebase never
reads os.environ directly and never hardcodes storage paths.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", env_file=".env", extra="ignore")

    # Root directory for immutable raw storage. Relative paths are resolved
    # against the current working directory at startup.
    RAW_STORAGE_ROOT: Path = Path("data/raw")

    # Maximum accepted upload size, in megabytes.
    MAX_UPLOAD_SIZE_MB: int = 512

    # Extensions accepted by the ingestion endpoint (lowercase, with dot).
    ALLOWED_EXTENSIONS: tuple[str, ...] = (".csv", ".json", ".jsonl", ".zip")

    # Directory of schema-definition JSON files (Step 2).
    SCHEMA_DIR: Path = Path("schemas")

    # Root directory for validation reports, kept separate from raw storage.
    VALIDATION_STORAGE_ROOT: Path = Path("data/validation")

    # Cap on the number of detailed error objects a validation report stores.
    MAX_VALIDATION_ERRORS: int = 1000

    # Root directory for integrity reports (Step 3), kept separate from both
    # raw storage and validation reports.
    INTEGRITY_STORAGE_ROOT: Path = Path("data/integrity")

    # Cap on the number of detailed issue objects an integrity report stores.
    MAX_INTEGRITY_ISSUES: int = 1000

    # Root directory for normalized artifacts (Step 4), kept separate from
    # raw storage and every report store.
    NORMALIZED_STORAGE_ROOT: Path = Path("data/normalized")

    # Root directory for synchronized artifacts (Step 5).
    SYNCHRONIZED_STORAGE_ROOT: Path = Path("data/synchronized")

    # Upper bound on fixed_rate synchronization frequency, to prevent an
    # accidentally huge generated timeline (e.g. a typo'd 10_000 Hz request).
    MAX_SYNC_FREQUENCY_HZ: float = 1000.0

    # Fallback alignment tolerance when a request doesn't specify one.
    DEFAULT_SYNC_TOLERANCE_MS: float = 100.0

    # Root directory for cleaned artifacts (Step 6).
    CLEANED_STORAGE_ROOT: Path = Path("data/cleaned")

    # Cap on the number of detailed dropped/redacted row examples a
    # cleaning report stores (independently for each list).
    MAX_CLEANING_ISSUE_DETAILS: int = 1000

    # Root directory for transformed artifacts (Step 7).
    TRANSFORMED_STORAGE_ROOT: Path = Path("data/transformed")

    # Upper bound on count-based window size, to prevent an accidentally huge
    # in-memory buffer (e.g. a typo'd size=1_000_000 request).
    MAX_WINDOW_SIZE: int = 100_000

    # Upper bound on time-based window duration, in milliseconds.
    MAX_TIME_WINDOW_MS: float = 3_600_000.0

    # Root directory for QC artifacts (Step 8).
    QC_STORAGE_ROOT: Path = Path("data/qc")

    # Cap on the number of detailed issue objects a QC report stores.
    MAX_QC_ISSUE_DETAILS: int = 1000

    # Cap on the number of raw scalar values retained per feature for exact
    # percentile computation. Mean/std/min/max stay exact (streaming);
    # beyond this cap, percentiles are marked "percentiles_truncated".
    MAX_QC_VALUES_PER_FEATURE: int = 100_000

    # Root directory for dataset package artifacts (Step 9).
    PACKAGE_STORAGE_ROOT: Path = Path("data/packages")

    # SQLite metadata catalog (Step 10) — an INDEX over the manifests
    # above, never their source of truth. Deleting this file and running
    # a rebuild must fully restore catalog state from the filesystem.
    CATALOG_DB_PATH: Path = Path("data/catalog/catalog.db")

    # Crash safety / atomic artifact commit (v2.1). See
    # docs/DETAILED_GUIDE.md#crash-consistency-and-atomic-artifacts.
    STAGING_DIR_NAME: str = ".staging"

    # A staging entry with no observed activity older than this is
    # classified STALE by the recovery scanner rather than ACTIVE.
    # Conservative default: long enough that a real in-flight request
    # (even a large upload) is never mistaken for an abandoned one.
    STALE_STAGING_AFTER_SECONDS: float = 3600.0

    # fsync staged files, the staging directory, and the destination's
    # parent directory before/after atomic rename. Disabling this keeps
    # atomic visibility (the rename itself is still atomic) but drops the
    # best-effort durability guarantee -- useful only for test speed.
    FSYNC_ENABLED: bool = True

    # Large-scale streaming & resource bounds (v2.2). See
    # docs/DETAILED_GUIDE.md#large-data-execution-and-resource-model.

    # Chunk size for every streamed byte-level read (ingestion upload,
    # future chunked writers). One conservative default, not per-stage
    # knobs -- see STREAM_CHUNK_BYTES docs for why 1 MiB.
    STREAM_CHUNK_BYTES: int = 1024 * 1024

    # Disk-space preflight (packaging, ingestion): refuse to start an
    # expensive write when free space is obviously insufficient, rather
    # than failing partway through. Conservative defaults deliberately
    # small so a normal dev laptop or CI runner never trips them on a
    # tiny test fixture.
    DISK_RESERVE_BYTES: int = 100 * 1024 * 1024  # 100 MiB headroom kept free beyond the estimate
    DISK_SAFETY_FACTOR: float = 1.2  # multiply the size estimate by this before comparing
    MIN_FREE_DISK_BYTES: int = 50 * 1024 * 1024  # absolute floor, independent of any estimate

    APP_NAME: str = "ai-data-pipeline"
    LOG_LEVEL: str = "INFO"

    @property
    def max_upload_size_bytes(self) -> int:
        return self.MAX_UPLOAD_SIZE_MB * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()
