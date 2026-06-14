from __future__ import annotations

from pathlib import Path
import re

from building_change_plugin.models import (
    _artifact_geojson_source,
    build_temporal_project_payload,
    clean_temporal_project_summaries,
    discover_temporal_layer_candidates,
    normalize_aoi_geojson_geometry,
    project_display_label,
    release_display_label,
    release_identifier,
    sorted_unique_releases,
)
from building_change_plugin.layer_controller import (
    ADDITIONS_LAYER_KEY,
    BUFFER_10_LAYER_KEY,
    BUFFER_15_LAYER_KEY,
    BUFFER_20_LAYER_KEY,
    CUMULATIVE_GROWTH_LAYER_KEY,
    DEFAULT_VISIBILITY_KEY,
    DIAGNOSTICS_LAYER_KEY,
    MILESTONE_GROUP_KEY,
    PROJECT_KEY,
    REFERENCE_LAYER_KEY,
    RELEASE_KEY,
    TemporalLayerController,
    default_layer_visibility,
    select_active_milestone,
    sorted_milestones_newest_first,
)
from building_change_plugin.temporal_colors import (
    additions_label,
    all_previous_additions_label,
    buffer_label,
    cumulative_buffer_label,
    get_milestone_color_map,
    temporal_style_for_artifact,
)


PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "building_change_plugin"


def test_plugin_metadata_declares_normal_plugin() -> None:
    metadata = (PLUGIN_ROOT / "metadata.txt").read_text(encoding="utf-8")

    assert "hasProcessingProvider=False" in metadata


def test_plugin_source_does_not_expose_model_selection() -> None:
    forbidden = ("model" + "_backend", "inference" + "_backend")
    for path in PLUGIN_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not any(value in text for value in forbidden), path


def test_plugin_source_uses_backend_api_client() -> None:
    text = (PLUGIN_ROOT / "api_client.py").read_text(encoding="utf-8")

    assert "/api/health" in text
    assert "/api/temporal-projects" in text
    assert "/api/jobs/temporal-projects/" in text


def test_project_dropdown_filters_cached_pairwise_runs() -> None:
    payload = [
        {
            "project_id": "temporal-1",
            "name": "Casablanca",
            "updated_at": "2026-05-13T10:00:00Z",
            "milestones": [{"release_identifier": "WB_2018_R01"}, {"release_identifier": "WB_2020_R01"}],
        },
        {
            "project_id": "temporal-1",
            "name": "Casablanca duplicate",
            "updated_at": "2026-05-13T11:00:00Z",
            "milestones": [{"release_identifier": "WB_2018_R01"}, {"release_identifier": "WB_2020_R01"}],
        },
        {
            "project_id": "pairwise-1",
            "project_kind": "pairwise",
            "display_name": "Pairwise · WB_2014_R01 → WB_2026_R04",
        },
        {
            "project_id": "run-1",
            "run_id": "abc",
            "display_name": "Temporal mosaic · 65",
        },
    ]

    projects = clean_temporal_project_summaries(payload)

    assert [project["project_id"] for project in projects] == ["temporal-1"]
    assert project_display_label(projects[0]) == "Casablanca · WB_2018_R01 → WB_2020_R01"


def test_release_dropdown_uses_release_identifier() -> None:
    release = {"releaseIdentifier": "WB_2024_R03", "release_date": "2024-07-14"}

    assert release_identifier(release) == "WB_2024_R03"
    assert release_display_label(release) == "2024-07-14 · WB_2024_R03"


def test_project_payload_does_not_send_model_selection_fields() -> None:
    forbidden_model_field = "model" + "_backend"
    forbidden_inference_field = "inference" + "_backend"
    forbidden_segmentation_field = "segmentation" + "_backend"
    payload = build_temporal_project_payload(
        name="Projet QGIS",
        aoi_geojson=_polygon(),
        releases=[
            {"identifier": "WB_2018_R01", "release_date": "2018-01-01"},
            {"identifier": "WB_2020_R01", "release_date": "2020-01-01"},
        ],
    )
    text = repr(payload)

    assert forbidden_model_field not in text
    assert forbidden_inference_field not in text
    assert forbidden_segmentation_field not in text


