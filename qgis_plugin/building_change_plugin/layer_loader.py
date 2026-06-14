from __future__ import annotations

import json
import re
import time
from html import escape
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from qgis.core import Qgis, QgsMessageLog, QgsProject, QgsRasterLayer, QgsVectorLayer

from .api_client import BackendClient
from .errors import LayerLoadError
from .layer_controller import (
    ADDITIONS_LAYER_KEY,
    BUFFER_10_LAYER_KEY,
    BUFFER_15_LAYER_KEY,
    BUFFER_20_LAYER_KEY,
    CUMULATIVE_GROWTH_LAYER_KEY,
    DIAGNOSTICS_LAYER_KEY,
    REFERENCE_LAYER_KEY,
    TemporalLayerController,
    default_layer_visibility,
    install_temporal_layer_controller,
    is_baseline_milestone,
    milestone_display_label,
    milestone_release_identifier,
    milestone_date,
    select_active_milestone,
    sorted_milestones_newest_first,
    sorted_milestones_oldest_first,
)
from .models import discover_temporal_layer_candidates
from .styles import (
    style_additions,
    style_buffer,
    style_buffer_15,
    style_buffer_20,
    style_cumulative,
    style_flagged,
    style_mask_raster,
    style_probability_raster,
    style_reference_raster,
    style_temporal_artifact,
)
from .temporal_colors import (
    additions_label,
    all_previous_additions_label,
    buffer_label,
    get_milestone_color_map,
    temporal_style_for_artifact,
)


StyleFn = Callable[[QgsVectorLayer], None]
LOG_CATEGORY = "Building Change"

ArtifactVectorSpec = Tuple[str, str, str, StyleFn]
ARTIFACT_VECTOR_SPECS: Dict[str, ArtifactVectorSpec] = {
    "additions": (ADDITIONS_LAYER_KEY, "Added building", "additions_geojson", style_additions),
    "cumulative_union": (CUMULATIVE_GROWTH_LAYER_KEY, "Cumulative growth", "cumulative_union_geojson", style_cumulative),
    "automated_candidate_footprint": (
        DIAGNOSTICS_LAYER_KEY,
        "Addition candidate diagnostics",
        "automated_candidate_footprint_geojson",
        style_flagged,
    ),
    "building_change_buffer_10m": (BUFFER_10_LAYER_KEY, "Buffer 10m", "buffer_layers_geojson.10m", style_buffer),
    "building_change_buffer_15m": (BUFFER_15_LAYER_KEY, "Buffer 15m", "buffer_layers_geojson.15m", style_buffer_15),
    "building_change_buffer_20m": (BUFFER_20_LAYER_KEY, "Buffer 20m", "buffer_layers_geojson.20m", style_buffer_20),
}
DEFAULT_ARTIFACT_VECTOR_KEYS = {"additions", "building_change_buffer_10m"}


