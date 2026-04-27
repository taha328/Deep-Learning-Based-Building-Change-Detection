from __future__ import annotations

import json
from pathlib import Path
import re

from src.config import Settings
from src.domain.exports import _get_raster_georeferencing
from src.schemas import RunResponse


def request_result_dir(settings: Settings, request_hash: str) -> Path:
    path = settings.request_cache_dir / request_hash
    path.mkdir(parents=True, exist_ok=True)
    return path


def response_json_path(settings: Settings, request_hash: str) -> Path:
    return request_result_dir(settings, request_hash) / "run_response.json"


def _candidate_rasters(result_dir: Path) -> list[Path]:
    explicit = [
        result_dir / "t1_wayback_rgb.tif",
        result_dir / "t2_wayback_rgb.tif",
        result_dir / "change_probability.tif",
        result_dir / "building_change_mask.tif",
    ]
    existing = [path for path in explicit if path.exists()]
    if existing:
        return existing
    return sorted(result_dir.glob("*.tif"))


def _upgrade_preview_georeferencing(raw_payload: dict[str, object], result_dir: Path) -> tuple[dict[str, object], bool]:
    preview = raw_payload.get("preview_images")
    if not isinstance(preview, dict):
        return raw_payload, False

    has_any_preview = any(
        bool(preview.get(key))
        for key in (
            "t1_preview_path",
            "t2_preview_path",
            "change_probability_preview_path",
            "change_overlay_preview_path",
        )
    )
    if not has_any_preview:
        return raw_payload, False

    if preview.get("raster_bounds_wgs84"):
        return raw_payload, False

    georef: dict[str, object] = {}
    for raster_path in _candidate_rasters(result_dir):
        georef = _get_raster_georeferencing(raster_path)
        if georef:
            break
    if not georef:
        return raw_payload, False

    upgraded = dict(raw_payload)
    upgraded_preview = dict(preview)
    for key, value in georef.items():
        upgraded_preview[key] = value
    upgraded["preview_images"] = upgraded_preview
    return upgraded, True


def _upgrade_buffer_layers(raw_payload: dict[str, object], result_dir: Path) -> tuple[dict[str, object], bool]:
    existing = raw_payload.get("buffer_layers_geojson")
    if isinstance(existing, dict) and existing:
        return raw_payload, False

    buffer_layers: dict[str, object] = {}
    for path in sorted(result_dir.glob("building_change_buffer_*.geojson")):
        match = re.fullmatch(r"building_change_buffer_(.+)\.geojson", path.name)
        if not match:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("type") == "FeatureCollection":
            buffer_layers[match.group(1)] = payload

    if not buffer_layers:
        return raw_payload, False

    upgraded = dict(raw_payload)
    upgraded["buffer_layers_geojson"] = buffer_layers
    return upgraded, True


def load_cached_response(settings: Settings, request_hash: str) -> RunResponse | None:
    path = response_json_path(settings, request_hash)
    if not path.exists():
        return None
    raw_payload = json.loads(path.read_text())
    upgraded_payload, changed = _upgrade_preview_georeferencing(raw_payload, path.parent)
    upgraded_payload, buffer_changed = _upgrade_buffer_layers(upgraded_payload, path.parent)
    changed = changed or buffer_changed
    if changed:
        path.write_text(json.dumps(upgraded_payload, indent=2))
    return RunResponse.model_validate(upgraded_payload)


def save_cached_response(settings: Settings, request_hash: str, response: RunResponse) -> None:
    path = response_json_path(settings, request_hash)
    path.write_text(json.dumps(response.model_dump(mode="json"), indent=2))
