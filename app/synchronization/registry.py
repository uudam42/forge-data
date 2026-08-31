"""Maps an alignment method name to its AlignmentStrategy implementation,
and guards discrete (non-"tabular") schemas against unsupported methods.

Discrete-stream detection reuses SchemaDefinition.record_type — already
present on every schema for exactly this purpose (Step 2 defines it,
unused until now) — rather than inventing a new field. A future camera/
frame-reference schema declaring `"record_type": "discrete"` (or anything
other than "tabular") would be rejected for "linear" here without any
change to the synchronization core, satisfying the requirement that
discrete streams support nearest but not linear.
"""

from __future__ import annotations

from app.synchronization.strategies.base import AlignmentStrategy
from app.synchronization.strategies.linear import LinearInterpolationStrategy
from app.synchronization.strategies.nearest import NearestAlignmentStrategy
from app.validation.schemas.base import SchemaDefinition


class UnsupportedAlignmentMethodError(Exception):
    pass


class AlignmentStrategyRegistry:
    def __init__(self) -> None:
        self._strategies: dict[str, AlignmentStrategy] = {
            "nearest": NearestAlignmentStrategy(),
            "linear": LinearInterpolationStrategy(),
        }

    def supports(self, method: str) -> bool:
        return method in self._strategies

    def get(self, method: str, *, schema: SchemaDefinition) -> AlignmentStrategy:
        strategy = self._strategies.get(method)
        if strategy is None:
            raise UnsupportedAlignmentMethodError(
                f"Unknown alignment method '{method}'; supported: {sorted(self._strategies)}"
            )
        if not strategy.supports_discrete_streams and schema.record_type != "tabular":
            raise UnsupportedAlignmentMethodError(
                f"Alignment method '{method}' is not supported for discrete schema "
                f"'{schema.schema_name}' (record_type='{schema.record_type}') — only 'nearest' "
                "is valid for non-tabular streams"
            )
        return strategy
