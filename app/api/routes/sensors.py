"""HTTP layer for sensor plugin discovery (v2.3).

Read-only — lets a caller inspect which sensor plugins are built in
without reading source code. Returns metadata only, never an
implementation object.

Status codes:
- 200: the plugin (or plugin list) was found
- 404: the requested sensor_type has no registered plugin
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.sensors.base import SensorPlugin, SensorPluginNotFoundError
from app.sensors.models import SensorPluginSummary
from app.sensors.registry import get_default_registry

router = APIRouter(prefix="/api/v1/sensors", tags=["sensors"])


def _to_summary(plugin: SensorPlugin) -> SensorPluginSummary:
    return SensorPluginSummary(
        sensor_type=plugin.sensor_type,
        plugin_version=plugin.plugin_version,
        display_name=plugin.display_name,
        schema_name=plugin.normalization_profile.schema_name,
        schema_version=plugin.schema_version,
        normalization_profile=plugin.normalization_profile.profile_name,
        normalization_profile_version=plugin.normalization_profile.profile_version,
        timestamp_field=plugin.timestamp_field,
        numeric_fields=list(plugin.numeric_fields),
        required_fields=list(plugin.required_fields),
        canonical_units=plugin.canonical_units,
        has_feature_extractor=plugin.feature_extractor is not None,
    )


@router.get("", response_model=list[SensorPluginSummary], status_code=status.HTTP_200_OK)
async def list_sensors() -> list[SensorPluginSummary]:
    return [_to_summary(p) for p in get_default_registry().list_plugins()]


@router.get("/{sensor_type}", response_model=SensorPluginSummary, status_code=status.HTTP_200_OK)
async def get_sensor(sensor_type: str) -> SensorPluginSummary:
    try:
        plugin = get_default_registry().get(sensor_type)
    except SensorPluginNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return _to_summary(plugin)
