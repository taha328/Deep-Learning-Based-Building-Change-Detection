from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from .temporal_colors import additions_label, buffer_label


_VECTOR_LAYER_FIELDS = (
    ("additions_geojson", "Added building"),
    ("cumulative_union_geojson", "Cumulative growth"),
    ("automated_candidate_footprint_geojson", "Addition candidate diagnostics"),
)
_REMOVED_LAYER_FIELDS = {
    "automated_additions_geojson",
    "automated_building_blocks_geojson",
    "effective_building_blocks_geojson",
    "effective_footprint_geojson",
    "cumulative_growth_blocks_geojson",
}
_ARTIFACT_VECTOR_LAYERS = {
    "additions": "Added building",
    "cumulative_union": "Cumulative growth",
    "building_change_buffer_10m": "Buffer 10m",
    "building_change_buffer_15m": "Buffer 15m",
    "building_change_buffer_20m": "Buffer 20m",
    "automated_candidate_footprint": "Addition candidate diagnostics",
}


def utc_now_text() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def release_identifier(release: Dict[str, Any]) -> str:
    for key in ("identifier", "releaseIdentifier", "release_identifier", "id"):
        value = release.get(key)
        if value:
            return str(value)
    return ""


def release_display_label(release: Dict[str, Any]) -> str:
    identifier = release_identifier(release)
    raw_date = release_date_text(release)
    if identifier and raw_date:
        return "%s \u00b7 %s" % (raw_date, identifier)
    return str(release.get("label") or release.get("title") or identifier or "Wayback release")


def release_date_text(release: Dict[str, Any]) -> str:
    for key in ("release_date", "date", "capture_date", "archive_date"):
        value = release.get(key)
        if value:
            return str(value)[:10]
    label = release.get("label")
    if isinstance(label, str) and len(label) >= 10 and label[4:5] == "-" and label[7:8] == "-":
        return label[:10]
    return ""