def load_temporal_project_layers(
    project: Dict[str, Any],
    *,
    client: BackendClient,
    output_dir: Path,
    active_release_identifier: Optional[str] = None,
    active_display_date: Optional[str] = None,
) -> List[str]:
    load_started_at = time.perf_counter()
    project_id = str(project.get("project_id") or "temporal-project")
    project_name = str(project.get("name") or project_id)
    base_group = _reset_project_group(project_name)
    controller = install_temporal_layer_controller(project_id)
    layer_dir = output_dir / "qgis_layers" / _safe_name(project_id)
    layer_dir.mkdir(parents=True, exist_ok=True)
    candidates = discover_temporal_layer_candidates(project)
    _log(f"QGIS_LOAD_LAYERS_STAGE projectId={project_id} stage=discover_artifacts")
    _log(f"BUILDING_CHANGE_PLUGIN_LAYER_DISCOVERY project_id={project_id} artifacts_found={len(candidates)}")
    for candidate in candidates:
        _log(
            "BUILDING_CHANGE_PLUGIN_LAYER_CANDIDATE "
            f"name={candidate.get('name')} type={candidate.get('kind')} source={candidate.get('source')}"
        )

    added: List[str] = []
    milestones_oldest_first = sorted_milestones_oldest_first(project.get("milestones") or [])
    milestone_color_map = get_milestone_color_map(project.get("milestones") or [])
    active_milestone, active_reason = select_active_milestone(
        project,
        requested_release_identifier=active_release_identifier,
        requested_display_date=active_display_date,
    )
    active_release_id = milestone_release_identifier(active_milestone) if active_milestone else ""
    if active_reason == "requested":
        _log(
            "QGIS_ACTIVE_MILESTONE_SELECTED "
            f"project_id={project_id} display_date={milestone_date(active_milestone)} release_identifier={active_release_id}"
        )
    elif active_release_id:
        _log(
            "QGIS_ACTIVE_MILESTONE_FALLBACK "
            f"project_id={project_id} reason={active_reason} selected_release_identifier={active_release_id}"
        )
    else:
        _log(f"QGIS_ACTIVE_MILESTONE_FALLBACK project_id={project_id} reason=no_milestones selected_release_identifier=")
    milestones_newest_first = sorted_milestones_newest_first(project.get("milestones") or [])
    included_addition_milestones = _milestones_up_to_release(milestones_oldest_first, active_release_id)
    all_previous_additions_group = base_group.addGroup(all_previous_additions_label(milestones_oldest_first, active_release_id))
    all_previous_added: List[str] = []
    for included_milestone in included_addition_milestones:
        included_label = milestone_display_label(included_milestone, 1)
        included_baseline = is_baseline_milestone(included_milestone, milestones_oldest_first)
        all_previous_added.extend(
            _load_all_previous_additions_layer(
                milestone=included_milestone,
                label=included_label,
                client=client,
                layer_dir=layer_dir,
                group=all_previous_additions_group,
                controller=controller,
                project_id=project_id,
                is_baseline=included_baseline,
                milestone_color_map=milestone_color_map,
            )
        )
    _log(
        "QGIS_ALL_PREVIOUS_ADDITIONS_LOAD_DONE "
        f"projectId={project_id} activeReleaseIdentifier={active_release_id} "
        f"includedReleaseIdentifiers={','.join(milestone_release_identifier(milestone) for milestone in included_addition_milestones)} "
        f"createdCount={len(all_previous_added)}"
    )
    added.extend(all_previous_added)
    _log(
        "QGIS_LOAD_LAYERS_SELECTED_MILESTONES "
        f"projectId={project_id} selectedCount={len(milestones_newest_first)} "
        f"activeReleaseIdentifier={active_release_id} "
        f"releaseIdentifiers={','.join(milestone_release_identifier(milestone) for milestone in milestones_newest_first)}"
    )
    for index, milestone in enumerate(milestones_newest_first, start=1):
        if not isinstance(milestone, dict):
            continue
        label = milestone_display_label(milestone, index)
        release_id = milestone_release_identifier(milestone)
        is_active_milestone = release_id == active_release_id
        milestone_visible = is_active_milestone
        release_date = milestone_date(milestone)
        _log(
            "QGIS_MILESTONE_LOAD_START "
            f"projectId={project_id} releaseIdentifier={release_id} milestoneDate={release_date} "
            f"activeReleaseIdentifier={active_release_id} isActive={is_active_milestone}"
        )
        milestone_group = base_group.addGroup(label)
        controller.tag_group(milestone_group, release_identifier=release_id, release_date=release_date)
        detection_group = milestone_group.addGroup("Detection results")
        reference_group = milestone_group.addGroup("Reference imagery")
        baseline = is_baseline_milestone(milestone, milestones_oldest_first)
        _log(f"QGIS_LOAD_LAYERS_STAGE projectId={project_id} releaseIdentifier={release_id} stage=prepare_default_result_layers")
        detection_added: List[str] = []
        detection_added.extend(
            _load_milestone_vectors(
                milestone=milestone,
                label=label,
                layer_dir=layer_dir,
                detection_group=detection_group,
                controller=controller,
                project_id=project_id,
                is_baseline=baseline,
                milestone_color_map=milestone_color_map,
            )
        )
        _log(
            "QGIS_DETECTION_ARTIFACT_VECTOR_CALL "
            f"projectId={project_id} releaseIdentifier={release_id} label={label} "
            f"artifactKeys={_artifact_keys_for_log(milestone)} hasMilestoneColorMap={bool(milestone_color_map)}"
        )
        detection_added.extend(
            _load_milestone_artifact_vectors(
                milestone=milestone,
                label=label,
                client=client,
                group=detection_group,
                controller=controller,
                project_id=project_id,
                is_baseline=baseline,
                milestone_color_map=milestone_color_map,
            )
        )
        _log(
            "QGIS_DETECTION_ARTIFACT_RASTER_CALL "
            f"projectId={project_id} releaseIdentifier={release_id} label={label} "
            f"artifactKeys={_artifact_keys_for_log(milestone)} hasMilestoneColorMap={bool(milestone_color_map)}"
        )
        detection_added.extend(
            _load_milestone_artifact_rasters(
                milestone=milestone,
                label=label,
                group=detection_group,
                controller=controller,
                project_id=project_id,
                is_baseline=baseline,
                milestone_color_map=milestone_color_map,
            )
        )
        added.extend(detection_added)
        _log(
            "QGIS_MILESTONE_DETECTION_LOAD_DONE "
            f"projectId={project_id} releaseIdentifier={release_id} milestoneDate={release_date} "
            f"activeReleaseIdentifier={active_release_id} isActive={is_active_milestone} "
            f"createdCount={len(detection_added)} checked={milestone_visible}"
        )
        _log(f"QGIS_LOAD_LAYERS_STAGE projectId={project_id} releaseIdentifier={release_id} stage=prepare_reference_imagery")
        reference_added = _load_reference_imagery(
            milestone=milestone,
            label=label,
            client=client,
            group=reference_group,
            layer_dir=layer_dir,
            controller=controller,
            project_id=project_id,
        )
        added.extend(reference_added)
        _log(
            "QGIS_MILESTONE_REFERENCE_LOAD_DONE "
            f"projectId={project_id} releaseIdentifier={release_id} milestoneDate={release_date} "
            f"activeReleaseIdentifier={active_release_id} isActive={is_active_milestone} "
            f"createdCount={len(reference_added)} checked={milestone_visible}"
        )
        controller.initialize_milestone_group(milestone_group, visible=milestone_visible, expanded=milestone_visible)
        _log(
            "QGIS_MILESTONE_VISIBILITY_SET "
            f"projectId={project_id} releaseIdentifier={release_id} milestoneDate={release_date} "
            f"activeReleaseIdentifier={active_release_id} isActive={is_active_milestone} checked={milestone_visible}"
        )
    if not added:
        if not candidates:
            raise LayerLoadError("No backend-generated layers were available to load: no artifacts in project payload.")
        raise LayerLoadError(
            "Backend artifacts were discovered, but QGIS could not load them. "
            "Check that local artifact paths exist and that QGIS can read the raster/vector formats."
        )
    _log(
        "QGIS_LOAD_LAYERS_DONE "
        f"projectId={project_id} layerCount={len(added)} durationMs={round((time.perf_counter() - load_started_at) * 1000, 2)}"
    )
    return added