def test_polygon_aoi_passes_unchanged() -> None:
    polygon = _polygon()

    assert normalize_aoi_geojson_geometry(polygon) == polygon


def test_feature_aoi_normalizes_to_polygon() -> None:
    normalized = normalize_aoi_geojson_geometry({"type": "Feature", "properties": {}, "geometry": _polygon()})

    assert normalized["type"] == "Polygon"


def test_single_polygon_feature_collection_normalizes_to_polygon() -> None:
    normalized = normalize_aoi_geojson_geometry(
        {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": _polygon()}]}
    )

    assert normalized["type"] == "Polygon"


def test_multiple_polygon_feature_collection_normalizes_to_multipolygon() -> None:
    normalized = normalize_aoi_geojson_geometry(
        {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "properties": {}, "geometry": _polygon()},
                {"type": "Feature", "properties": {}, "geometry": _polygon(offset=2.0)},
            ],
        }
    )

    assert normalized["type"] == "MultiPolygon"
    assert len(normalized["coordinates"]) == 2


def test_point_and_line_aoi_are_rejected() -> None:
    for geometry in (
        {"type": "Point", "coordinates": [0.0, 0.0]},
        {"type": "LineString", "coordinates": [[0.0, 0.0], [1.0, 1.0]]},
    ):
        try:
            normalize_aoi_geojson_geometry(geometry)
        except ValueError:
            pass
        else:
            raise AssertionError("Invalid AOI geometry was accepted: %s" % geometry["type"])


def test_payload_contains_geometry_aoi_only() -> None:
    payload = build_temporal_project_payload(
        name="Projet QGIS",
        aoi_geojson={"type": "Feature", "properties": {}, "geometry": _polygon()},
        releases=[
            {"identifier": "WB_2018_R01", "release_date": "2018-01-01"},
            {"identifier": "WB_2020_R01", "release_date": "2020-01-01"},
        ],
    )

    assert payload["aoi_geojson"]["type"] == "Polygon"
    assert "FeatureCollection" not in repr(payload["aoi_geojson"])
    assert "Feature" not in repr(payload["aoi_geojson"])


def test_reference_imagery_cog_candidates_are_discovered() -> None:
    project = {
        "project_id": "qgis-test",
        "milestones": [
            {"release_identifier": "WB_2014_R01", "reference_imagery": {"cog_path": "/tmp/WB_2014_R01/reference_imagery_cog.tif"}},
            {"release_identifier": "WB_2026_R04", "reference_imagery": {"cog_path": "/tmp/WB_2026_R04/reference_imagery_cog.tif"}},
        ],
    }

    candidates = discover_temporal_layer_candidates(project)

    reference_candidates = [candidate for candidate in candidates if candidate["group"] == "reference"]
    assert len(reference_candidates) == 2
    assert reference_candidates[0]["kind"] == "raster"
    assert reference_candidates[0]["name"] == "WB_2014_R01 - reference imagery"


def test_tile_template_is_not_a_loadable_candidate_set() -> None:
    project = {
        "project_id": "qgis-test",
        "milestones": [
            {
                "release_identifier": "WB_2014_R01",
                "reference_imagery": {"tiles_url_template": "/api/temporal-projects/qgis-test/milestones/WB_2014_R01/reference/tiles/{z}/{x}/{y}.png"},
            }
        ],
    }

    candidates = discover_temporal_layer_candidates(project)

    assert candidates == []


