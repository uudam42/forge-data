"""The cleaning policy contract.

A policy is looked up explicitly by (policy_name, policy_version) — never
an implicit "latest", mirroring every other registry in this project
(schemas, normalization profiles, alignment strategies). It decides WHICH
rules run and in WHAT order, given a request's CleaningConfig; the config
only ever parameterizes rules the policy already knows how to build —
there is no dynamic code execution or arbitrary expression evaluation of
any kind (no eval()).

This is the extension point for customer-specific policies (e.g. a future
WarehouseRobotCleaningPolicy) — they subclass CleaningPolicy and override
build_rules(), without CleaningService ever branching on customer
identity itself.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod

from app.cleaning.models import CleaningConfig
from app.cleaning.rules.base import CleaningRule


class CleaningPolicy(ABC):
    policy_name: str
    policy_version: str
    transform_version: str

    @abstractmethod
    def build_rules(self, config: CleaningConfig, *, known_streams: list[str]) -> list[CleaningRule]:
        """Returns the ordered list of rules this policy applies for the
        given effective configuration. Order matters — see
        policies/default.py for why privacy redaction must run last.
        """
        raise NotImplementedError

    def config_hash(self, config: CleaningConfig) -> str:
        """Deterministic hash of the effective cleaning configuration.

        Serialized via sort_keys + compact separators — never Python's
        repr() — so the same logical config always hashes identically
        regardless of dict insertion order.
        """
        payload = {
            "policy_name": self.policy_name,
            "policy_version": self.policy_version,
            "transform_version": self.transform_version,
            "config": config.model_dump(mode="json"),
        }
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
