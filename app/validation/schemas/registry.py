"""Loads and looks up schema definitions from JSON files on disk.

All filesystem access for schemas is isolated here — the rest of the
validation engine only ever deals with SchemaDefinition objects, never with
schema file paths.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.validation.schemas.base import SchemaDefinition


class SchemaRegistryError(Exception):
    """Base class for schema registry failures."""


class SchemaNotFoundError(SchemaRegistryError):
    pass


class DuplicateSchemaError(SchemaRegistryError):
    pass


class SchemaRegistry:
    def __init__(self, schema_dir: Path) -> None:
        self._schema_dir = Path(schema_dir)
        self._schemas: dict[tuple[str, str], SchemaDefinition] = {}
        self._load()

    def _load(self) -> None:
        if not self._schema_dir.exists():
            return

        for path in sorted(self._schema_dir.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            schema = SchemaDefinition.model_validate(data)
            key = (schema.schema_name, schema.schema_version)
            if key in self._schemas:
                raise DuplicateSchemaError(
                    f"Duplicate schema definition for "
                    f"'{schema.schema_name}' v{schema.schema_version}: {path}"
                )
            self._schemas[key] = schema

    def get(self, *, schema_name: str, schema_version: str) -> SchemaDefinition:
        schema = self._schemas.get((schema_name, schema_version))
        if schema is None:
            raise SchemaNotFoundError(f"Schema not found: '{schema_name}' v{schema_version}")
        return schema

    def list_schemas(self) -> list[tuple[str, str]]:
        return sorted(self._schemas.keys())
