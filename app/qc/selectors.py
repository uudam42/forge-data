"""Deterministic scalar-feature discovery.

Recursively traverses a transformed sample's `features` object and yields
`(dotted_path, value)` pairs for every leaf that is a genuine numeric
scalar or an explicit null — never for raw arrays, strings, sample IDs,
timestamps, or nested metadata objects.

`bool` is deliberately excluded even though Python's `bool` is a subclass
of `int` — `isinstance(True, int)` is `True`, so the bool check MUST come
before the numeric check below, or `True`/`False` would silently become
numeric feature observations.
"""

from __future__ import annotations

from typing import Iterator

_MISSING = object()  # sentinel: distinguishes "path absent" from "path present with None"


def iter_scalar_feature_paths(features: dict, *, prefix: str = "features") -> Iterator[tuple[str, object]]:
    for key in sorted(features.keys()):
        value = features[key]
        path = f"{prefix}.{key}"
        if isinstance(value, dict):
            yield from iter_scalar_feature_paths(value, prefix=path)
        elif isinstance(value, bool):
            continue
        elif isinstance(value, (int, float)):
            yield path, value
        elif value is None:
            yield path, None
        else:
            # list (raw arrays), str, or any other non-scalar shape.
            continue


def discover_scalar_feature_paths(features: dict) -> dict[str, object]:
    """Materializes iter_scalar_feature_paths into a dict for a single
    sample — a dict lookup (`path in paths`) correctly distinguishes
    "present with null value" from "path entirely absent", since a plain
    dict retains `None` values under their key."""
    return dict(iter_scalar_feature_paths(features))