def _reset_project_group(project_name: str):
    root = QgsProject.instance().layerTreeRoot()
    parent = root.findGroup("Building Change") or root.insertGroup(0, "Building Change")
    existing = parent.findGroup(project_name)
    if existing is not None:
        for layer_id in _collect_layer_ids(existing):
            QgsProject.instance().removeMapLayer(layer_id)
        parent.removeChildNode(existing)
    return parent.insertGroup(0, project_name)


def _collect_layer_ids(group) -> List[str]:
    layer_ids: List[str] = []
    for child in group.children():
        layer = getattr(child, "layer", lambda: None)()
        if layer is not None:
            layer_ids.append(layer.id())
            continue
        if hasattr(child, "children"):
            layer_ids.extend(_collect_layer_ids(child))
    return layer_ids


def _add_layer(layer, group, *, visible: bool = True, controller: TemporalLayerController = None) -> bool:
    if not layer.isValid():
        return False
    QgsProject.instance().addMapLayer(layer, False)
    node = group.addLayer(layer)
    if controller is not None:
        controller.initialize_node(node, visible=visible)
    else:
        node.setItemVisibilityChecked(visible)
    return True


def _load_reference_imagery(
    milestone: Dict[str, Any],
    label: str,
    client: BackendClient,
    group,
    layer_dir: Path,
    controller: TemporalLayerController,
    project_id: str,
) -> List[str]:
    imagery = milestone.get("reference_imagery")
    if not isinstance(imagery, dict):
        return []
    layer_name = f"{label} - reference imagery"
    cog_path = imagery.get("cog_path")
    if not (isinstance(cog_path, str) and Path(cog_path).exists()):
        canonical_cog_path = imagery.get("canonical_cog_path")
        if isinstance(canonical_cog_path, str) and Path(canonical_cog_path).exists():
            cog_path = canonical_cog_path
    if isinstance(cog_path, str) and Path(cog_path).exists():
        qgis_source = _qgis_reference_source(Path(cog_path), layer_dir, label)
        layer = QgsRasterLayer(str(qgis_source), layer_name)
        visible = default_layer_visibility(REFERENCE_LAYER_KEY)
        _set_layer_metadata(layer, milestone, project_id, REFERENCE_LAYER_KEY, visible, controller)
        if _add_layer(layer, group, visible=visible, controller=controller):
            style_reference_raster(layer)
            _log(f"BUILDING_CHANGE_PLUGIN_LAYER_LOADED name={layer_name}")
            return [layer_name]
        _log(f"BUILDING_CHANGE_PLUGIN_LAYER_MISSING name={layer_name} reason=qgis_invalid_cog source={qgis_source}", Qgis.Warning)
    elif isinstance(cog_path, str) and cog_path:
        _log(f"BUILDING_CHANGE_PLUGIN_LAYER_MISSING name={layer_name} reason=path_missing source={cog_path}", Qgis.Warning)
    tiles_template = imagery.get("tiles_url_template")
    if isinstance(tiles_template, str) and _is_tile_template(tiles_template):
        _log(
            f"INVALID_TILE_TEMPLATE_REQUEST_BLOCKED name={layer_name} source={tiles_template}",
            Qgis.Warning,
        )
    return []


def _load_milestone_vectors(
    milestone: Dict[str, Any],
    label: str,
    layer_dir: Path,
    detection_group,
    controller: TemporalLayerController,
    project_id: str,
    is_baseline: bool,
    milestone_color_map: Dict[str, str] = None,
) -> List[str]:
    release_id = milestone_release_identifier(milestone)
    specs: List[Tuple[str, str, str, StyleFn, Any]] = [
        (
            ADDITIONS_LAYER_KEY,
            "additions_geojson",
            additions_label(milestone),
            _temporal_style_fn(release_id, "additions", milestone_color_map, style_additions),
            detection_group,
        ),
        (CUMULATIVE_GROWTH_LAYER_KEY, "cumulative_union_geojson", "Cumulative growth", style_cumulative, detection_group),
        (DIAGNOSTICS_LAYER_KEY, "automated_candidate_footprint_geojson", "Addition candidate diagnostics", style_flagged, detection_group),
    ]
    added: List[str] = []
    for layer_key, key, title, style_fn, group in specs:
        name = title
        visible = default_layer_visibility(layer_key, is_baseline=is_baseline)
        if _load_geojson_payload(
            milestone.get(key),
            layer_dir / f"{_safe_name(label)}_{key}.geojson",
            name,
            style_fn,
            group,
            visible,
            milestone,
            project_id,
            layer_key,
            controller,
        ):
            added.append(name)

    buffers = milestone.get("buffer_layers_geojson")
    if isinstance(buffers, dict):
        for buffer_label, payload in sorted(buffers.items()):
            normalized_label = _buffer_label_text(buffer_label)
            if normalized_label not in {"10 m", "15 m", "20 m"}:
                continue
            title = buffer_label(_buffer_distance_m(normalized_label), milestone)
            path = layer_dir / f"{_safe_name(label)}_buffer_{_safe_name(str(buffer_label))}.geojson"
            layer_key = _buffer_layer_key(normalized_label)
            visible = default_layer_visibility(layer_key, is_baseline=is_baseline)
            style_fn = _temporal_style_fn(release_id, _buffer_artifact_key(normalized_label), milestone_color_map or {}, _buffer_style(normalized_label))
            if _load_geojson_payload(payload, path, title, style_fn, detection_group, visible, milestone, project_id, layer_key, controller):
                added.append(title)
    return added