def test_run_nested_project_artifacts_are_discovered() -> None:
    project = {
        "project_id": "qgis-test",
        "runs": [
            {
                "result_run_id": "temporal-qgis-test",
                "project": {
                    "milestones": [
                        {
                            "release_identifier": "WB_2026_R04",
                            "reference_imagery": {"cog_path": "/tmp/reference_imagery_cog.tif"},
                            "artifacts": [{"name": "change_probability", "path": "/tmp/change_probability.tif"}],
                        }
                    ]
                },
            }
        ],
    }

    candidates = discover_temporal_layer_candidates(project)

    assert any(candidate["source"] == "/tmp/reference_imagery_cog.tif" for candidate in candidates)
    assert any(candidate["source"] == "/tmp/change_probability.tif" for candidate in candidates)


def test_missing_optional_layers_do_not_remove_reference_candidate() -> None:
    project = {
        "project_id": "qgis-test",
        "milestones": [
            {
                "release_identifier": "WB_2014_R01",
                "additions_geojson": {"type": "FeatureCollection", "features": []},
                "reference_imagery": {"cog_path": "/tmp/reference_imagery_cog.tif"},
            }
        ],
    }

    candidates = discover_temporal_layer_candidates(project)

    assert len(candidates) == 1
    assert candidates[0]["name"].endswith("reference imagery")


def test_layer_names_use_exact_date_without_release_prefix_when_available() -> None:
    project = {
        "project_id": "qgis-test",
        "milestones": [
            {
                "release_identifier": "WB_2026_R04",
                "release_date": "2026-04-30",
                "reference_imagery": {"cog_path": "/tmp/reference_imagery_cog.tif"},
            }
        ],
    }

    candidates = discover_temporal_layer_candidates(project)

    assert candidates[0]["name"] == "2026-04-30 - reference imagery"
    assert "WB_2026_R04" not in candidates[0]["name"]


def test_payload_accepts_more_than_two_sorted_milestones() -> None:
    payload = build_temporal_project_payload(
        name="Projet QGIS",
        aoi_geojson=_polygon(),
        releases=[
            {"identifier": "WB_2026_R04", "release_date": "2026-04-30"},
            {"identifier": "WB_2014_R01", "release_date": "2014-02-20"},
            {"identifier": "WB_2020_R04", "release_date": "2020-03-23"},
        ],
    )

    assert [milestone["release_identifier"] for milestone in payload["milestones"]] == [
        "WB_2014_R01",
        "WB_2020_R04",
        "WB_2026_R04",
    ]


def test_duplicate_milestones_are_deduplicated_before_payload() -> None:
    releases = sorted_unique_releases(
        [
            {"identifier": "WB_2026_R04", "release_date": "2026-04-30"},
            {"identifier": "WB_2026_R04", "release_date": "2026-04-30"},
            {"identifier": "WB_2014_R01", "release_date": "2014-02-20"},
        ]
    )

    assert [release["identifier"] for release in releases] == ["WB_2014_R01", "WB_2026_R04"]


def test_result_layer_filter_removes_unwanted_layers_and_keeps_meaningful_layers() -> None:
    project = {
        "project_id": "qgis-test",
        "milestones": [
            {
                "release_identifier": "WB_2026_R04",
                "release_date": "2026-04-30",
                "additions_geojson": _feature_collection(),
                "automated_additions_geojson": _feature_collection(),
                "effective_footprint_geojson": _feature_collection(),
                "cumulative_growth_blocks_geojson": _feature_collection(),
                "cumulative_union_geojson": _feature_collection(),
                "automated_candidate_footprint_geojson": _feature_collection(),
                "buffer_layers_geojson": {
                    "10m": _feature_collection(),
                    "15m": _feature_collection(),
                    "20m": _feature_collection(),
                    "25m": _feature_collection(),
                },
            }
        ],
    }

    names = [candidate["name"] for candidate in discover_temporal_layer_candidates(project)]

    assert any("Added building in 2026" in name for name in names)
    assert any("Cumulative growth" in name for name in names)
    assert any("Buffer 10m 2026" in name for name in names)
    assert any("Buffer 15m 2026" in name for name in names)
    assert any("Buffer 20m 2026" in name for name in names)
    assert any("Addition candidate diagnostics" in name for name in names)
    assert not any("Automated additions" in name for name in names)
    assert not any("Effective footprints" in name for name in names)
    assert not any("Cumulative growth blocks" in name for name in names)
    assert not any("buffer 25 m" in name for name in names)


