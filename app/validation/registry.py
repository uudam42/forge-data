"""Maps a file extension to its Validator implementation.

.zip is intentionally absent: Step 2 does not inspect archive contents.
ValidationService checks `supports()` and raises a clear, typed error for
anything unregistered (zip included) rather than silently doing nothing.
"""

from __future__ import annotations

from app.validation.validators.base import Validator
from app.validation.validators.csv_validator import CsvValidator
from app.validation.validators.json_validator import JsonValidator
from app.validation.validators.jsonl_validator import JsonlValidator


class UnsupportedValidationTypeError(Exception):
    pass


class ValidatorRegistry:
    def __init__(self) -> None:
        self._validators: dict[str, Validator] = {
            ".csv": CsvValidator(),
            ".json": JsonValidator(),
            ".jsonl": JsonlValidator(),
        }

    def supports(self, extension: str) -> bool:
        return extension.lower() in self._validators

    def get(self, extension: str) -> Validator:
        validator = self._validators.get(extension.lower())
        if validator is None:
            raise UnsupportedValidationTypeError(
                f"No validator registered for extension '{extension}'"
            )
        return validator
