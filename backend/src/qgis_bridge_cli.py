from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.config import Settings, get_settings
from src.core_api import (
    list_releases_api,
    probe_backends_api,
    run_detection_api,
    run_segmentation_api,
    validate_request_api,
    validate_segmentation_api,
)
from src.execution_profiles import PipelineExecutionConfig
from src.schemas import RunRequest, SegmentationRequest, ValidationRequest


EVENT_PREFIX = "__BC_EVENT__"


class RunnerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runtime_cache_dir: str | None = None
    execution: PipelineExecutionConfig = Field(default_factory=PipelineExecutionConfig)


def _emit_event(event: dict[str, Any]) -> None:
    print(f"{EVENT_PREFIX}{json.dumps(event, ensure_ascii=True)}", flush=True)


def _load_json(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _settings_from_config(config: RunnerConfig | None) -> Settings:
    settings = get_settings()
    if config is None or not config.runtime_cache_dir:
        return settings
    configured = settings.model_copy(update={"runtime_cache_dir": Path(config.runtime_cache_dir)})
    configured.ensure_runtime_cache_dirs()
    return configured


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="QGIS bridge CLI for Building Change Detection")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command in ("list_releases", "probe_backends"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--config-file", dest="config_file")

    for command in ("validate_request", "run_detection", "validate_segmentation", "run_segmentation"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--request-file", required=True)
        sub.add_argument("--config-file", dest="config_file")

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    config = RunnerConfig.model_validate(_load_json(args.config_file)) if getattr(args, "config_file", None) else None
    settings = _settings_from_config(config)

    if args.command == "list_releases":
        response = list_releases_api(settings=settings)
        print(response.model_dump_json())
        return 0

    if args.command == "probe_backends":
        response = [item.model_dump(mode="json") for item in probe_backends_api(settings=settings, execution_config=config.execution if config else None)]
        print(json.dumps(response))
        return 0

    request_payload = _load_json(args.request_file)

    if args.command == "validate_request":
        request = ValidationRequest.model_validate(request_payload)
        response = validate_request_api(
            request,
            settings=settings,
            execution_config=config.execution if config else None,
        )
        print(response.model_dump_json())
        return 0

    if args.command == "validate_segmentation":
        request = SegmentationRequest.model_validate(request_payload)
        response = validate_segmentation_api(
            request,
            settings=settings,
            execution_config=config.execution if config else None,
        )
        print(response.model_dump_json())
        return 0

    if args.command == "run_detection":
        request = RunRequest.model_validate(request_payload)

        def progress_callback(value: float, message: str) -> None:
            _emit_event({"event": "progress", "progress": value, "message": message})

        response = run_detection_api(
            request,
            settings=settings,
            execution_config=config.execution if config else None,
            progress_callback=progress_callback,
        )
        _emit_event(
            {
                "event": "result",
                "payload": response.model_dump(mode="json"),
            }
        )
        return 0

    if args.command == "run_segmentation":
        request = SegmentationRequest.model_validate(request_payload)

        def progress_callback(value: float, message: str) -> None:
            _emit_event({"event": "progress", "progress": value, "message": message})

        response = run_segmentation_api(
            request,
            settings=settings,
            execution_config=config.execution if config else None,
            progress_callback=progress_callback,
        )
        _emit_event(
            {
                "event": "result",
                "payload": response.model_dump(mode="json"),
            }
        )
        return 0

    raise RuntimeError(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
