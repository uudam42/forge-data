"""Shared cleaning-rule utilities: the one canonical JSON serialization
convention, and dot-path field access for redaction.

Dot paths (e.g. "streams.gps.latitude") are the only mechanism for
addressing nested fields — no expression language, no eval(), no dynamic
code execution.
"""

from __future__ import annotations

import copy
import json


def canonical_json(obj: object) -> str:
    """The one deterministic serialization convention used throughout Step 6.

    sort_keys means dict-insertion-order differences (e.g. between two
    otherwise-identical rows built by different code paths) can never
    change a serialized row's bytes, and therefore never change an
    artifact's checksum.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def is_valid_field_path(path: str) -> bool:
    """Structural validity only — not whether the path exists in any given
    row. Rejects empty strings and paths with empty segments
    (""," .foo", "foo.", "foo..bar") — a syntactically nonsensical
    redaction path is a configuration error (INVALID_REDACTION_PATH, 400),
    not a per-row concern.
    """
    if not isinstance(path, str) or not path:
        return False
    return all(part != "" for part in path.split("."))


def path_exists(row: dict, path: str) -> bool:
    node = row
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def apply_redactions(row: dict, fields: list[str]) -> dict:
    """Returns a NEW dict with every given dot-path set to null.

    Never mutates `row` — a single deepcopy up front, then in-place
    mutation of the copy only. A field absent from this particular row is
    silently skipped (the caller filters via path_exists first) rather
    than treated as an error — an optional field that simply wasn't
    present isn't a redaction failure.
    """
    if not fields:
        return row

    result = copy.deepcopy(row)
    for path in fields:
        parts = path.split(".")
        node = result
        reachable = True
        for part in parts[:-1]:
            if not isinstance(node, dict) or part not in node:
                reachable = False
                break
            node = node[part]
        if reachable and isinstance(node, dict) and parts[-1] in node:
            node[parts[-1]] = None
    return result
