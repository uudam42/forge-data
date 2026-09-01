"""The built-in default_multimodal cleaning policy.

Fixed, deterministic rule order — not configurable per-request, only
parameterized by it:

    1. required streams          (MISSING_REQUIRED_STREAM)
    2. minimum present streams   (INSUFFICIENT_MODALITY_COVERAGE)
    3. all-optional-missing      (ALL_OPTIONAL_STREAMS_MISSING)
    4. duplicate detection       (DUPLICATE_ROW)
    5. privacy redaction         (FIELD_REDACTED)

Privacy redaction is deliberately last: redacting a distinguishing field
before duplicate detection could make two genuinely different rows
collide and be misreported as duplicates. See RowEvaluator for the
short-circuit-on-drop behavior that this order depends on.
"""

from __future__ import annotations

from pathlib import Path

from app.cleaning.models import CleaningConfig
from app.cleaning.policies.base import CleaningPolicy
from app.cleaning.rules.base import CleaningRule
from app.cleaning.rules.coverage import AllOptionalMissingRule, MinPresentStreamsRule, RequiredStreamsRule
from app.cleaning.rules.duplicates import DuplicateRowRule
from app.cleaning.rules.privacy import PrivacyRedactionRule


class DefaultMultimodalPolicy(CleaningPolicy):
    policy_name = "default_multimodal"
    policy_version = "1.0.0"
    transform_version = "1.0.0"

    def build_rules(
        self, config: CleaningConfig, *, known_streams: list[str], temp_dir: Path | None = None
    ) -> list[CleaningRule]:
        rules: list[CleaningRule] = []

        if config.required_streams:
            rules.append(RequiredStreamsRule(required_streams=tuple(config.required_streams)))

        if config.min_present_streams is not None:
            rules.append(
                MinPresentStreamsRule(
                    min_present_streams=config.min_present_streams,
                    known_streams=tuple(known_streams),
                )
            )

        if config.drop_if_all_optional_streams_missing:
            optional_streams = tuple(s for s in known_streams if s not in config.required_streams)
            rules.append(
                AllOptionalMissingRule(
                    required_streams=tuple(config.required_streams),
                    optional_streams=optional_streams,
                )
            )

        if config.duplicate_policy.enabled:
            rules.append(DuplicateRowRule(backend=config.duplicate_policy.backend, temp_dir=temp_dir))

        if config.privacy.redact_fields:
            rules.append(PrivacyRedactionRule(fields=tuple(config.privacy.redact_fields)))

        return rules


DEFAULT_MULTIMODAL_V1 = DefaultMultimodalPolicy()
