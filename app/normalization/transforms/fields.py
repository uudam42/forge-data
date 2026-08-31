"""Deterministic field-name alias resolution.

Aliases are explicit, declared per-profile — never inferred via fuzzy
matching. If two distinct input field names resolve to the same canonical
field within one record, that is a genuine ambiguity the profile author
did not intend, and normalization must fail loudly rather than silently
pick one.
"""

from __future__ import annotations


class AmbiguousFieldMappingError(Exception):
    pass


def resolve_field_names(record: dict, aliases: dict[str, str]) -> dict:
    """Returns a new dict with alias keys replaced by canonical field names.

    A field absent from `aliases` passes through under its original name
    unchanged (already-canonical or genuinely unrecognized fields alike —
    the caller decides what to do with anything not in the target schema).
    """
    resolved: dict[str, object] = {}
    origin_by_canonical: dict[str, str] = {}

    for raw_name, value in record.items():
        canonical_name = aliases.get(raw_name, raw_name)
        if canonical_name in resolved:
            raise AmbiguousFieldMappingError(
                f"Both '{origin_by_canonical[canonical_name]}' and '{raw_name}' map to "
                f"canonical field '{canonical_name}'"
            )
        resolved[canonical_name] = value
        origin_by_canonical[canonical_name] = raw_name

    return resolved