def _milestones_up_to_release(milestones_oldest_first: List[Dict[str, Any]], active_release_identifier: str) -> List[Dict[str, Any]]:
    if not active_release_identifier:
        return []
    included: List[Dict[str, Any]] = []
    for milestone in milestones_oldest_first:
        included.append(milestone)
        if milestone_release_identifier(milestone) == active_release_identifier:
            return included
    return []


def _load_all_previous_additions_layer(
    *,
    milestone: Dict[str, Any],
    label: str,
    client: BackendClient,
    layer_dir: Path,
    group,
    controller: TemporalLayerController,
    project_id: str,
    is_baseline: bool,
    milestone_color_map: Optional[Dict[str, str]] = None,
) -> List[str]:
    if is_baseline:
        return []
    release_id = milestone_release_identifier(milestone)
    layer_name = additions_label(milestone)
    style_fn = _temporal_style_fn(release_id, "additions", milestone_color_map or {}, style_additions)
    visible = default_layer_visibility(ADDITIONS_LAYER_KEY, is_baseline=is_baseline)
    inline_payload = milestone.get("additions_geojson")
    if _load_geojson_payload(
        inline_payload,
        layer_dir / "all_previous_additions" / f"{_safe_name(label)}_additions.geojson",
        layer_name,
        style_fn,
        group,
        visible,
        milestone,
        project_id,
        ADDITIONS_LAYER_KEY,
        controller,
    ):
        _log(
            "QGIS_ALL_PREVIOUS_ADDITIONS_LAYER_LOADED "
            f"projectId={project_id} releaseIdentifier={release_id} source=inline_geojson layerName={layer_name} checked={visible}"
        )
        return [layer_name]

    artifact = next(
        (
            item
            for item in milestone.get("artifacts") or []
            if isinstance(item, dict) and str(item.get("key") or "") == "additions" and not _artifact_is_empty(item)
        ),
        None,
    )
    if not isinstance(artifact, dict):
        _log(
            "QGIS_ALL_PREVIOUS_ADDITIONS_LAYER_SKIPPED "
            f"projectId={project_id} releaseIdentifier={release_id} reason=missing_additions_artifact"
        )
        return []
    source_info = _artifact_vector_source(artifact, client)
    if source_info is None:
        _log(
            "QGIS_ALL_PREVIOUS_ADDITIONS_LAYER_SKIPPED "
            f"projectId={project_id} releaseIdentifier={release_id} reason=missing_geojson_source"
        )
        return []
    source, selected_format = source_info
    cached_source = _local_cached_artifact_source(
        artifact,
        client,
        project_id=project_id,
        release_identifier=release_id,
        artifact_key="additions",
        source=source,
        selected_format=selected_format,
    )
    layer = QgsVectorLayer(cached_source, layer_name, "ogr")
    _set_layer_metadata(layer, milestone, project_id, ADDITIONS_LAYER_KEY, visible, controller)
    if not _add_layer(layer, group, visible=visible, controller=controller):
        _log(
            "QGIS_ALL_PREVIOUS_ADDITIONS_LAYER_SKIPPED "
            f"projectId={project_id} releaseIdentifier={release_id} reason=qgis_invalid_vector source={cached_source}",
            Qgis.Warning,
        )
        return []
    style_fn(layer)
    _log(
        "QGIS_ALL_PREVIOUS_ADDITIONS_LAYER_LOADED "
        f"projectId={project_id} releaseIdentifier={release_id} source={selected_format} layerName={layer_name} checked={visible}"
    )
    return [layer_name]