def sorted_unique_releases(releases: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_id: Dict[str, Dict[str, Any]] = {}
    for release in releases:
        identifier = release_identifier(release)
        if identifier:
            by_id.setdefault(identifier, release)
    return sorted(by_id.values(), key=lambda release: (release_date_text(release), release_identifier(release)))


def clean_temporal_project_summaries(payload: object) -> List[Dict[str, Any]]:
    if isinstance(payload, dict):
        raw_items = payload.get("projects") or payload.get("items") or payload.get("data") or []
    else:
        raw_items = payload
    if not isinstance(raw_items, list):
        return []

    by_id: Dict[str, Dict[str, Any]] = {}
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        project_id = str(item.get("project_id") or item.get("id") or "").strip()
        if not project_id:
            continue
        if _is_cached_pairwise_run(item):
            continue
        by_id.setdefault(project_id, item)
    return sorted(by_id.values(), key=_project_sort_key, reverse=True)


def project_display_label(project: Dict[str, Any]) -> str:
    project_id = str(project.get("project_id") or project.get("id") or "").strip()
    name = str(project.get("name") or project.get("title") or "").strip()
    display_name = str(project.get("display_name") or "").strip()
    if not name and display_name and not display_name.lower().startswith("pairwise "):
        name = display_name
    if not name:
        name = project_id or "Temporal project"

    milestone_labels = _project_milestone_labels(project)
    if len(milestone_labels) >= 2:
        return "%s \u00b7 %s \u2192 %s" % (name, milestone_labels[0], milestone_labels[1])
    return name if name != project_id else project_id


def normalize_aoi_geojson_geometry(value: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("AOI must be a GeoJSON object.")

    value_type = str(value.get("type") or "")
    if value_type in {"Polygon", "MultiPolygon"}:
        _validate_polygon_coordinates(value)
        return value

    if value_type == "Feature":
        geometry = value.get("geometry")
        if not isinstance(geometry, dict):
            raise ValueError("AOI Feature must contain a geometry.")
        return normalize_aoi_geojson_geometry(geometry)

    if value_type == "FeatureCollection":
        features = value.get("features")
        if not isinstance(features, list):
            raise ValueError("AOI FeatureCollection must contain features.")
        geometries: List[Dict[str, Any]] = []
        for feature in features:
            if not isinstance(feature, dict):
                continue
            geometry = feature.get("geometry")
            if not isinstance(geometry, dict):
                continue
            try:
                normalized = normalize_aoi_geojson_geometry(geometry)
            except ValueError:
                continue
            geometries.append(normalized)
        return _merge_polygon_geometries(geometries)

    raise ValueError("AOI must be a Polygon or MultiPolygon geometry.")


def discover_temporal_layer_candidates(project: Dict[str, Any]) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    for index, milestone in enumerate(_iter_payload_milestones(project), start=1):
        label = _milestone_label(milestone, index)
        imagery = milestone.get("reference_imagery")
        if isinstance(imagery, dict):
            cog_path = imagery.get("cog_path") or imagery.get("canonical_cog_path") or imagery.get("reference_imagery_cog")
            cog_url = imagery.get("cog_url")
            if isinstance(cog_path, str) and cog_path:
                candidates.append({"name": f"{label} - reference imagery", "kind": "raster", "source": cog_path, "group": "reference"})
            elif isinstance(cog_url, str) and cog_url:
                candidates.append({"name": f"{label} - reference imagery", "kind": "raster_url", "source": cog_url, "group": "reference"})

        for key, title in _VECTOR_LAYER_FIELDS:
            payload = milestone.get(key)
            if _has_geojson_features(payload):
                layer_title = additions_label(milestone) if key == "additions_geojson" else title
                candidates.append({"name": f"{label} - {layer_title}", "kind": "geojson", "source": key, "group": "results"})

        buffers = milestone.get("buffer_layers_geojson")
        if isinstance(buffers, dict):
            for buffer_key, payload in sorted(buffers.items()):
                normalized_buffer_label = _buffer_label_text(buffer_key)
                if normalized_buffer_label in {"10 m", "15 m", "20 m"} and _has_geojson_features(payload):
                    candidates.append(
                        {
                            "name": f"{label} - {buffer_label(_buffer_distance_m(normalized_buffer_label), milestone)}",
                            "kind": "geojson",
                            "source": f"buffer_layers_geojson.{buffer_key}",
                            "group": "results",
                        }
                    )

        artifacts = milestone.get("artifacts")
        if isinstance(artifacts, list):
            for artifact in artifacts:
                if not isinstance(artifact, dict):
                    continue
                source = _artifact_geojson_source(artifact)
                key = str(artifact.get("key") or "")
                if source and key in _ARTIFACT_VECTOR_LAYERS and not _artifact_is_empty(artifact):
                    candidates.append(
                        {
                            "name": f"{label} - {_artifact_layer_title(key, milestone)}",
                            "kind": "geojson",
                            "source": source,
                            "group": "results",
                        }
                    )
                    continue
                path = artifact.get("path") or artifact.get("url")
                if not isinstance(path, str) or not path:
                    continue
                name = str(artifact.get("name") or artifact.get("description") or Path(path).name)
                kind = _artifact_kind(name, path)
                if kind == "raster":
                    candidates.append({"name": f"{label} - {name}", "kind": kind, "source": path, "group": "artifacts"})
    return candidates


def build_temporal_project_payload(
    *,
    name: str,
    aoi_geojson: Dict[str, Any],
    releases: List[Dict[str, Any]],
    project_id: Optional[str] = None,
) -> Dict[str, Any]:
    now = utc_now_text()
    milestones = [
        {
            "release_identifier": release_identifier(release),
            "release_date": release.get("release_date"),
            "status": "pending",
            "source_mode": "automated",
            "warnings": [],
            "buffer_layers_geojson": {},
            "artifacts": [],
        }
        for release in sorted_unique_releases(releases)
        if release_identifier(release)
    ]
    return {
        "project_id": project_id or f"qgis-{uuid4().hex[:12]}",
        "name": name.strip() or "Projet QGIS",
        "semantics": "expansion_only",
        "aoi_geojson": normalize_aoi_geojson_geometry(aoi_geojson),
        "milestones": milestones,
        "created_at": now,
        "updated_at": now,
        "warnings": [],
        "validation_blocking_errors": [],
        "latest_source": "esri_wayback",
    }


def _is_cached_pairwise_run(item: Dict[str, Any]) -> bool:
    kind = str(item.get("project_kind") or item.get("kind") or item.get("type") or "").lower()
    if kind in {"pairwise", "cached_run", "run"}:
        return True
    display = str(item.get("display_name") or item.get("name") or item.get("title") or "").strip().lower()
    if display.startswith("pairwise "):
        return True
    if "run_id" in item and not item.get("milestones"):
        return True
    return False


def _project_sort_key(project: Dict[str, Any]) -> str:
    return str(project.get("updated_at") or project.get("created_at") or project.get("project_id") or project.get("id") or "")


def _project_milestone_labels(project: Dict[str, Any]) -> List[str]:
    milestones = project.get("milestones")
    labels: List[str] = []
    if isinstance(milestones, list):
        for milestone in milestones[:2]:
            if not isinstance(milestone, dict):
                continue
            label = milestone.get("label") or milestone.get("release_identifier") or milestone.get("releaseIdentifier")
            if label:
                labels.append(str(label))
    if len(labels) >= 2:
        return labels[:2]

    for keys in (("start_milestone", "end_milestone"), ("from_release", "to_release"), ("t1", "t2")):
        values = [project.get(key) for key in keys]
        if all(values):
            return [str(values[0]), str(values[1])]
    return []


def _milestone_label(milestone: Dict[str, Any], index: int) -> str:
    date_value = milestone.get("release_date") or milestone.get("date")
    if date_value:
        return str(date_value)[:10]
    return str(milestone.get("release_identifier") or milestone.get("releaseIdentifier") or f"milestone-{index}")


def _iter_payload_milestones(project: Dict[str, Any]) -> List[Dict[str, Any]]:
    milestones: List[Dict[str, Any]] = []
    raw_milestones = project.get("milestones")
    if isinstance(raw_milestones, list):
        milestones.extend(item for item in raw_milestones if isinstance(item, dict))

    runs = project.get("runs")
    if isinstance(runs, list):
        for run in runs:
            if not isinstance(run, dict):
                continue
            run_project = run.get("project")
            if isinstance(run_project, dict):
                milestones.extend(_iter_payload_milestones(run_project))
            run_milestones = run.get("milestones")
            if isinstance(run_milestones, list):
                milestones.extend(item for item in run_milestones if isinstance(item, dict))
    return milestones


def _has_geojson_features(payload: Any) -> bool:
    return isinstance(payload, dict) and isinstance(payload.get("features"), list) and bool(payload["features"])


def _buffer_label_text(value: Any) -> str:
    text = str(value).strip()
    if text.endswith("m") and not text.endswith(" m"):
        text = text[:-1].strip()
    try:
        number = float(text)
        if number.is_integer():
            return f"{int(number)} m"
        return f"{number:g} m"
    except ValueError:
        return text


def _buffer_distance_m(label: str) -> int:
    if label == "15 m":
        return 15
    if label == "20 m":
        return 20
    return 10


def _artifact_layer_title(key: str, milestone: Dict[str, Any]) -> str:
    if key == "additions":
        return additions_label(milestone)
    if key == "building_change_buffer_15m":
        return buffer_label(15, milestone)
    if key == "building_change_buffer_20m":
        return buffer_label(20, milestone)
    if key == "building_change_buffer_10m":
        return buffer_label(10, milestone)
    return _ARTIFACT_VECTOR_LAYERS[key]


def _artifact_kind(name: str, path: str) -> str:
    lowered = f"{name} {path}".lower()
    if "probability" in lowered or lowered.endswith((".tif", ".tiff")):
        return "raster"
    if "mask" in lowered:
        return "raster"
    return "artifact"


def _artifact_geojson_source(artifact: Dict[str, Any]) -> Optional[str]:
    if artifact.get("media_type") != "application/geo+json":
        return None
    preferred_format = str(artifact.get("qgis_preferred_format") or artifact.get("qgisPreferredFormat") or "").lower()
    preferred_url = artifact.get("qgis_preferred_url") or artifact.get("qgisPreferredUrl")
    if preferred_format == "gpkg" and isinstance(preferred_url, str) and preferred_url:
        return preferred_url
    for key in ("gpkg_url", "gpkgUrl", "geojson_url", "geojsonUrl", "download_url", "downloadUrl", "artifact_url", "artifactUrl", "url", "path"):
        value = artifact.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _artifact_is_empty(artifact: Dict[str, Any]) -> bool:
    feature_count = artifact.get("feature_count")
    if feature_count is None:
        feature_count = artifact.get("featureCount")
    try:
        return feature_count is not None and int(feature_count) == 0
    except (TypeError, ValueError):
        return False


def _validate_polygon_coordinates(geometry: Dict[str, Any]) -> None:
    coordinates = geometry.get("coordinates")
    if not coordinates:
        raise ValueError("AOI geometry has no coordinates.")
    geometry_type = geometry.get("type")
    if geometry_type == "Polygon":
        if not isinstance(coordinates, list) or not coordinates or not isinstance(coordinates[0], list):
            raise ValueError("AOI Polygon coordinates are invalid.")
        return
    if geometry_type == "MultiPolygon":
        if not isinstance(coordinates, list) or not coordinates or not isinstance(coordinates[0], list):
            raise ValueError("AOI MultiPolygon coordinates are invalid.")
        return
    raise ValueError("AOI must be Polygon or MultiPolygon.")


def _merge_polygon_geometries(geometries: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not geometries:
        raise ValueError("AOI FeatureCollection contains no polygon geometry.")
    if len(geometries) == 1:
        return geometries[0]

    multipolygon_coordinates: List[Any] = []
    for geometry in geometries:
        if geometry.get("type") == "Polygon":
            multipolygon_coordinates.append(geometry.get("coordinates"))
        elif geometry.get("type") == "MultiPolygon":
            coordinates = geometry.get("coordinates")
            if isinstance(coordinates, list):
                multipolygon_coordinates.extend(coordinates)
    if not multipolygon_coordinates:
        raise ValueError("AOI FeatureCollection contains no polygon geometry.")
    return {"type": "MultiPolygon", "coordinates": multipolygon_coordinates}
