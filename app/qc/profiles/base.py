"""The QC profile contract.

A profile is looked up explicitly by (profile_name, profile_version) —
never an implicit "latest" — mirroring every other registry in this
project. It decides which checks run and validates the effective request
config; there is no arbitrary Python execution or user-supplied expression
evaluation of any kind.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.qc.checks.base import QCCheck
from app.qc.models import QC_ENGINE_VERSION, QCConfig
from app.qc.serialization import compute_qc_config_hash


class InvalidQCConfigurationError(Exception):
    pass


class QCProfile(ABC):
    profile_name: str
    profile_version: str
    qc_engine_version: str = QC_ENGINE_VERSION

    @abstractmethod
    def validate_config(self, config: QCConfig) -> None:
        """Raises InvalidQCConfigurationError for a nonsensical effective
        configuration. Called once up front, before any sample is read."""
        raise NotImplementedError

    @abstractmethod
    def build_checks(self, config: QCConfig) -> list[QCCheck]:
        """Returns the ordered list of dataset-level checks this profile
        applies. Group-imbalance and drift checks are evaluated separately
        by the service, since they need lineage/storage access that plain
        QCCheck implementations deliberately don't have."""
        raise NotImplementedError

    def config_hash(self, config: QCConfig) -> str:
        payload = {
            "profile_name": self.profile_name,
            "profile_version": self.profile_version,
            "qc_engine_version": self.qc_engine_version,
            "config": config.model_dump(mode="json"),
        }
        return compute_qc_config_hash(payload)
