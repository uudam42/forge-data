"""Deterministic, factor-based unit conversion.

Every conversion this MVP supports (g -> m/s^2, deg/s -> rad/s, ft -> m,
km/h -> m/s, mph -> m/s) is a simple linear scale, so one UnitDimension
mechanism covers all of them: multiply by a fixed factor to reach the
canonical unit. Units are never inferred from data — the source unit must
always be supplied explicitly by the caller (request config), never guessed
from numeric magnitude.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Standard gravity (the conventional fixed constant, not local/measured
# gravity) — this is the value this pipeline uses for g -> m/s^2.
STANDARD_GRAVITY_MPS2 = 9.80665

DEG_TO_RAD = math.pi / 180.0
FEET_TO_METERS = 0.3048
KMH_TO_MPS = 1000.0 / 3600.0
MPH_TO_MPS = 0.44704

# Exact (defined) conversion factor: 1 lbf = 1 lbm * standard gravity.
LBF_TO_NEWTONS = 4.4482216152605
LBF_FT_TO_NEWTON_METERS = LBF_TO_NEWTONS * FEET_TO_METERS


@dataclass(frozen=True)
class UnitDimension:
    """A convertible quantity (e.g. "acceleration") with a canonical unit
    and a fixed set of supported source units, each a linear factor to
    reach the canonical unit.
    """

    name: str
    canonical_unit: str
    factors: dict[str, float]  # source_unit -> multiplier to reach canonical_unit

    def convert(self, value: float, source_unit: str) -> float:
        return value * self.factors[source_unit]


ACCELERATION = UnitDimension(
    name="acceleration",
    canonical_unit="m/s^2",
    factors={"m/s^2": 1.0, "g": STANDARD_GRAVITY_MPS2},
)

ANGULAR_VELOCITY = UnitDimension(
    name="angular_velocity",
    canonical_unit="rad/s",
    factors={"rad/s": 1.0, "deg/s": DEG_TO_RAD},
)

ALTITUDE = UnitDimension(
    name="altitude",
    canonical_unit="m",
    factors={"m": 1.0, "ft": FEET_TO_METERS},
)

SPEED = UnitDimension(
    name="speed",
    canonical_unit="m/s",
    factors={"m/s": 1.0, "km/h": KMH_TO_MPS, "mph": MPH_TO_MPS},
)

FORCE = UnitDimension(
    name="force",
    canonical_unit="N",
    factors={"N": 1.0, "kN": 1000.0, "lbf": LBF_TO_NEWTONS},
)

TORQUE = UnitDimension(
    name="torque",
    canonical_unit="N·m",
    factors={"N*m": 1.0, "N·m": 1.0, "mN*m": 0.001, "mN·m": 0.001, "lbf*ft": LBF_FT_TO_NEWTON_METERS, "lbf·ft": LBF_FT_TO_NEWTON_METERS},
)