def _load_milestone_artifact_rasters(
    milestone: Dict[str, Any],
    label: str,
    group,
    controller: TemporalLayerController,
    project_id: str,
    is_baseline: bool,
    milestone_color_map: Optional[Dict[str, str]] = None,
) -> List[str]:
    added: List[str] = []
    artifacts = milestone.get("artifacts") or []
    release_id = milestone_release_identifier(milestone)
    _log(
        "QGIS_RASTER_ARTIFACTS_LOAD_START "
        f"projectId={project_id} releaseIdentifier={release_id} label={label} "
        f"artifactCount={len(artifacts)} hasMilestoneColorMap={bool(milestone_color_map)}"
    )
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        artifact_key = str(artifact.get("key") or artifact.get("name") or "")
        artifact_metadata_keys = ",".join(sorted(str(key) for key in artifact.keys()))
        _log(
            "QGIS_RASTER_ARTIFACT_INSPECT "
            f"projectId={project_id} releaseIdentifier={release_id} artifactKey={artifact_key} "
            f"metadataKeys={artifact_metadata_keys} renderingStrategy=raster"
        )
        name = str(artifact.get("name") or "")
        path = artifact.get("path")
        if not isinstance(path, str) or not Path(path).exists():
            _log(
                "QGIS_RASTER_ARTIFACT_SKIPPED "
                f"projectId={project_id} releaseIdentifier={release_id} artifactKey={artifact_key} "
                f"reason=missing_local_path path={path} metadataKeys={artifact_metadata_keys}",
                Qgis.Info,
            )
            continue
        if "probability" in name:
            layer_name = f"{label} - Change probability raster"
            layer = QgsRasterLayer(path, layer_name)
            visible = default_layer_visibility("change_probability", is_baseline=is_baseline)
            _set_layer_metadata(layer, milestone, project_id, "change_probability", visible, controller)
            if _add_layer(layer, group, visible=visible, controller=controller):
                style_probability_raster(layer)
                added.append(layer_name)
                _log(
                    "QGIS_RASTER_ARTIFACT_LAYER_ADD_DONE "
                    f"projectId={project_id} releaseIdentifier={release_id} artifactKey={artifact_key} "
                    f"layerName={layer_name} visible={visible} renderingStrategy=raster"
                )
            else:
                _log(
                    "QGIS_RASTER_ARTIFACT_LAYER_ADD_FAILED "
                    f"projectId={project_id} releaseIdentifier={release_id} artifactKey={artifact_key} "
                    f"layerName={layer_name} path={path} renderingStrategy=raster",
                    Qgis.Warning,
                )
        elif "mask" in name:
            layer_name = f"{label} - Building change mask"
            layer = QgsRasterLayer(path, layer_name)
            visible = default_layer_visibility("building_change_mask", is_baseline=is_baseline)
            _set_layer_metadata(layer, milestone, project_id, "building_change_mask", visible, controller)
            if _add_layer(layer, group, visible=visible, controller=controller):
                style_mask_raster(layer)
                added.append(layer_name)
                _log(
                    "QGIS_RASTER_ARTIFACT_LAYER_ADD_DONE "
                    f"projectId={project_id} releaseIdentifier={release_id} artifactKey={artifact_key} "
                    f"layerName={layer_name} visible={visible} renderingStrategy=raster"
                )
            else:
                _log(
                    "QGIS_RASTER_ARTIFACT_LAYER_ADD_FAILED "
                    f"projectId={project_id} releaseIdentifier={release_id} artifactKey={artifact_key} "
                    f"layerName={layer_name} path={path} renderingStrategy=raster",
                    Qgis.Warning,
                )
        else:
            _log(
                "QGIS_RASTER_ARTIFACT_SKIPPED "
                f"projectId={project_id} releaseIdentifier={release_id} artifactKey={artifact_key} "
                f"reason=not_raster_artifact name={name} metadataKeys={artifact_metadata_keys}",
                Qgis.Info,
            )
    _log(
        "QGIS_RASTER_ARTIFACTS_LOAD_DONE "
        f"projectId={project_id} releaseIdentifier={release_id} addedCount={len(added)}"
    )
    return added


def _load_milestone_artifact_vectors(
    milestone: Dict[str, Any],
    label: str,
    client: BackendClient,
    group,
    controller: TemporalLayerController,
    project_id: str,
    is_baseline: bool,
    milestone_color_map: Optional[Dict[str, str]] = None,
) -> List[str]:
    added: List[str] = []
    artifacts = milestone.get("artifacts") or []
    release_id = milestone_release_identifier(milestone)
    _log(
        "QGIS_DETECTION_RESULTS_LOAD_START "
        f"projectId={project_id} releaseIdentifier={release_id} artifactCount={len(artifacts)} "
        f"hasMilestoneColorMap={bool(milestone_color_map)}"
    )
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        artifact_key = str(artifact.get("key") or "")
        artifact_metadata_keys = ",".join(sorted(str(key) for key in artifact.keys()))
        _log(
            "QGIS_DETECTION_ARTIFACT_INSPECT "
            f"projectId={project_id} releaseIdentifier={release_id} artifactKey={artifact_key} "
            f"metadataKeys={artifact_metadata_keys}"
        )
        spec = ARTIFACT_VECTOR_SPECS.get(artifact_key)
        if spec is None:
            _log(
                "QGIS_DETECTION_ARTIFACT_SKIPPED "
                f"projectId={project_id} releaseIdentifier={release_id} artifactKey={artifact_key} "
                f"reason=unsupported_artifact metadataKeys={artifact_metadata_keys}",
                Qgis.Info,
            )
            continue
        if artifact_key not in DEFAULT_ARTIFACT_VECTOR_KEYS:
            _log(
                "QGIS_OPTIONAL_LAYER_DEFERRED "
                f"projectId={project_id} releaseIdentifier={release_id} "
                f"artifactKey={artifact_key} reason=optional_not_loaded_by_default metadataKeys={artifact_metadata_keys}"
            )
            continue
        layer_key, title, inline_field_path, style_fn = spec
        style_fn = _temporal_style_fn(release_id, artifact_key, milestone_color_map or {}, style_fn)
        layer_name = _artifact_layer_name(artifact_key, title, milestone)
        if _artifact_is_empty(artifact):
            _log(
                "QGIS_DETECTION_ARTIFACT_SKIPPED "
                f"projectId={project_id} releaseIdentifier={release_id} "
                f"artifactKey={artifact_key} reason=empty_artifact metadataKeys={artifact_metadata_keys}",
                Qgis.Info,
            )
            continue
        if _has_inline_payload(milestone, inline_field_path):
            _log(
                "QGIS_DETECTION_ARTIFACT_SKIPPED "
                f"projectId={project_id} releaseIdentifier={release_id} "
                f"artifactKey={artifact_key} reason=inline_payload_already_loaded metadataKeys={artifact_metadata_keys}",
                Qgis.Info,
            )
            continue
        source_info = _artifact_vector_source(artifact, client)
        if source_info is None:
            _log(
                "QGIS_DETECTION_ARTIFACT_SKIPPED "
                f"projectId={project_id} releaseIdentifier={release_id} "
                f"artifactKey={artifact_key} reason=missing_geojson_source metadataKeys={artifact_metadata_keys}",
                Qgis.Warning,
            )
            continue
        source, selected_format = source_info
        _log(
            "QGIS_DETECTION_ARTIFACT_DISCOVERED "
            f"projectId={project_id} releaseIdentifier={release_id} "
            f"artifactKey={artifact_key} source={source} renderingStrategy={selected_format} "
            f"featureCount={artifact.get('feature_count')} metadataKeys={artifact_metadata_keys}"
        )
        cached_source = _local_cached_artifact_source(
            artifact,
            client,
            project_id=project_id,
            release_identifier=release_id,
            artifact_key=artifact_key,
            source=source,
            selected_format=selected_format,
        )
        _log(
            "QGIS_DETECTION_VECTOR_LAYER_ADD_START "
            f"projectId={project_id} releaseIdentifier={release_id} "
            f"artifactKey={artifact_key} layerName={layer_name} source={cached_source} renderingStrategy={selected_format}"
        )
        visible = default_layer_visibility(layer_key, is_baseline=is_baseline)
        layer = QgsVectorLayer(cached_source, layer_name, "ogr")
        _set_layer_metadata(layer, milestone, project_id, layer_key, visible, controller)
        if not _add_layer(layer, group, visible=visible, controller=controller):
            _log(
                "QGIS_DETECTION_VECTOR_LAYER_ADD_FAILED "
                f"projectId={project_id} releaseIdentifier={release_id} "
                f"artifactKey={artifact_key} layerName={layer_name} source={cached_source} renderingStrategy={selected_format}",
                Qgis.Warning,
            )
            continue
        style_fn(layer)
        _apply_scale_visibility(layer, artifact_key)
        _log(
            "QGIS_DETECTION_VECTOR_LAYER_ADD_DONE "
            f"projectId={project_id} releaseIdentifier={release_id} "
            f"artifactKey={artifact_key} layerName={layer_name} visible={visible} "
            f"source={cached_source} renderingStrategy={selected_format}"
        )
        _log(f"QGIS_DEFAULT_LAYER_SELECTED projectId={project_id} releaseIdentifier={release_id} artifactKey={artifact_key}")
        added.append(layer_name)
    _log(
        "QGIS_DETECTION_RESULTS_LOAD_DONE "
        f"projectId={project_id} releaseIdentifier={release_id} addedCount={len(added)}"
    )
    _log(
        "QGIS_DEFAULT_LAYER_LOAD_DONE "
        f"projectId={project_id} releaseIdentifier={release_id} addedCount={len(added)}"
    )
    return added