def test_artifact_backed_temporal_vectors_are_discovered_for_qgis() -> None:
    project = {
        "project_id": "temporal-tanger",
        "milestones": [
            {
                "release_identifier": "WB_2026_R04",
                "release_date": "2026-04-30",
                "artifacts": [
                    {
                        "key": "additions",
                        "media_type": "application/geo+json",
                        "geojson_url": "/api/temporal-projects/temporal-tanger/milestones/WB_2026_R04/artifacts/additions.geojson",
                        "gpkg_url": "/api/temporal-projects/temporal-tanger/milestones/WB_2026_R04/artifacts/additions.gpkg",
                        "qgis_preferred_url": "/api/temporal-projects/temporal-tanger/milestones/WB_2026_R04/artifacts/additions.gpkg",
                        "qgis_preferred_format": "gpkg",
                        "tilejson_url": "/api/temporal-projects/temporal-tanger/milestones/WB_2026_R04/artifacts/additions/tilejson.json",
                    },
                    {
                        "key": "building_change_buffer_10m",
                        "media_type": "application/geo+json",
                        "download_url": "/api/temporal-projects/temporal-tanger/milestones/WB_2026_R04/artifacts/building_change_buffer_10m.geojson",
                        "tiles_url_template": "/api/temporal-projects/temporal-tanger/milestones/WB_2026_R04/artifacts/building_change_buffer_10m/tiles/{z}/{x}/{y}.mvt",
                    },
                ],
            }
        ],
    }

    candidates = discover_temporal_layer_candidates(project)

    assert {
        "2026-04-30 - Added building in 2026",
        "2026-04-30 - Buffer 10m 2026",
    }.issubset({candidate["name"] for candidate in candidates})
    assert all(candidate["kind"] == "geojson" for candidate in candidates)
    assert any(candidate["source"].endswith("/artifacts/additions.gpkg") for candidate in candidates)


def test_artifact_backed_temporal_vectors_fall_back_to_geojson() -> None:
    project = {
        "project_id": "temporal-tanger",
        "milestones": [
            {
                "release_identifier": "WB_2026_R04",
                "release_date": "2026-04-30",
                "artifacts": [
                    {
                        "key": "additions",
                        "media_type": "application/geo+json",
                        "geojson_url": "/api/temporal-projects/temporal-tanger/milestones/WB_2026_R04/artifacts/additions.geojson",
                    }
                ],
            }
        ],
    }

    candidates = discover_temporal_layer_candidates(project)

    assert candidates[0]["source"].endswith("/artifacts/additions.geojson")


def test_empty_artifact_backed_temporal_vectors_are_skipped() -> None:
    project = {
        "project_id": "temporal-baseline",
        "milestones": [
            {
                "release_identifier": "WB_2020_R04",
                "release_date": "2020-03-23",
                "artifacts": [
                    {
                        "key": "additions",
                        "media_type": "application/geo+json",
                        "geojson_url": "/api/temporal-projects/temporal-baseline/milestones/WB_2020_R04/artifacts/additions.geojson",
                        "feature_count": 0,
                    }
                ],
            }
        ],
    }

    assert discover_temporal_layer_candidates(project) == []


def test_active_display_date_maps_to_release_identifier() -> None:
    project = {
        "project_id": "temporal-tanger",
        "milestones": [
            {"release_identifier": "WB_2023_R02", "release_date": "2023-03-15"},
            {"release_identifier": "WB_2026_R04", "release_date": "2026-04-30"},
        ],
    }

    milestone, reason = select_active_milestone(project, requested_display_date="2026-04-30")

    assert reason == "requested"
    assert milestone["release_identifier"] == "WB_2026_R04"


