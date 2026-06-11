from __future__ import annotations

import json
from datetime import date

import numpy as np
import rasterio
from rasterio.transform import from_origin

from src.config import Settings
from src.domain.mosaic import AlignmentResult, MosaicResult
from src.domain.wayback import MetadataSummary, TileAvailabilitySummary, WaybackRelease
from src.schemas import PreviewImages, RunRequest, TabularMetrics
from src.services.processing import run_detection


def _write_rgb_tif(path, array: np.ndarray) -> None:
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=array.shape[1],
        height=array.shape[0],
        count=array.shape[2],
        dtype=array.dtype,
        crs="EPSG:3857",
        transform=from_origin(0.0, float(array.shape[0]), 1.0, 1.0),
    ) as dst:
        for band_index in range(array.shape[2]):
            dst.write(array[:, :, band_index], band_index + 1)


def _scene_result(path, valid_mask_path, identifier: str, release_date: date) -> MosaicResult:
    return MosaicResult(
        identifier=identifier,
        release_date=str(release_date),
        zoom=19,
        tile_count=1,
        available_tile_count=1,
        missing_tile_count=0,
        tile_range=(0, 0, 0, 0),
        bounds_3857=(0.0, 0.0, 1.0, 1.0),
        png_path=path.with_suffix(".png"),
        geotiff_path=path,
        valid_mask_path=valid_mask_path,
    )


