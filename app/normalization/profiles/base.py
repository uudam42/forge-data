"""The normalization profile contract and the generic engine that applies
any profile to any record.

A profile is pure declarative data (field aliases, which canonical fields
need unit conversion and to which dimension, the unit dimensions
themselves). RecordNormalizer is the one engine that interprets any profile
against any record — imu.py / gps.py declare profiles; they do not
implement any transformation logic themselves.

Canonical field order and required/nullable semantics are read directly
from the target SchemaDefinition rather than redeclared here — for both
built-in profiles the canonical field set is exactly the schema's field
set, so there's nothing to duplicate.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from app.normalization.transforms.common import is_finite, to_bool, to_float
from app.normalization.transforms.fields import AmbiguousFieldMappingError, resolve_field_names
from app.normalization.transforms.timestamps import normalize_timestamp
from app.normalization.transforms.units import UnitDimension
from app.validation.schemas.base import FieldType, SchemaDefinition

__all__ = [
    "AmbiguousFieldMappingError",
    "MissingUnitMetadataError",
    "UnsupportedSourceUnitError",
    "NormalizationConversionError",
    "NormalizationProfile",
    "RecordNormalizer",
]


class MissingUnitMetadataError(Exception):
    pass


class UnsupportedSourceUnitError(Exception):
    pass


class NormalizationConversionError(Exception):
    pass


@dataclass(frozen=True)
class NormalizationProfile:
    schema_name: str
    schema_version: str
    profile_name: str
    profile_version: str
    transform_version: str
    # raw input field name -> canonical field name (explicit only, no fuzzy matching)
    field_aliases: dict[str, str] = field(default_factory=dict)
    # canonical field name -> dimension name, only for fields needing unit conversion
    field_dimensions: dict[str, str] = field(default_factory=dict)
    dimensions: dict[str, UnitDimension] = field(default_factory=dict)
    timestamp_field: str = "timestamp"
    # Bumped whenever the *policy* changes, independent of profile_version,
    # so it can be included in the config hash without redefining it inline.
    timestamp_policy_version: str = "utc_iso8601_z_preserve_subsecond_v1"

    def config_hash(self, source_units: dict[str, str]) -> str:
        """Deterministic hash of the effective normalization configuration.

        Serialized via sort_keys + compact separators (never Python repr())
        so the same logical config always hashes identically regardless of
        dict insertion order.
        """
        payload = {
            "profile_name": self.profile_name,
            "profile_version": self.profile_version,
            "transform_version": self.transform_version,
            "field_aliases": self.field_aliases,
            "field_dimensions": self.field_dimensions,
            "dimensions": {
                name: {"canonical_unit": dim.canonical_unit, "factors": dim.factors}
                for name, dim in self.dimensions.items()
            },
            "timestamp_field": self.timestamp_field,
            "timestamp_policy_version": self.timestamp_policy_version,
            "source_units": source_units,
        }
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class RecordNormalizer:
    """Applies one NormalizationProfile (against one SchemaDefinition) to records.

    Resolves unit-conversion factors once at construction time — failing
    fast if the profile needs a source unit the caller never configured, or
    configured an unsupported one — rather than discovering it partway
    through a large file.
    """

    def __init__(
        self, *, schema: SchemaDefinition, profile: NormalizationProfile, source_units: dict[str, str]
    ) -> None:
        self._schema = schema
        self._profile = profile
        self._canonical_fields = list(schema.fields.keys())
        self._factor_by_field: dict[str, float] = {}

        for canonical_field, dimension_name in profile.field_dimensions.items():
            dimension = profile.dimensions[dimension_name]
            source_unit = source_units.get(dimension_name)
            if source_unit is None:
                raise MissingUnitMetadataError(
                    f"Missing source unit for dimension '{dimension_name}' "
                    f"(required to normalize field '{canonical_field}')"
                )
            if source_unit not in dimension.factors:
                raise UnsupportedSourceUnitError(
                    f"Unsupported unit '{source_unit}' for dimension '{dimension_name}'; "
                    f"supported units: {sorted(dimension.factors)}"
                )
            self._factor_by_field[canonical_field] = dimension.factors[source_unit]

    @property
    def canonical_fields(self) -> list[str]:
        return self._canonical_fields

    def normalize_record(self, record_number: int, raw_record: dict) -> dict:
        try:
            mapped = resolve_field_names(raw_record, self._profile.field_aliases)
        except AmbiguousFieldMappingError as exc:
            raise AmbiguousFieldMappingError(f"Record {record_number}: {exc}") from exc

        output: dict[str, object] = {}
        for field_name in self._canonical_fields:
            field_def = self._schema.fields[field_name]
            raw_value = mapped.get(field_name)
            is_null = raw_value is None or (isinstance(raw_value, str) and raw_value.strip() == "")

            if is_null:
                if field_def.required:
                    # Should be unreachable: the lineage gate already requires
                    # passing schema validation, which guarantees required
                    # fields are present and non-null. Fail loudly rather
                    # than silently emit an impossible null if it somehow
                    # isn't.
                    raise NormalizationConversionError(
                        f"Record {record_number}: required field '{field_name}' is missing or "
                        "null (expected schema validation to have already caught this)"
                    )
                output[field_name] = None
                continue

            if field_name == self._profile.timestamp_field:
                output[field_name] = self._normalize_timestamp_field(
                    record_number, field_name, raw_value
                )
                continue

            if field_name in self._factor_by_field:
                output[field_name] = self._convert_numeric(record_number, field_name, raw_value)
                continue

            output[field_name] = self._cast_plain_value(record_number, field_def, field_name, raw_value)

        return output

    def _normalize_timestamp_field(self, record_number: int, field_name: str, raw_value: object) -> str:
        if not isinstance(raw_value, str):
            raise NormalizationConversionError(
                f"Record {record_number}: timestamp field '{field_name}' value {raw_value!r} "
                "is not a string"
            )
        try:
            return normalize_timestamp(raw_value)
        except (ValueError, TypeError) as exc:
            raise NormalizationConversionError(
                f"Record {record_number}: could not normalize timestamp field '{field_name}' "
                f"value {raw_value!r}: {exc}"
            ) from exc

    def _convert_numeric(self, record_number: int, field_name: str, raw_value: object) -> float:
        value = to_float(raw_value)
        if value is None:
            raise NormalizationConversionError(
                f"Record {record_number}: field '{field_name}' value {raw_value!r} is not numeric"
            )
        if not is_finite(value):
            raise NormalizationConversionError(
                f"Record {record_number}: field '{field_name}' value {raw_value!r} is not finite"
            )
        factor = self._factor_by_field[field_name]
        return value * factor

    def _cast_plain_value(self, record_number: int, field_def, field_name: str, raw_value: object):
        if field_def.type is FieldType.FLOAT:
            value = to_float(raw_value)
            if value is None:
                raise NormalizationConversionError(
                    f"Record {record_number}: field '{field_name}' value {raw_value!r} is not numeric"
                )
            if not is_finite(value):
                raise NormalizationConversionError(
                    f"Record {record_number}: field '{field_name}' value {raw_value!r} is not finite"
                )
            return value

        if field_def.type is FieldType.INTEGER:
            value = to_float(raw_value)
            if value is None or not value.is_integer():
                raise NormalizationConversionError(
                    f"Record {record_number}: field '{field_name}' value {raw_value!r} is not an integer"
                )
            return int(value)

        if field_def.type is FieldType.BOOLEAN:
            value = to_bool(raw_value)
            if value is None:
                raise NormalizationConversionError(
                    f"Record {record_number}: field '{field_name}' value {raw_value!r} is not a "
                    "recognized boolean ('true'/'false' only)"
                )
            return value

        # STRING (and anything else passthrough) — already validated by Step 2.
        return raw_value