def test_active_milestone_fallback_picks_latest_non_baseline_only() -> None:
    project = {
        "project_id": "temporal-tanger",
        "milestones": [
            {"release_identifier": "WB_2020_R04", "release_date": "2020-03-23"},
            {"release_identifier": "WB_2024_R02", "release_date": "2024-03-07"},
            {"release_identifier": "WB_2026_R04", "release_date": "2026-04-30"},
        ],
    }

    milestone, reason = select_active_milestone(project)

    assert reason == "latest_non_baseline"
    assert milestone["release_identifier"] == "WB_2026_R04"


def test_gpkg_url_preference_order_for_qgis_artifacts() -> None:
    source = _artifact_geojson_source(
        {
            "key": "additions",
            "media_type": "application/geo+json",
            "geojson_url": "/artifacts/additions.geojson",
            "gpkg_url": "/artifacts/additions.gpkg",
            "qgis_preferred_url": "/artifacts/additions-preferred.gpkg",
            "qgis_preferred_format": "gpkg",
        }
    )

    assert source.endswith("/artifacts/additions-preferred.gpkg")


def test_completed_job_polling_reloads_project_before_layer_loading() -> None:
    text = (PLUGIN_ROOT / "tasks.py").read_text(encoding="utf-8")

    assert 'status == "completed"' in text
    assert "return self.client.get_temporal_project(project_id)" in text
    assert "raw_result" not in text


def test_layer_loader_places_detection_before_reference_and_blocks_tile_template() -> None:
    text = (PLUGIN_ROOT / "layer_loader.py").read_text(encoding="utf-8")

    assert 'milestone_group.addGroup("Detection results")' in text
    assert 'milestone_group.addGroup("Reference imagery")' in text
    assert "INVALID_TILE_TEMPLATE_REQUEST_BLOCKED" in text
    assert "type=xyz" not in text
    assert 'DEFAULT_ARTIFACT_VECTOR_KEYS = {"additions", "building_change_buffer_10m"}' in text
    assert "QGIS_OPTIONAL_LAYER_DEFERRED" in text
    assert "QGIS_ARTIFACT_CACHE_HIT" in text
    assert "QGIS_ACTIVE_MILESTONE_SELECTED" in text
    assert "QGIS_ACTIVE_MILESTONE_FALLBACK" in text
    assert "QGIS_LOAD_LAYERS_SELECTED_MILESTONES" in text
    assert "QGIS_MILESTONE_LOAD_START" in text
    assert "QGIS_MILESTONE_DETECTION_LOAD_DONE" in text
    assert "QGIS_MILESTONE_REFERENCE_LOAD_DONE" in text
    assert "QGIS_MILESTONE_VISIBILITY_SET" in text
    assert "QGIS_INACTIVE_MILESTONE_RESULTS_DEFERRED" not in text
    assert "QGIS_INACTIVE_MILESTONE_REFERENCE_DEFERRED" not in text
    assert "is_active_milestone" in text


def test_temporal_milestone_groups_sort_newest_first() -> None:
    milestones = [
        {"release_identifier": "WB_2020_R04", "release_date": "2020-03-23"},
        {"release_identifier": "WB_2026_R04", "release_date": "2026-04-30"},
        {"release_identifier": "WB_2016_R06", "release_date": "2016-03-16"},
    ]

    sorted_milestones = sorted_milestones_newest_first(milestones)

    assert [milestone["release_identifier"] for milestone in sorted_milestones] == [
        "WB_2026_R04",
        "WB_2020_R04",
        "WB_2016_R06",
    ]


