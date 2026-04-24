from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import rasterio
from rasterio.transform import from_origin
import requests

from src.config import Settings
from src.domain.inference import InferenceDiagnostics
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


def _scene_result(path, valid_mask_path, identifier: str, release_date: date, *, png_path=None) -> MosaicResult:
    return MosaicResult(
        identifier=identifier,
        release_date=str(release_date),
        tile_count=1,
        available_tile_count=1,
        missing_tile_count=0,
        tile_range=(0, 0, 0, 0),
        bounds_3857=(0.0, 0.0, 1.0, 1.0),
        png_path=png_path or path.with_suffix(".png"),
        geotiff_path=path,
        valid_mask_path=valid_mask_path,
    )


def test_run_detection_populates_release_dates_in_summary(tmp_path, monkeypatch) -> None:
    settings = Settings(
        runtime_cache_dir=tmp_path,
        arosics_enabled=False,
        wayback_tilemap_preflight_enabled=False,
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

    shared_cache_dir = tmp_path / "wayback_mosaics" / "shared-entry"
    shared_cache_dir.mkdir(parents=True, exist_ok=True)
    shared_png = shared_cache_dir / "mosaic.png"
    shared_png.write_bytes(b"preview")
    scene_t1 = _scene_result(
        t1_rgb_path,
        t1_valid_mask_path,
        "WB_2022_R03",
        date(2022, 3, 16),
        png_path=shared_png,
    )
    scene_t2 = _scene_result(
        t2_rgb_path,
        t2_valid_mask_path,
        "WB_2026_R03",
        date(2026, 3, 25),
        png_path=shared_png,
    )

    monkeypatch.setattr("src.services.processing.list_releases", lambda settings: releases)
    monkeypatch.setattr("src.services.processing.load_cached_response", lambda *args, **kwargs: None)
    monkeypatch.setattr("src.services.processing.build_session", lambda settings: object())
    monkeypatch.setattr(
        "src.services.processing.summarize_wayback_metadata",
        lambda *args, **kwargs: MetadataSummary(dominant_src_date="2021-02-23", dominant_src_res_m=0.31),
    )
    monkeypatch.setattr(
        "src.services.processing.download_wayback_mosaic",
        lambda release, *args, **kwargs: scene_t1 if release.identifier == "WB_2022_R03" else scene_t2,
    )
    monkeypatch.setattr(
        "src.services.processing.align_mosaic_pair",
        lambda *args, **kwargs: AlignmentResult(
            t1_rgb=rgb,
            t2_rgb=rgb,
            t1_valid_mask=valid_mask.astype(bool),
            t2_valid_mask=valid_mask.astype(bool),
            diagnostics={"method": "reprojection_only", "used_arosics": False, "warnings": []},
        ),
    )
    monkeypatch.setattr("src.services.processing.resolve_min_new_building_pixels", lambda *args, **kwargs: 1)
    monkeypatch.setattr(
        "src.services.processing.derive_new_building_products",
        lambda *args, **kwargs: {
            "change_mask": np.zeros((4, 4), dtype=bool),
            "t1_building_mask": np.zeros((4, 4), dtype=bool),
            "t1_building_mask_dilated": np.zeros((4, 4), dtype=bool),
            "t2_building_mask": np.zeros((4, 4), dtype=bool),
            "new_building_mask_raw": np.zeros((4, 4), dtype=bool),
            "new_building_mask_filtered": np.zeros((4, 4), dtype=bool),
            "new_building_mask": np.zeros((4, 4), dtype=bool),
            "new_building_labels": np.zeros((4, 4), dtype=np.int32),
        },
    )
    empty_fc = {"type": "FeatureCollection", "features": []}
    monkeypatch.setattr(
        "src.services.processing.vectorize_new_buildings",
        lambda *args, **kwargs: (pd.DataFrame(columns=["area_m2"]), empty_fc),
    )
    monkeypatch.setattr(
        "src.services.processing.merge_close_buildings",
        lambda *args, **kwargs: (pd.DataFrame(columns=["area_m2"]), empty_fc),
    )
    monkeypatch.setattr(
        "src.services.processing.build_building_blocks",
        lambda *args, **kwargs: (pd.DataFrame(columns=["area_m2"]), empty_fc),
    )
    monkeypatch.setattr("src.services.processing.build_metric_buffer_layers", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        "src.services.processing.export_run_outputs",
        lambda **kwargs: (PreviewImages(), [], None, TabularMetrics()),
    )
    monkeypatch.setattr("src.services.processing.save_cached_response", lambda *args, **kwargs: None)

    def fake_inference_runner(*args, **kwargs):
        probs = {
            "change_prediction": np.zeros((4, 4), dtype=np.float32),
            "t1_semantic_prediction": np.zeros((4, 4), dtype=np.float32),
            "t2_semantic_prediction": np.zeros((4, 4), dtype=np.float32),
        }
        return probs, InferenceDiagnostics(patch_count=2, patch_prepare_seconds=0.0, remote_seconds=0.0, mask_decode_seconds=0.0)

    response = run_detection(
        request,
        settings=settings,
        inference_runner=fake_inference_runner,
    )

    assert response.success is True
    assert response.summary is not None
    assert response.summary.release_date_t1 == "2022-03-16"
    assert response.summary.release_date_t2 == "2026-03-25"
    assert response.summary.dominant_src_date_t1 == "2021-02-23"


def test_run_detection_reports_tile_availability_stage_before_download(tmp_path, monkeypatch) -> None:
    settings = Settings(
        runtime_cache_dir=tmp_path,
        arosics_enabled=False,
        wayback_tilemap_preflight_enabled=True,
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

    monkeypatch.setattr("src.services.processing.list_releases", lambda settings: releases)
    monkeypatch.setattr("src.services.processing.load_cached_response", lambda *args, **kwargs: None)
    monkeypatch.setattr("src.services.processing.build_session", lambda settings: object())
    monkeypatch.setattr(
        "src.services.processing.summarize_wayback_metadata",
        lambda *args, **kwargs: MetadataSummary(dominant_src_date="2021-02-23", dominant_src_res_m=0.31),
    )
    monkeypatch.setattr(
        "src.services.processing.preflight_wayback_tile_availability",
        lambda *args, **kwargs: TileAvailabilitySummary(
            candidate_count=1,
            available_count=1,
            missing_count=0,
            failed_check_count=0,
            preflight_complete=True,
            availability_fraction=1.0,
            available_tiles=frozenset({(0, 0)}),
        ),
    )
    monkeypatch.setattr(
        "src.services.processing.download_wayback_mosaic",
        lambda release, *args, **kwargs: scene_t1 if release.identifier == "WB_2022_R03" else scene_t2,
    )
    monkeypatch.setattr(
        "src.services.processing.align_mosaic_pair",
        lambda *args, **kwargs: AlignmentResult(
            t1_rgb=rgb,
            t2_rgb=rgb,
            t1_valid_mask=valid_mask.astype(bool),
            t2_valid_mask=valid_mask.astype(bool),
            diagnostics={"method": "reprojection_only", "used_arosics": False, "warnings": []},
        ),
    )
    monkeypatch.setattr("src.services.processing.resolve_min_new_building_pixels", lambda *args, **kwargs: 1)
    monkeypatch.setattr(
        "src.services.processing.derive_new_building_products",
        lambda *args, **kwargs: {
            "change_mask": np.zeros((4, 4), dtype=bool),
            "t1_building_mask": np.zeros((4, 4), dtype=bool),
            "t1_building_mask_dilated": np.zeros((4, 4), dtype=bool),
            "t2_building_mask": np.zeros((4, 4), dtype=bool),
            "new_building_mask_raw": np.zeros((4, 4), dtype=bool),
            "new_building_mask_filtered": np.zeros((4, 4), dtype=bool),
            "new_building_mask": np.zeros((4, 4), dtype=bool),
            "new_building_labels": np.zeros((4, 4), dtype=np.int32),
        },
    )
    empty_fc = {"type": "FeatureCollection", "features": []}
    monkeypatch.setattr(
        "src.services.processing.vectorize_new_buildings",
        lambda *args, **kwargs: (pd.DataFrame(columns=["area_m2"]), empty_fc),
    )
    monkeypatch.setattr(
        "src.services.processing.merge_close_buildings",
        lambda *args, **kwargs: (pd.DataFrame(columns=["area_m2"]), empty_fc),
    )
    monkeypatch.setattr(
        "src.services.processing.build_building_blocks",
        lambda *args, **kwargs: (pd.DataFrame(columns=["area_m2"]), empty_fc),
    )
    monkeypatch.setattr("src.services.processing.build_metric_buffer_layers", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        "src.services.processing.export_run_outputs",
        lambda **kwargs: (PreviewImages(), [], None, TabularMetrics()),
    )
    monkeypatch.setattr("src.services.processing.save_cached_response", lambda *args, **kwargs: None)

    def fake_inference_runner(*args, **kwargs):
        probs = {
            "change_prediction": np.zeros((4, 4), dtype=np.float32),
            "t1_semantic_prediction": np.zeros((4, 4), dtype=np.float32),
            "t2_semantic_prediction": np.zeros((4, 4), dtype=np.float32),
        }
        return probs, InferenceDiagnostics(patch_count=2, patch_prepare_seconds=0.0, remote_seconds=0.0, mask_decode_seconds=0.0)

    progress_messages: list[str] = []

    response = run_detection(
        request,
        settings=settings,
        inference_runner=fake_inference_runner,
        progress=lambda _value, message: progress_messages.append(message),
    )

    assert response.success is True
    assert "Resolving Wayback metadata" in progress_messages
    assert "Checking tile availability" in progress_messages
    assert "Downloading Wayback imagery" in progress_messages
    assert progress_messages.index("Resolving Wayback metadata") < progress_messages.index("Checking tile availability")
    assert progress_messages.index("Checking tile availability") < progress_messages.index("Downloading Wayback imagery")


def test_run_detection_does_not_forward_scene_tile_caps_to_download(tmp_path, monkeypatch) -> None:
    settings = Settings(
        runtime_cache_dir=tmp_path,
        arosics_enabled=False,
        wayback_tilemap_preflight_enabled=False,
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
        mode="full_run",
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
    max_tiles_seen: list[int | None] = []

    monkeypatch.setattr("src.services.processing.list_releases", lambda settings: releases)
    monkeypatch.setattr("src.services.processing.load_cached_response", lambda *args, **kwargs: None)
    monkeypatch.setattr("src.services.processing.build_session", lambda settings: object())
    monkeypatch.setattr(
        "src.services.processing.summarize_wayback_metadata",
        lambda *args, **kwargs: MetadataSummary(dominant_src_date="2021-02-23", dominant_src_res_m=0.31),
    )

    def fake_download(release, *args, **kwargs):
        max_tiles_seen.append(kwargs.get("max_tiles"))
        return scene_t1 if release.identifier == "WB_2022_R03" else scene_t2

    monkeypatch.setattr("src.services.processing.download_wayback_mosaic", fake_download)
    monkeypatch.setattr(
        "src.services.processing.align_mosaic_pair",
        lambda *args, **kwargs: AlignmentResult(
            t1_rgb=rgb,
            t2_rgb=rgb,
            t1_valid_mask=valid_mask.astype(bool),
            t2_valid_mask=valid_mask.astype(bool),
            diagnostics={"method": "reprojection_only", "used_arosics": False, "warnings": []},
        ),
    )
    monkeypatch.setattr("src.services.processing.resolve_min_new_building_pixels", lambda *args, **kwargs: 1)
    monkeypatch.setattr(
        "src.services.processing.derive_new_building_products",
        lambda *args, **kwargs: {
            "change_mask": np.zeros((4, 4), dtype=bool),
            "t1_building_mask": np.zeros((4, 4), dtype=bool),
            "t1_building_mask_dilated": np.zeros((4, 4), dtype=bool),
            "t2_building_mask": np.zeros((4, 4), dtype=bool),
            "new_building_mask_raw": np.zeros((4, 4), dtype=bool),
            "new_building_mask_filtered": np.zeros((4, 4), dtype=bool),
            "new_building_mask": np.zeros((4, 4), dtype=bool),
            "new_building_labels": np.zeros((4, 4), dtype=np.int32),
        },
    )
    empty_fc = {"type": "FeatureCollection", "features": []}
    monkeypatch.setattr(
        "src.services.processing.vectorize_new_buildings",
        lambda *args, **kwargs: (pd.DataFrame(columns=["area_m2"]), empty_fc),
    )
    monkeypatch.setattr(
        "src.services.processing.merge_close_buildings",
        lambda *args, **kwargs: (pd.DataFrame(columns=["area_m2"]), empty_fc),
    )
    monkeypatch.setattr(
        "src.services.processing.build_building_blocks",
        lambda *args, **kwargs: (pd.DataFrame(columns=["area_m2"]), empty_fc),
    )
    monkeypatch.setattr("src.services.processing.build_metric_buffer_layers", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        "src.services.processing.export_run_outputs",
        lambda **kwargs: (PreviewImages(), [], None, TabularMetrics()),
    )
    monkeypatch.setattr("src.services.processing.save_cached_response", lambda *args, **kwargs: None)

    def fake_inference_runner(*args, **kwargs):
        probs = {
            "change_prediction": np.zeros((4, 4), dtype=np.float32),
            "t1_semantic_prediction": np.zeros((4, 4), dtype=np.float32),
            "t2_semantic_prediction": np.zeros((4, 4), dtype=np.float32),
        }
        return probs, InferenceDiagnostics(patch_count=2, patch_prepare_seconds=0.0, remote_seconds=0.0, mask_decode_seconds=0.0)

    response = run_detection(
        request,
        settings=settings,
        inference_runner=fake_inference_runner,
    )

    assert response.success is True
    assert max_tiles_seen == [None, None]


def test_run_detection_supports_bandon_backend(tmp_path, monkeypatch) -> None:
    settings = Settings(
        runtime_cache_dir=tmp_path,
        arosics_enabled=False,
        wayback_tilemap_preflight_enabled=False,
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

    monkeypatch.setattr("src.services.processing.list_releases", lambda settings: releases)
    monkeypatch.setattr("src.services.processing.load_cached_response", lambda *args, **kwargs: None)
    monkeypatch.setattr("src.services.processing.build_session", lambda settings: object())
    monkeypatch.setattr(
        "src.services.processing.summarize_wayback_metadata",
        lambda *args, **kwargs: MetadataSummary(dominant_src_date="2021-02-23", dominant_src_res_m=0.31),
    )
    monkeypatch.setattr(
        "src.services.processing.download_wayback_mosaic",
        lambda release, *args, **kwargs: scene_t1 if release.identifier == "WB_2022_R03" else scene_t2,
    )
    monkeypatch.setattr(
        "src.services.processing.align_mosaic_pair",
        lambda *args, **kwargs: AlignmentResult(
            t1_rgb=rgb,
            t2_rgb=rgb,
            t1_valid_mask=valid_mask.astype(bool),
            t2_valid_mask=valid_mask.astype(bool),
            diagnostics={"method": "reprojection_only", "used_arosics": False, "warnings": []},
        ),
    )
    monkeypatch.setattr("src.services.processing.resolve_min_new_building_pixels", lambda *args, **kwargs: 1)
    monkeypatch.setattr(
        "src.services.processing.run_bandon_inference",
        lambda **kwargs: type(
            "BandonResult",
            (),
            {
                "change_probability": np.full((4, 4), 0.9, dtype=np.float32),
                "change_mask": np.ones((4, 4), dtype=bool),
                "metadata": {
                    "device_resolved": "mps",
                    "allow_mps_fallback": False,
                    "pytorch_enable_mps_fallback": None,
                    "mps_test_cfg": {"applied": True},
                    "mps_built": True,
                    "mps_available": True,
                },
                "launcher": "env_python",
                "command": ["python", "infer_mps.py"],
            },
        )(),
    )
    empty_fc = {"type": "FeatureCollection", "features": []}
    monkeypatch.setattr(
        "src.services.processing.vectorize_change_regions",
        lambda *args, **kwargs: (pd.DataFrame(columns=["area_m2"]), empty_fc),
    )
    monkeypatch.setattr(
        "src.services.processing.merge_close_change_regions",
        lambda *args, **kwargs: (pd.DataFrame(columns=["area_m2"]), empty_fc),
    )
    monkeypatch.setattr(
        "src.services.processing.export_bandon_outputs",
        lambda **kwargs: (PreviewImages(), [], None, TabularMetrics()),
    )
    monkeypatch.setattr("src.services.processing.save_cached_response", lambda *args, **kwargs: None)

    response = run_detection(
        request,
        settings=settings,
        model_backend="bandon_mps",
    )

    assert response.success is True
    assert response.summary is not None
    assert response.summary.result_semantics == "building_change"
    assert response.summary.total_change_polygons == 0
    assert response.diagnostics is not None
    assert response.diagnostics.backend["model_backend"] == "bandon_mps"


def test_run_detection_returns_download_error_on_connection_failure(tmp_path, monkeypatch) -> None:
    settings = Settings(
        runtime_cache_dir=tmp_path,
        arosics_enabled=False,
        wayback_tilemap_preflight_enabled=False,
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
        mode="full_run",
    )

    monkeypatch.setattr("src.services.processing.list_releases", lambda settings: releases)
    monkeypatch.setattr("src.services.processing.load_cached_response", lambda *args, **kwargs: None)
    monkeypatch.setattr("src.services.processing.build_session", lambda settings: object())
    monkeypatch.setattr(
        "src.services.processing.summarize_wayback_metadata",
        lambda *args, **kwargs: MetadataSummary(dominant_src_date="2021-02-23", dominant_src_res_m=0.31),
    )
    monkeypatch.setattr(
        "src.services.processing.download_wayback_mosaic",
        lambda *args, **kwargs: (_ for _ in ()).throw(requests.ConnectionError("connection dropped")),
    )

    response = run_detection(
        request,
        settings=settings,
        inference_runner=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("inference should not run")),
    )

    assert response.success is False
    assert response.error_code == "wayback_tile_download_failed"
    assert "connection dropped" in (response.error_message or "")


def test_run_detection_bandon_preserves_valid_change_components_inside_scene(tmp_path, monkeypatch) -> None:
    settings = Settings(
        runtime_cache_dir=tmp_path,
        arosics_enabled=False,
        wayback_tilemap_preflight_enabled=False,
    )
    releases = [
        WaybackRelease(
            identifier="WB_2023_R02",
            release_date=date(2023, 3, 15),
            label="2023-03-15 | WB_2023_R02",
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
        t1_release="WB_2023_R02",
        t2_release="WB_2026_R03",
        mode="full_run",
        change_threshold=0.5,
        new_building_core_distance_pixels=2,
        min_new_building_pixels=1,
    )

    rgb = np.zeros((8, 8, 3), dtype=np.uint8)
    valid_mask = np.ones((8, 8), dtype=np.uint8)
    valid_mask[0, :] = 0
    valid_mask[:, 0] = 0
    valid_mask[-1, :] = 0
    valid_mask[:, -1] = 0
    t1_rgb_path = tmp_path / "t1.tif"
    t2_rgb_path = tmp_path / "t2.tif"
    t1_valid_mask_path = tmp_path / "t1_valid.tif"
    t2_valid_mask_path = tmp_path / "t2_valid.tif"
    _write_rgb_tif(t1_rgb_path, rgb)
    _write_rgb_tif(t2_rgb_path, rgb)
    _write_rgb_tif(t1_valid_mask_path, valid_mask[:, :, None])
    _write_rgb_tif(t2_valid_mask_path, valid_mask[:, :, None])
    scene_t1 = _scene_result(t1_rgb_path, t1_valid_mask_path, "WB_2023_R02", date(2023, 3, 15))
    scene_t2 = _scene_result(t2_rgb_path, t2_valid_mask_path, "WB_2026_R03", date(2026, 3, 25))

    captured: dict[str, np.ndarray] = {}
    centered_change = np.zeros((8, 8), dtype=bool)
    centered_change[2:6, 2:6] = True

    monkeypatch.setattr("src.services.processing.list_releases", lambda settings: releases)
    monkeypatch.setattr("src.services.processing.load_cached_response", lambda *args, **kwargs: None)
    monkeypatch.setattr("src.services.processing.build_session", lambda settings: object())
    monkeypatch.setattr(
        "src.services.processing.summarize_wayback_metadata",
        lambda *args, **kwargs: MetadataSummary(dominant_src_date="2022-01-12", dominant_src_res_m=0.5),
    )
    monkeypatch.setattr(
        "src.services.processing.download_wayback_mosaic",
        lambda release, *args, **kwargs: scene_t1 if release.identifier == "WB_2023_R02" else scene_t2,
    )
    monkeypatch.setattr(
        "src.services.processing.align_mosaic_pair",
        lambda *args, **kwargs: AlignmentResult(
            t1_rgb=rgb,
            t2_rgb=rgb,
            t1_valid_mask=valid_mask.astype(bool),
            t2_valid_mask=valid_mask.astype(bool),
            diagnostics={"method": "reprojection_only", "used_arosics": False, "warnings": []},
        ),
    )
    monkeypatch.setattr("src.services.processing.resolve_min_new_building_pixels", lambda *args, **kwargs: 1)
    monkeypatch.setattr(
        "src.services.processing.run_bandon_inference",
        lambda **kwargs: type(
            "BandonResult",
            (),
            {
                "change_probability": np.where(centered_change, 0.9, 0.1).astype(np.float32),
                "change_mask": centered_change.copy(),
                "metadata": {
                    "device_resolved": "mps",
                    "allow_mps_fallback": False,
                    "pytorch_enable_mps_fallback": None,
                    "mps_test_cfg": {"applied": False},
                    "mps_built": True,
                    "mps_available": True,
                },
                "launcher": "env_python",
                "command": ["python", "infer_mps.py"],
            },
        )(),
    )
    empty_fc = {"type": "FeatureCollection", "features": []}
    monkeypatch.setattr(
        "src.services.processing.vectorize_change_regions",
        lambda mask, *args, **kwargs: (
            pd.DataFrame([{"area_m2": float(mask.sum())}]) if mask.any() else pd.DataFrame(columns=["area_m2"]),
            empty_fc,
        ),
    )
    monkeypatch.setattr(
        "src.services.processing.merge_close_change_regions",
        lambda geojson, *args, **kwargs: (pd.DataFrame([{"area_m2": 16.0}]), empty_fc),
    )
    monkeypatch.setattr(
        "src.services.processing.build_change_blocks",
        lambda geojson, *args, **kwargs: (
            pd.DataFrame([{"area_m2": 25.0, "change_block_id": 1, "source_change_count": 1, "block_gap_m": 25.0}]),
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {"type": "Polygon", "coordinates": []},
                        "properties": {"change_block_id": 1, "source_change_count": 1, "block_gap_m": 25.0},
                    }
                ],
            },
        ),
    )
    monkeypatch.setattr(
        "src.services.processing.build_change_buffer_layers",
        lambda geojson, *args, **kwargs: {
            "10m": (
                pd.DataFrame([{"buffer_id": 1, "source_change_block_id": 1, "buffer_m": 10.0}]),
                {"type": "FeatureCollection", "features": []},
            )
        },
    )

    def fake_export_bandon_outputs(**kwargs):
        captured["change_mask"] = kwargs["change_mask"].copy()
        captured["change_labels"] = kwargs["change_labels"].copy()
        captured["change_blocks_df"] = kwargs["change_blocks_df"].copy()
        captured["buffer_layers"] = kwargs["buffer_layers"]
        return PreviewImages(), [], None, TabularMetrics()

    monkeypatch.setattr("src.services.processing.export_bandon_outputs", fake_export_bandon_outputs)
    monkeypatch.setattr("src.services.processing.save_cached_response", lambda *args, **kwargs: None)

    response = run_detection(
        request,
        settings=settings,
        model_backend="bandon_mps",
    )

    assert response.success is True
    assert captured["change_mask"].sum() == 16
    assert captured["change_labels"].max() >= 1
    assert len(captured["change_blocks_df"]) == 1
    assert "10m" in captured["buffer_layers"]


def test_run_detection_returns_tilemap_unavailability_diagnostics(tmp_path, monkeypatch) -> None:
    settings = Settings(runtime_cache_dir=tmp_path, arosics_enabled=False)
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
        mode="full_run",
    )

    monkeypatch.setattr("src.services.processing.list_releases", lambda settings: releases)
    monkeypatch.setattr("src.services.processing.load_cached_response", lambda *args, **kwargs: None)
    monkeypatch.setattr("src.services.processing.build_session", lambda settings: object())
    monkeypatch.setattr(
        "src.services.processing.summarize_wayback_metadata",
        lambda *args, **kwargs: MetadataSummary(
            dominant_src_date="2021-02-23",
            dominant_src_res_m=0.31,
            metadata_region_count=2,
            capture_date_count=2,
            mixed_capture_dates=True,
            metadata_coverage_fraction=0.65,
        ),
    )

    tilemap_calls = []

    def fake_preflight(*args, **kwargs):
        tilemap_calls.append(kwargs)
        return TileAvailabilitySummary(
            candidate_count=8,
            available_count=0,
            missing_count=8,
            failed_check_count=0,
            preflight_complete=True,
            availability_fraction=0.0,
            available_tiles=frozenset(),
        )

    monkeypatch.setattr("src.services.processing.preflight_wayback_tile_availability", fake_preflight)

    response = run_detection(
        request,
        settings=settings,
        inference_runner=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("inference should not run")),
    )

    assert response.success is False
    assert response.error_code == "wayback_tile_coverage_unavailable"
    assert "metadata coverage but zero downloadable WMTS tiles" in (response.error_message or "")
    assert response.diagnostics is not None
    assert response.diagnostics.coverage["t1"]["available_count"] == 0
    assert response.diagnostics.coverage["t1"]["mixed_capture_dates"] is True
    assert any("capture-date regions within the AOI" in warning for warning in response.diagnostics.warnings)
    assert tilemap_calls