def test_run_detection_supports_bandon_backend(tmp_path, monkeypatch) -> None:
    settings = Settings(
        runtime_cache_dir=tmp_path,
        wayback_tilemap_preflight_enabled=False,
        keep_intermediate_artifacts=True,
        change_threshold=0.37,
        semantic_threshold=0.42,
        bandon_min_model_input_size_px=1,
    )
    releases = [
        WaybackRelease(
            identifier="WB_2022_R03",
            release_date=date(2022, 3, 16),
            label="2022-03-16 | WB_2022_R03",
            release_num=1,
            tile_matrix_sets=("default028mm",),
            resource_url_template="https://example.com/t1",
        ),
        WaybackRelease(
            identifier="WB_2026_R03",
            release_date=date(2026, 3, 25),
            label="2026-03-25 | WB_2026_R03",
            release_num=2,
            tile_matrix_sets=("default028mm",),
            resource_url_template="https://example.com/t2",
        ),
    ]
    request = RunRequest(
        aoi_geojson={
            "type": "Polygon",
            "coordinates": [[[-7.0, 33.0], [-6.9995, 33.0], [-6.9995, 33.0005], [-7.0, 33.0005], [-7.0, 33.0]]],
        },
        t1_release="WB_2022_R03",
        t2_release="WB_2026_R03",
        mode="fast_preview",
        change_threshold=0.91,
        semantic_threshold=0.92,
    )

    rgb = np.zeros((4, 4, 3), dtype=np.uint8)
    valid_mask = np.ones((4, 4), dtype=np.uint8)
    t1_rgb_path = tmp_path / "t1.tif"
    t2_rgb_path = tmp_path / "t2.tif"
    t1_valid_mask_path = tmp_path / "t1_valid.tif"
    t2_valid_mask_path = tmp_path / "t2_valid.tif"
    _write_rgb_tif(t1_rgb_path, rgb)
    _write_rgb_tif(t2_rgb_path, rgb)
    _write_rgb_tif(t1_valid_mask_path, valid_mask[:, :, None])
    _write_rgb_tif(t2_valid_mask_path, valid_mask[:, :, None])
    scene_t1 = _scene_result(t1_rgb_path, t1_valid_mask_path, "WB_2022_R03", date(2022, 3, 16))
    scene_t2 = _scene_result(t2_rgb_path, t2_valid_mask_path, "WB_2026_R03", date(2026, 3, 25))
    metadata = MetadataSummary(
        dominant_src_date="2026-03-25",
        dominant_src_res_m=0.3,
        capture_date_count=1,
        mixed_capture_dates=False,
        metadata_region_count=1,
    )
    tilemap = TileAvailabilitySummary(
        candidate_count=1,
        available_count=1,
        missing_count=0,
        failed_check_count=0,
        preflight_complete=True,
        availability_fraction=1.0,
        available_tiles=frozenset({(0, 0)}),
    )

    class _BandonResult:
        change_probability = np.ones((4, 4), dtype=np.float32)
        change_mask = np.ones((4, 4), dtype=bool)
        child_timing = None
        launcher = "test"
        command = ["test"]
        metadata = {
            "effective_backend": "bandon_mps",
            "runner_family": "bandon_mps",
            "threshold": 0.4,
        }

    monkeypatch.setattr("src.services.processing.list_releases", lambda _settings: releases)
    monkeypatch.setattr(
        "src.services.processing._resolve_release_for_aoi",
        lambda settings_arg, *, release, aoi_bbox, normalized_aoi, timing, stage_prefix, scene_role: type(
            "Resolved",
            (),
            {"release": release, "zoom": settings_arg.zoom, "metadata": metadata, "tilemap": tilemap},
        )(),
    )
    monkeypatch.setattr(
        "src.services.processing.get_or_create_inference_reference_imagery",
        lambda *, release, **kwargs: scene_t1 if release.identifier == "WB_2022_R03" else scene_t2,
    )
    monkeypatch.setattr(
        "src.services.processing.align_mosaic_pair",
        lambda *args, **kwargs: AlignmentResult(
            t1_rgb=rgb,
            t2_rgb=rgb,
            t1_valid_mask=valid_mask.astype(bool),
            t2_valid_mask=valid_mask.astype(bool),
            diagnostics={},
        ),
    )
    monkeypatch.setattr("src.services.processing.rasterize_aoi_mask_like", lambda *args, **kwargs: valid_mask.astype(bool))
    inference_kwargs = {}

    def _run_bandon_inference(**kwargs):
        inference_kwargs.update(kwargs)
        return _BandonResult()

    monkeypatch.setattr("src.services.processing.run_bandon_inference", _run_bandon_inference)
    monkeypatch.setattr(
        "src.services.processing.vectorize_change_regions",
        lambda *args, **kwargs: (TabularMetrics().model_dump() if False else __import__("pandas").DataFrame({"area_m2": []}), {"type": "FeatureCollection", "features": []}),
    )
    monkeypatch.setattr("src.services.processing.merge_close_change_regions", lambda geojson, **kwargs: (__import__("pandas").DataFrame({"area_m2": []}), geojson))
    monkeypatch.setattr("src.services.processing.build_change_blocks", lambda geojson, **kwargs: (__import__("pandas").DataFrame({"area_m2": []}), geojson))
    monkeypatch.setattr("src.services.processing.build_change_buffer_layers", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        "src.services.processing.export_bandon_outputs",
        lambda **kwargs: (PreviewImages(), [], None, TabularMetrics()),
    )

    response = run_detection(request, settings=settings, model_backend="bandon_mps")

    assert response.success is True
    assert response.summary is not None
    assert response.summary.model_backend == "bandon_mps"
    assert response.summary.result_semantics == "building_change"
    assert inference_kwargs["threshold"] == 0.37
    assert response.diagnostics is not None
    assert response.diagnostics.thresholds["change_threshold"] == 0.37
    assert response.diagnostics.thresholds["semantic_threshold"] == 0.42
    assert response.diagnostics.backend["threshold_source"] == "backend_settings_env"
    assert any("threshold overrides were ignored" in warning for warning in response.diagnostics.warnings)

    request_dir = settings.request_cache_dir / response.summary.request_hash
    manifest = json.loads((request_dir / "manifest.json").read_text(encoding="utf-8"))
    run_response = json.loads((request_dir / "run_response.json").read_text(encoding="utf-8"))
    assert manifest["change_threshold"] == 0.37
    assert manifest["semantic_threshold"] == 0.42
    assert manifest["threshold_source"] == "backend_settings_env"
    assert run_response["diagnostics"]["thresholds"]["change_threshold"] == 0.37
    assert run_response["diagnostics"]["thresholds"]["semantic_threshold"] == 0.42
    assert run_response["diagnostics"]["backend"]["threshold_source"] == "backend_settings_env"
