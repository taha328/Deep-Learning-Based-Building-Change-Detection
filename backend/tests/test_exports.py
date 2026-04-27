import json

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.transform import from_bounds
from shapely.geometry import LineString, Polygon, shape

from src.domain.exports import export_bandon_outputs
from src.domain.vectorize import VectorizationContext, build_metric_buffer_layers, vectorize_new_buildings


def test_vector_export_returns_feature_collection(tmp_path) -> None:
    raster_path = tmp_path / "reference.tif"
    data = np.ones((8, 8), dtype=np.uint8)
    with rasterio.open(
        raster_path,
        "w",
        driver="GTiff",
        width=8,
        height=8,
        count=1,
        dtype="uint8",
        crs="EPSG:3857",
        transform=from_bounds(0, 0, 8, 8, 8, 8),
    ) as dst:
        dst.write(data, 1)

    df, geojson = vectorize_new_buildings(
        data.astype(bool),
        raster_path,
        VectorizationContext(
            release_t1="WB_2022_R01",
            release_t2="WB_2023_R01",
            src_date_t1="2022-01-01",
            src_date_t2="2023-01-01",
        ),
    )
    assert not df.empty
    assert geojson["type"] == "FeatureCollection"
    assert geojson["features"][0]["type"] == "Feature"


def test_metric_buffers_are_constrained_by_road_lines_without_holes(tmp_path) -> None:
    source_geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[
                        [-7.00000, 33.00000],
                        [-6.99990, 33.00000],
                        [-6.99990, 33.00010],
                        [-7.00000, 33.00010],
                        [-7.00000, 33.00000],
                    ]],
                },
                "properties": {"block_id": 1, "source_building_count": 1, "block_gap_m": 25.0},
            }
        ],
    }
    roads_path = tmp_path / "roads.geojson"
    roads_gdf = gpd.GeoDataFrame(
        geometry=[LineString([(-6.99978, 32.99980), (-6.99978, 33.00030)])],
        crs="EPSG:4326",
    )
    roads_gdf.to_file(roads_path, driver="GeoJSON")

    context = VectorizationContext(
        release_t1="WB_2022_R01",
        release_t2="WB_2023_R01",
        src_date_t1="2022-01-01",
        src_date_t2="2023-01-01",
    )
    unconstrained = build_metric_buffer_layers(
        source_geojson,
        distances_m=[20.0],
        context=context,
    )
    constrained = build_metric_buffer_layers(
        source_geojson,
        distances_m=[20.0],
        context=context,
        road_constraint_layer_path=str(roads_path),
    )

    unconstrained_geom = shape(unconstrained["20m"][1]["features"][0]["geometry"])
    constrained_geom = shape(constrained["20m"][1]["features"][0]["geometry"])
    source_geom = shape(source_geojson["features"][0]["geometry"])

    assert constrained_geom.area < unconstrained_geom.area
    assert constrained_geom.contains(source_geom.centroid)
    assert constrained_geom.bounds[0] <= unconstrained_geom.bounds[0] + 1e-9
    assert constrained_geom.bounds[2] < unconstrained_geom.bounds[2]
    if isinstance(constrained_geom, Polygon):
        assert len(constrained_geom.interiors) == 0
    else:
        assert all(len(part.interiors) == 0 for part in constrained_geom.geoms)


def test_metric_buffers_strip_holes_from_exported_geometry() -> None:
    source_geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [[-7.00020, 33.00000], [-6.99980, 33.00000], [-6.99980, 33.00040], [-7.00020, 33.00040], [-7.00020, 33.00000]],
                        [[-7.00008, 33.00012], [-6.99992, 33.00012], [-6.99992, 33.00028], [-7.00008, 33.00028], [-7.00008, 33.00012]],
                    ],
                },
                "properties": {"block_id": 1, "source_building_count": 1, "block_gap_m": 25.0},
            }
        ],
    }
    context = VectorizationContext(
        release_t1="WB_2022_R01",
        release_t2="WB_2023_R01",
        src_date_t1="2022-01-01",
        src_date_t2="2023-01-01",
    )
    outputs = build_metric_buffer_layers(
        source_geojson,
        distances_m=[10.0],
        context=context,
    )
    buffered_geom = shape(outputs["10m"][1]["features"][0]["geometry"])
    if isinstance(buffered_geom, Polygon):
        assert len(buffered_geom.interiors) == 0
    else:
        assert all(len(part.interiors) == 0 for part in buffered_geom.geoms)


def test_metric_buffers_can_keep_disjoint_parts_as_separate_features() -> None:
    source_geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "MultiPolygon",
                        "coordinates": [
                            [[[-7.00030, 33.00000], [-7.00020, 33.00000], [-7.00020, 33.00010], [-7.00030, 33.00010], [-7.00030, 33.00000]]],
                            [[[-6.99970, 33.00000], [-6.99960, 33.00000], [-6.99960, 33.00010], [-6.99970, 33.00010], [-6.99970, 33.00000]]],
                        ],
                    },
                "properties": {"block_id": 9, "source_building_count": 2, "block_gap_m": 25.0},
            }
        ],
    }
    context = VectorizationContext(
        release_t1="WB_2022_R01",
        release_t2="WB_2023_R01",
        src_date_t1="2022-01-01",
        src_date_t2="2023-01-01",
    )
    outputs = build_metric_buffer_layers(
        source_geojson,
        distances_m=[10.0],
        context=context,
        keep_disjoint_parts_separate=True,
    )
    buffer_df, buffer_geojson = outputs["10m"]
    assert len(buffer_geojson["features"]) == 2
    assert len(buffer_df) == 2
    assert set(buffer_df["source_block_id"]) == {9}
    assert set(buffer_df["buffer_part_index"]) == {1, 2}


def test_bandon_exports_include_raster_georeferencing_metadata(tmp_path) -> None:
    reference_raster_path = tmp_path / "reference.tif"
    data = np.zeros((8, 8), dtype=np.uint8)
    with rasterio.open(
        reference_raster_path,
        "w",
        driver="GTiff",
        width=8,
        height=8,
        count=1,
        dtype="uint8",
        crs="EPSG:3857",
        transform=from_bounds(-8, 33, -7, 34, 8, 8),
    ) as dst:
        dst.write(data, 1)

    result_dir = tmp_path / "result"
    result_dir.mkdir()
    rgb = np.zeros((8, 8, 3), dtype=np.uint8)
    probability = np.zeros((8, 8), dtype=np.float32)
    mask = np.zeros((8, 8), dtype=bool)
    labels = np.zeros((8, 8), dtype=np.uint16)
    empty_geojson = {"type": "FeatureCollection", "features": []}

    previews, artifacts, _, _ = export_bandon_outputs(
        result_dir=result_dir,
        reference_raster_path=reference_raster_path,
        t1_rgb=rgb,
        t2_rgb=rgb,
        change_prob=probability,
        change_mask=mask,
        change_labels=labels,
        change_polygons_df=pd.DataFrame({"change_id": []}),
        change_polygons_geojson=empty_geojson,
        change_blocks_df=pd.DataFrame({"block_id": []}),
        change_blocks_geojson=empty_geojson,
        buffer_layers={},
        summary_df=pd.DataFrame({"metric": []}),
    )

    assert previews.raster_bounds_wgs84 is not None
    assert previews.raster_bounds_native == [-8.0, 33.0, -7.0, 34.0]
    assert previews.raster_crs == "EPSG:3857"
    assert previews.raster_transform is not None
    assert previews.raster_size == [8, 8]
    assert any(artifact.name == "t2_wayback_rgb_tif" for artifact in artifacts)