def test_temporal_default_visibility_rules() -> None:
    assert default_layer_visibility(REFERENCE_LAYER_KEY) is True
    assert default_layer_visibility(ADDITIONS_LAYER_KEY) is True
    assert default_layer_visibility(BUFFER_10_LAYER_KEY) is True
    assert default_layer_visibility(CUMULATIVE_GROWTH_LAYER_KEY) is False
    assert default_layer_visibility(BUFFER_15_LAYER_KEY) is False
    assert default_layer_visibility(BUFFER_20_LAYER_KEY) is False
    assert default_layer_visibility(DIAGNOSTICS_LAYER_KEY) is False
    assert default_layer_visibility(ADDITIONS_LAYER_KEY, is_baseline=True) is False
    assert default_layer_visibility(BUFFER_10_LAYER_KEY, is_baseline=True) is False


def test_temporal_controller_group_toggle_syncs_result_layers() -> None:
    controller = TemporalLayerController("project-1")
    group_node = _FakeNode(project_id="project-1", release_id="WB_2026_R04", layer_key="", visible=True)
    reference_node = _FakeLayerNode(project_id="project-1", release_id="WB_2026_R04", layer_key=REFERENCE_LAYER_KEY, visible=True)
    additions_node = _FakeLayerNode(project_id="project-1", release_id="WB_2026_R04", layer_key=ADDITIONS_LAYER_KEY, visible=True)
    buffer_node = _FakeLayerNode(project_id="project-1", release_id="WB_2026_R04", layer_key=BUFFER_10_LAYER_KEY, visible=True)
    optional_node = _FakeLayerNode(project_id="project-1", release_id="WB_2026_R04", layer_key=BUFFER_15_LAYER_KEY, visible=False)
    controller._matching_layer_nodes = lambda release_id: [reference_node, additions_node, buffer_node, optional_node]

    group_node.setItemVisibilityChecked(False)
    controller._on_visibility_changed(group_node)

    assert reference_node.itemVisibilityChecked() is False
    assert additions_node.itemVisibilityChecked() is False
    assert buffer_node.itemVisibilityChecked() is False
    assert optional_node.itemVisibilityChecked() is False

    group_node.setItemVisibilityChecked(True)
    controller._on_visibility_changed(group_node)

    assert reference_node.itemVisibilityChecked() is True
    assert additions_node.itemVisibilityChecked() is True
    assert buffer_node.itemVisibilityChecked() is True
    assert optional_node.itemVisibilityChecked() is False


def test_temporal_controller_reference_toggle_does_not_hide_results() -> None:
    controller = TemporalLayerController("project-1")
    reference_node = _FakeLayerNode(project_id="project-1", release_id="WB_2026_R04", layer_key=REFERENCE_LAYER_KEY, visible=True)
    additions_node = _FakeLayerNode(project_id="project-1", release_id="WB_2026_R04", layer_key=ADDITIONS_LAYER_KEY, visible=True)
    buffer_node = _FakeLayerNode(project_id="project-1", release_id="WB_2026_R04", layer_key=BUFFER_10_LAYER_KEY, visible=True)
    controller._matching_layer_nodes = lambda release_id: [reference_node, additions_node, buffer_node]

    reference_node.setItemVisibilityChecked(False)
    controller._on_visibility_changed(reference_node)

    assert reference_node.itemVisibilityChecked() is False
    assert additions_node.itemVisibilityChecked() is True
    assert buffer_node.itemVisibilityChecked() is True


def test_temporal_controller_restores_user_optional_layer_choice() -> None:
    controller = TemporalLayerController("project-1")
    group_node = _FakeNode(project_id="project-1", release_id="WB_2026_R04", layer_key="", visible=True)
    optional_node = _FakeLayerNode(project_id="project-1", release_id="WB_2026_R04", layer_key=BUFFER_15_LAYER_KEY, visible=True)
    controller._matching_layer_nodes = lambda release_id: [optional_node]

    controller._on_visibility_changed(optional_node)
    group_node.setItemVisibilityChecked(False)
    controller._on_visibility_changed(group_node)
    group_node.setItemVisibilityChecked(True)
    controller._on_visibility_changed(group_node)

    assert optional_node.itemVisibilityChecked() is True


