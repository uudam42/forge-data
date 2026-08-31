"""Maps (policy_name, policy_version) to a CleaningPolicy.

Lookup is always explicit — never an implicit "latest" — mirroring every
other registry in this project (schemas, normalization profiles,
alignment strategies).
"""

from __future__ import annotations

from app.cleaning.policies.base import CleaningPolicy
from app.cleaning.policies.default import DEFAULT_MULTIMODAL_V1

_BUILTIN_POLICIES = (DEFAULT_MULTIMODAL_V1,)


class CleaningPolicyNotFoundError(Exception):
    pass


class CleaningPolicyRegistry:
    def __init__(self) -> None:
        self._policies: dict[tuple[str, str], CleaningPolicy] = {
            (p.policy_name, p.policy_version): p for p in _BUILTIN_POLICIES
        }

    def get(self, policy_name: str, policy_version: str) -> CleaningPolicy:
        policy = self._policies.get((policy_name, policy_version))
        if policy is None:
            raise CleaningPolicyNotFoundError(
                f"No cleaning policy '{policy_name}' v{policy_version} registered"
            )
        return policy

    def list_policies(self) -> list[tuple[str, str]]:
        return sorted(self._policies.keys())
