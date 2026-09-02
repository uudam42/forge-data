"""Pipeline YAML/JSON config file <-> `PipelineRunRequest` (Design
Requirement 5).

Deliberately NOT a second semantics system: every key besides `streams[].
path` maps directly onto the real `PipelineRunRequest`/`PipelineStreamConfig`/
`PipelineCleaningConfig`/etc. field names from `app.runs.models`, and is
handed to `PipelineRunRequest.model_validate()` unmodified -- the exact
same Pydantic validation `POST /api/v1/runs` uses, not a reimplementation
of it. The only CLI-specific concept is `streams[].path`: a per-stream
input file path, which `PipelineRunRequest` has no field for (the HTTP API
takes file bytes as separate multipart uploads) and which this module
resolves relative to the *config file's own directory* -- not the
workspace root -- so a pipeline config stays portable if the workspace
that runs it moves (Design Requirement 70).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from app.cli.errors import CliError
from app.runs.models import PipelineRunRequest


class PipelineConfigError(CliError):
    pass


@dataclass(frozen=True)
class ResolvedStream:
    sensor_type: str
    path: Path


@dataclass(frozen=True)
class LoadedPipelineConfig:
    request: PipelineRunRequest
    streams: list[ResolvedStream]
    source_path: Path


def _load_raw(config_path: Path) -> dict:
    if not config_path.is_file():
        raise PipelineConfigError(f"Config file not found: {config_path}")
    text = config_path.read_text(encoding="utf-8")
    try:
        if config_path.suffix.lower() == ".json":
            data = json.loads(text)
        else:
            data = yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise PipelineConfigError(f"{config_path}: invalid {'JSON' if config_path.suffix.lower() == '.json' else 'YAML'} -- {exc}") from exc
    if not isinstance(data, dict):
        raise PipelineConfigError(f"{config_path}: top-level document must be a mapping, got {type(data).__name__}")
    return data


def load_pipeline_config(config_path: Path) -> LoadedPipelineConfig:
    config_path = config_path.resolve()
    raw = dict(_load_raw(config_path))
    base_dir = config_path.parent

    streams_raw = raw.pop("streams", None)
    if not streams_raw or not isinstance(streams_raw, list):
        raise PipelineConfigError(f"{config_path}: 'streams' must be a non-empty list")

    resolved_streams: list[ResolvedStream] = []
    request_streams: list[dict] = []
    for i, stream in enumerate(streams_raw):
        if not isinstance(stream, dict) or "sensor_type" not in stream:
            raise PipelineConfigError(f"{config_path}: streams[{i}] is missing required field 'sensor_type'")
        if "path" not in stream:
            raise PipelineConfigError(f"{config_path}: streams[{i}] ('{stream['sensor_type']}') is missing required field 'path'")
        resolved_streams.append(ResolvedStream(sensor_type=stream["sensor_type"], path=(base_dir / stream["path"]).resolve()))
        request_streams.append({"sensor_type": stream["sensor_type"], "source_units": stream.get("source_units", {})})

    raw["streams"] = request_streams
    try:
        request = PipelineRunRequest.model_validate(raw)
    except ValidationError as exc:
        raise PipelineConfigError(f"{config_path}: {exc}") from exc

    return LoadedPipelineConfig(request=request, streams=resolved_streams, source_path=config_path)