def _load_geojson_payload(
    payload: Any,
    path: Path,
    layer_name: str,
    style_fn: StyleFn,
    group,
    visible: bool,
    milestone: Dict[str, Any],
    project_id: str,
    layer_key: str,
    controller: TemporalLayerController,
) -> bool:
    if not _has_features(payload):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    layer = QgsVectorLayer(str(path), layer_name, "ogr")
    _set_layer_metadata(layer, milestone, project_id, layer_key, visible, controller)
    if not _add_layer(layer, group, visible=visible, controller=controller):
        _log(f"BUILDING_CHANGE_PLUGIN_LAYER_MISSING name={layer_name} reason=qgis_invalid_geojson source={path}", Qgis.Warning)
        return False
    style_fn(layer)
    _log(f"BUILDING_CHANGE_PLUGIN_LAYER_LOADED name={layer_name}")
    return True


def _has_features(payload: Any) -> bool:
    return isinstance(payload, dict) and isinstance(payload.get("features"), list) and bool(payload["features"])


def _has_inline_payload(milestone: Dict[str, Any], field_path: str) -> bool:
    if "." not in field_path:
        return _has_features(milestone.get(field_path))
    root_key, child_key = field_path.split(".", 1)
    root = milestone.get(root_key)
    return isinstance(root, dict) and _has_features(root.get(child_key))


def _artifact_is_empty(artifact: Dict[str, Any]) -> bool:
    feature_count = artifact.get("feature_count")
    if feature_count is None:
        feature_count = artifact.get("featureCount")
    try:
        return feature_count is not None and int(feature_count) == 0
    except (TypeError, ValueError):
        return False


def _artifact_keys_for_log(milestone: Dict[str, Any]) -> str:
    keys: List[str] = []
    for artifact in milestone.get("artifacts") or []:
        if not isinstance(artifact, dict):
            continue
        key = artifact.get("key") or artifact.get("name")
        if key:
            keys.append(str(key))
    return ",".join(keys)


def _artifact_vector_source(artifact: Dict[str, Any], client: BackendClient) -> Optional[Tuple[str, str]]:
    if artifact.get("media_type") != "application/geo+json":
        return None
    preferred_format = str(artifact.get("qgis_preferred_format") or artifact.get("qgisPreferredFormat") or "").lower()
    preferred_url = artifact.get("qgis_preferred_url") or artifact.get("qgisPreferredUrl")
    if preferred_format == "gpkg" and isinstance(preferred_url, str) and preferred_url:
        return _absolute_artifact_url(preferred_url, client), "gpkg"
    for key in ("gpkg_url", "gpkgUrl"):
        value = artifact.get(key)
        if isinstance(value, str) and value:
            return _absolute_artifact_url(value, client), "gpkg"
    path = artifact.get("path")
    for key in ("geojson_url", "geojsonUrl", "download_url", "downloadUrl", "artifact_url", "artifactUrl", "url"):
        value = artifact.get(key)
        if not isinstance(value, str) or not value:
            continue
        return _absolute_artifact_url(value, client), "geojson"
    if isinstance(path, str) and Path(path).is_file():
        return path, _artifact_format_from_source(path)
    if isinstance(path, str) and path:
        return path, _artifact_format_from_source(path)
    return None