def test_temporal_controller_assigns_layer_metadata() -> None:
    controller = TemporalLayerController("project-1")
    layer = _FakeLayer(project_id="", release_id="", layer_key="")

    controller.tag_layer(layer, release_identifier="WB_2026_R04", release_date="2026-04-30", layer_key=ADDITIONS_LAYER_KEY, default_visible=True)

    assert layer.customProperty(PROJECT_KEY) == "project-1"
    assert layer.customProperty(RELEASE_KEY) == "WB_2026_R04"
    assert layer.customProperty("building_change/date") == "2026-04-30"
    assert layer.customProperty("building_change/layer_key") == ADDITIONS_LAYER_KEY
    assert layer.customProperty(MILESTONE_GROUP_KEY) == "project-1:WB_2026_R04"
    assert layer.customProperty(DEFAULT_VISIBILITY_KEY) == "1"


def test_temporal_qgis_colors_match_frontend_release_order() -> None:
    colors = get_milestone_color_map(_tanger_temporal_milestones())

    assert colors["WB_2026_R04"] == "#B91C1C"
    assert colors["WB_2025_R03"] == "#1D4ED8"
    assert colors["WB_2024_R02"] == "#C2410C"
    assert colors["WB_2023_R02"] == "#6D28D9"


def test_temporal_qgis_additions_styles_are_release_aware_and_opaque() -> None:
    colors = get_milestone_color_map(_tanger_temporal_milestones())
    expected = {
        "WB_2023_R02": "#6D28D9",
        "WB_2024_R02": "#C2410C",
        "WB_2025_R03": "#1D4ED8",
        "WB_2026_R04": "#B91C1C",
    }

    styles = {
        release_id: temporal_style_for_artifact(release_id, "additions", colors)
        for release_id in expected
    }

    assert {release_id: style.fill_color for release_id, style in styles.items()} == expected
    assert len({style.fill_color for style in styles.values()}) == len(expected)
    for style in styles.values():
        assert style.fill_opacity == 1.0
        assert style.outline_opacity == 1.0
        assert _is_saturated_not_pale(style.fill_color)


def test_temporal_qgis_buffer_styles_are_release_aware_and_opaque() -> None:
    colors = get_milestone_color_map(_tanger_temporal_milestones())
    expected = {
        "WB_2023_R02": "#6D28D9",
        "WB_2024_R02": "#C2410C",
        "WB_2025_R03": "#1D4ED8",
        "WB_2026_R04": "#B91C1C",
    }

    for artifact_key in ("building_change_buffer_10m", "building_change_buffer_15m", "building_change_buffer_20m"):
        styles = {
            release_id: temporal_style_for_artifact(release_id, artifact_key, colors)
            for release_id in expected
        }

        assert {release_id: style.fill_color for release_id, style in styles.items()} == expected
        assert len({style.fill_color for style in styles.values()}) == len(expected)
        for style in styles.values():
            assert style.fill_opacity == 1.0
            assert style.outline_opacity == 0.0
            assert _is_saturated_not_pale(style.fill_color)


def test_temporal_qgis_layer_labels_match_frontend_format() -> None:
    milestones = _tanger_temporal_milestones()

    assert all_previous_additions_label(milestones, "WB_2026_R04") == "All new buildings 2020 -> 2026"
    assert additions_label({"release_identifier": "WB_2023_R02", "release_date": "2023-03-15"}) == "Added building in 2023"
    assert buffer_label(10, {"release_identifier": "WB_2023_R02", "release_date": "2023-03-15"}) == "Buffer 10m 2023"
    assert buffer_label(15, {"release_identifier": "WB_2023_R02", "release_date": "2023-03-15"}) == "Buffer 15m 2023"
    assert buffer_label(20, {"release_identifier": "WB_2023_R02", "release_date": "2023-03-15"}) == "Buffer 20m 2023"
    assert cumulative_buffer_label(10, milestones, "WB_2026_R04") == "Buffer 10m 2020 -> 2026"


