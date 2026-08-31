"""JSON-based schema definition format owned by this application.

Deliberately small: five field types, a flat field map, no nested-object
validation. Extend FieldType / SchemaDefinition here as new needs arise
(e.g. nested camera metadata) rather than adopting a generic schema
framework prematurely.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class FieldType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    DATETIME = "datetime"


class FieldDefinition(BaseModel):
    type: FieldType
    required: bool = False
    nullable: bool = True
    # Informational for the MVP (e.g. "iso8601"); datetime fields are always
    # validated as ISO-8601-with-timezone regardless of this value.
    format: str | None = None


class SchemaDefinition(BaseModel):
    schema_name: str
    schema_version: str
    record_type: str = "tabular"
    fields: dict[str, FieldDefinition]
    allow_extra_fields: bool = False
    # e.g. {"sensor_type": "imu"} — checked against the ingestion manifest.
    # See ValidationService for exactly how this is applied.
    metadata_requirements: dict[str, str] = Field(default_factory=dict)