def _absolute_artifact_url(value: str, client: BackendClient) -> str:
    if value.startswith(("http://", "https://", "file://")):
        return value
    return client.absolute_url(value)


def _artifact_format_from_source(source: str) -> str:
    lowered = source.lower()
    if lowered.endswith(".gpkg"):
        return "gpkg"
    return "geojson"


def _local_cached_artifact_source(
    artifact: Dict[str, Any],
    client: BackendClient,
    *,
    project_id: str,
    release_identifier: str,
    artifact_key: str,
    source: str,
    selected_format: str,
) -> str:
    if not source.startswith(("http://", "https://")):
        path = Path(source.removeprefix("file://"))
        if path.is_file():
            _log(
                "QGIS_ARTIFACT_CACHE_HIT "
                f"projectId={project_id} releaseIdentifier={release_identifier} artifactKey={artifact_key} "
                f"format={selected_format} local_path={path} size_bytes={path.stat().st_size} reason=local_source"
            )
            return str(path)
    version = str(
        artifact.get("qgis_cache_key")
        or artifact.get("qgisCacheKey")
        or f"{artifact.get('source_mtime_ns') or artifact.get('sourceMtimeNs') or 'unknown'}-{artifact.get('size_bytes') or artifact.get('sizeBytes') or 'unknown'}"
    )
    extension = "gpkg" if selected_format == "gpkg" else "geojson"
    cache_path = (
        Path.home()
        / "Library"
        / "Application Support"
        / "QGIS"
        / "QGIS3"
        / "profiles"
        / "default"
        / "python"
        / "plugins"
        / "building_change_plugin"
        / "cache"
        / _safe_name(project_id)
        / _safe_name(release_identifier)
        / _safe_name(artifact_key)
        / _safe_name(version)
        / f"{_safe_name(artifact_key)}.{extension}"
    )
    _log(
        "QGIS_ARTIFACT_FORMAT_SELECTED "
        f"projectId={project_id} releaseIdentifier={release_identifier} artifactKey={artifact_key} "
        f"format={selected_format} url={source} local_path={cache_path}"
    )
    if cache_path.is_file() and cache_path.stat().st_size > 0:
        _log(
            "QGIS_ARTIFACT_CACHE_HIT "
            f"projectId={project_id} releaseIdentifier={release_identifier} artifactKey={artifact_key} "
            f"format={selected_format} url={source} local_path={cache_path} size_bytes={cache_path.stat().st_size}"
        )
        return str(cache_path)
    started_at = time.perf_counter()
    _log(
        "QGIS_ARTIFACT_CACHE_MISS "
        f"projectId={project_id} releaseIdentifier={release_identifier} artifactKey={artifact_key} "
        f"format={selected_format} url={source} local_path={cache_path}"
    )
    _log(
        "QGIS_ARTIFACT_DOWNLOAD_START "
        f"projectId={project_id} releaseIdentifier={release_identifier} artifactKey={artifact_key} "
        f"format={selected_format} url={source} local_path={cache_path}"
    )
    try:
        client.download_artifact(source, cache_path)
    except Exception as exc:
        _log(
            "QGIS_ARTIFACT_DOWNLOAD_FAILED "
            f"projectId={project_id} releaseIdentifier={release_identifier} artifactKey={artifact_key} "
            f"format={selected_format} url={source} local_path={cache_path} provider_error={exc}",
            Qgis.Warning,
        )
        raise
    _log(
        "QGIS_ARTIFACT_DOWNLOAD_DONE "
        f"projectId={project_id} releaseIdentifier={release_identifier} artifactKey={artifact_key} "
        f"format={selected_format} url={source} local_path={cache_path} "
        f"size_bytes={cache_path.stat().st_size} duration_ms={round((time.perf_counter() - started_at) * 1000, 2)}"
    )
    return str(cache_path)


def _apply_scale_visibility(layer, artifact_key: str) -> None:
    try:
        if artifact_key == "building_change_buffer_10m":
            layer.setScaleBasedVisibility(True)
            layer.setMinimumScale(25000)
            _log(f"QGIS_LAYER_SCALE_VISIBILITY_APPLIED layer={layer.name()} artifactKey={artifact_key} minimumScale=25000")
        _log(f"QGIS_LAYER_STYLE_APPLIED layer={layer.name()} artifactKey={artifact_key}")
    except Exception:
        _log(f"QGIS_LAYER_SCALE_VISIBILITY_APPLIED layer={getattr(layer, 'name', lambda: '')()} artifactKey={artifact_key} status=skipped", Qgis.Warning)


def _milestone_label(milestone: Dict[str, Any], index: int) -> str:
    date_value = milestone.get("release_date")
    if date_value:
        return str(date_value)[:10]
    return str(milestone.get("release_identifier") or f"milestone-{index}")


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "layer"


def _log(message: str, level=Qgis.Info) -> None:
    QgsMessageLog.logMessage(message, LOG_CATEGORY, level)


