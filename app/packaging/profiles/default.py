"""Built-in packaging profile: default_ml_package.

The only profile shipped in the Step 9 MVP — additional profiles are a
future extension point, not something the API route or service ever
branch on directly.
"""

from __future__ import annotations

from app.packaging.profiles.base import PackagingProfile


class DefaultMlPackageProfile(PackagingProfile):
    profile_name = "default_ml_package"
    profile_version = "1.0.0"
    supported_split_strategies = ("group_hash", "sequential")
    supported_grouping_modes = ("source_overlap", "session")
    supported_export_formats = ("jsonl", "parquet")


DEFAULT_ML_PACKAGE = DefaultMlPackageProfile()