def test_temporal_qgis_unknown_release_and_artifact_fall_back_safely() -> None:
    colors = get_milestone_color_map(_tanger_temporal_milestones())

    unknown_release = temporal_style_for_artifact("WB_2030_R01", "additions", colors)
    unknown_artifact = temporal_style_for_artifact("WB_2026_R04", "unexpected_layer", colors)

    assert unknown_release.fill_color == "#B91C1C"
    assert unknown_release.fill_opacity == 1.0
    assert unknown_artifact.fill_color == "#B91C1C"
    assert unknown_artifact.fill_opacity == 1.0


def test_plugin_source_has_no_python_310_union_syntax() -> None:
    union_pattern = re.compile(r"(:|->)\s*[^=\n#]*\s\|\s[^=\n#]*")
    for path in PLUGIN_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert union_pattern.search(text) is None, path


def _polygon(offset: float = 0.0):
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [offset, 0.0],
                [offset + 1.0, 0.0],
                [offset + 1.0, 1.0],
                [offset, 1.0],
                [offset, 0.0],
            ]
        ],
    }


def _feature_collection():
    return {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": _polygon(), "properties": {}}]}


def _tanger_temporal_milestones():
    return [
        {"release_identifier": "WB_2020_R04", "release_date": "2020-03-23"},
        {"release_identifier": "WB_2023_R02", "release_date": "2023-03-15"},
        {"release_identifier": "WB_2024_R02", "release_date": "2024-03-07"},
        {"release_identifier": "WB_2025_R03", "release_date": "2025-04-30"},
        {"release_identifier": "WB_2026_R04", "release_date": "2026-04-30"},
    ]


def _is_saturated_not_pale(color: str) -> bool:
    red = int(color[1:3], 16) / 255
    green = int(color[3:5], 16) / 255
    blue = int(color[5:7], 16) / 255
    max_channel = max(red, green, blue)
    min_channel = min(red, green, blue)
    lightness = (max_channel + min_channel) / 2
    saturation = 0 if max_channel == min_channel else (max_channel - min_channel) / (1 - abs(2 * lightness - 1))
    return saturation >= 0.55 and lightness <= 0.55


class _FakeLayer:
    def __init__(self, project_id: str, release_id: str, layer_key: str) -> None:
        self._properties = {
            "building_change/project_id": project_id,
            "building_change/releaseIdentifier": release_id,
            "building_change/layer_key": layer_key,
        }

    def customProperty(self, key: str, default: str = "") -> str:
        return self._properties.get(key, default)

    def setCustomProperty(self, key: str, value: str) -> None:
        self._properties[key] = value


class _FakeNode:
    def __init__(self, project_id: str, release_id: str, layer_key: str, visible: bool) -> None:
        self._visible = visible
        self._properties = {
            "building_change/project_id": project_id,
            "building_change/releaseIdentifier": release_id,
            "building_change/layer_key": layer_key,
        }

    def itemVisibilityChecked(self) -> bool:
        return self._visible

    def setItemVisibilityChecked(self, visible: bool) -> None:
        self._visible = visible

    def customProperty(self, key: str, default: str = "") -> str:
        return self._properties.get(key, default)

    def setCustomProperty(self, key: str, value: str) -> None:
        self._properties[key] = value


class _FakeLayerNode(_FakeNode):
    def __init__(self, project_id: str, release_id: str, layer_key: str, visible: bool) -> None:
        super().__init__(project_id, release_id, layer_key, visible)
        self._layer = _FakeLayer(project_id, release_id, layer_key)

    def layer(self) -> _FakeLayer:
        return self._layer