def _qgis_reference_source(cog_path: Path, layer_dir: Path, label: str) -> Path:
    alpha_vrt = _write_alpha_vrt_if_masked(cog_path, layer_dir / f"{_safe_name(label)}_reference_rgba.vrt")
    return alpha_vrt or cog_path


def _write_alpha_vrt_if_masked(cog_path: Path, vrt_path: Path) -> Path:
    try:
        from osgeo import gdal
    except Exception:
        return cog_path
    dataset = gdal.Open(str(cog_path))
    if dataset is None or dataset.RasterCount < 3:
        return cog_path
    first_band = dataset.GetRasterBand(1)
    has_dataset_mask = bool(first_band.GetMaskFlags() & gdal.GMF_PER_DATASET)
    if not has_dataset_mask:
        return cog_path
    projection = dataset.GetProjectionRef() or ""
    try:
        transform = dataset.GetGeoTransform(can_return_null=True)
    except TypeError:
        transform = dataset.GetGeoTransform()
    transform_text = ", ".join(f"{value:.16g}" for value in transform) if transform else None
    source = escape(str(cog_path))
    width = dataset.RasterXSize
    height = dataset.RasterYSize
    geo_transform_xml = f"  <GeoTransform>{transform_text}</GeoTransform>\n" if transform_text else ""
    vrt_path.parent.mkdir(parents=True, exist_ok=True)
    vrt_path.write_text(
        f"""<VRTDataset rasterXSize=\"{width}\" rasterYSize=\"{height}\">
  <SRS>{escape(projection)}</SRS>
{geo_transform_xml}  <VRTRasterBand dataType=\"Byte\" band=\"1\">
    <ColorInterp>Red</ColorInterp>
    <SimpleSource><SourceFilename relativeToVRT=\"0\">{source}</SourceFilename><SourceBand>1</SourceBand></SimpleSource>
  </VRTRasterBand>
  <VRTRasterBand dataType=\"Byte\" band=\"2\">
    <ColorInterp>Green</ColorInterp>
    <SimpleSource><SourceFilename relativeToVRT=\"0\">{source}</SourceFilename><SourceBand>2</SourceBand></SimpleSource>
  </VRTRasterBand>
  <VRTRasterBand dataType=\"Byte\" band=\"3\">
    <ColorInterp>Blue</ColorInterp>
    <SimpleSource><SourceFilename relativeToVRT=\"0\">{source}</SourceFilename><SourceBand>3</SourceBand></SimpleSource>
  </VRTRasterBand>
  <VRTRasterBand dataType=\"Byte\" band=\"4\">
    <ColorInterp>Alpha</ColorInterp>
    <SimpleSource><SourceFilename relativeToVRT=\"0\">{source}</SourceFilename><SourceBand>mask,1</SourceBand></SimpleSource>
  </VRTRasterBand>
</VRTDataset>
""",
        encoding="utf-8",
    )
    return vrt_path


def _set_layer_metadata(
    layer,
    milestone: Dict[str, Any],
    project_id: str,
    layer_key: str,
    default_visible: bool,
    controller: TemporalLayerController,
) -> None:
    release_id = milestone_release_identifier(milestone)
    if release_id:
        layer.setCustomProperty("building_change/releaseIdentifier", str(release_id))
    release_date = milestone_date(milestone)
    if release_date:
        layer.setCustomProperty("building_change/releaseDate", str(release_date))
    controller.tag_layer(layer, release_identifier=release_id, release_date=release_date, layer_key=layer_key, default_visible=default_visible)


def _is_tile_template(value: str) -> bool:
    lowered = value.lower()
    return any(token in lowered for token in ("{z}", "{x}", "{y}", "%7bz%7d", "%7bx%7d", "%7by%7d"))


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


def _buffer_style(label: str) -> StyleFn:
    if label == "15 m":
        return style_buffer_15
    if label == "20 m":
        return style_buffer_20
    return style_buffer


def _temporal_style_fn(
    release_identifier: str,
    artifact_key: str,
    milestone_color_map: Dict[str, str],
    fallback_style_fn: StyleFn,
) -> StyleFn:
    if artifact_key not in {"additions", "building_change_buffer_10m", "building_change_buffer_15m", "building_change_buffer_20m"}:
        return fallback_style_fn

    def _apply(layer: QgsVectorLayer) -> None:
        style_temporal_artifact(layer, temporal_style_for_artifact(release_identifier, artifact_key, milestone_color_map or {}))

    return _apply


def _buffer_layer_key(label: str) -> str:
    if label == "15 m":
        return BUFFER_15_LAYER_KEY
    if label == "20 m":
        return BUFFER_20_LAYER_KEY
    return BUFFER_10_LAYER_KEY


def _buffer_artifact_key(label: str) -> str:
    if label == "15 m":
        return "building_change_buffer_15m"
    if label == "20 m":
        return "building_change_buffer_20m"
    return "building_change_buffer_10m"


def _buffer_distance_m(label: str) -> int:
    if label == "15 m":
        return 15
    if label == "20 m":
        return 20
    return 10


def _artifact_layer_name(artifact_key: str, fallback_title: str, milestone: Dict[str, Any]) -> str:
    if artifact_key == "additions":
        return additions_label(milestone)
    if artifact_key == "building_change_buffer_15m":
        return buffer_label(15, milestone)
    if artifact_key == "building_change_buffer_20m":
        return buffer_label(20, milestone)
    if artifact_key == "building_change_buffer_10m":
        return buffer_label(10, milestone)
    return fallback_title
