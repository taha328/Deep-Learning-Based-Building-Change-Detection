from __future__ import annotations

from datetime import date

import pytest

from src.config import Settings
from src.core_api import probe_backends_api, run_detection_api, validate_request_api
from src.domain.wayback import WaybackRelease
from src.execution_profiles import PipelineExecutionConfig, resolve_backend
from src.domain.local_inference import LocalRuntimeProbe
from src.services.validation import validate_request
from src.schemas import RunRequest, ValidationRequest


def _sample_request() -> ValidationRequest:
    return ValidationRequest.model_validate(
        {
            "aoi_geojson": {
                "type": "Polygon",
                "coordinates": [[
                    [-7.0, 33.0],
                    [-6.9992, 33.0],
                    [-6.9992, 33.0008],
                    [-7.0, 33.0008],
                    [-7.0, 33.0],
                ]],
            },
            "t1_release": "WB_2023_R01",
            "t2_release": "WB_2026_R03",
            "mode": "full_run",
        }
    )


def _sample_releases(settings: Settings) -> list[WaybackRelease]:
    return [
        WaybackRelease(
            identifier="WB_2023_R01",
            release_date=date(2023, 1, 1),
            label="2023-01-01 | WB_2023_R01",
            release_num=1,
            tile_matrix_sets=(settings.tile_matrix_set,),
            resource_url_template="https://example.com/{TileMatrixSet}/{TileMatrix}/{TileRow}/{TileCol}.png",
        ),
        WaybackRelease(
            identifier="WB_2026_R03",
            release_date=date(2026, 3, 1),
            label="2026-03-01 | WB_2026_R03",
            release_num=2,
            tile_matrix_sets=(settings.tile_matrix_set,),
            resource_url_template="https://example.com/{TileMatrixSet}/{TileMatrix}/{TileRow}/{TileCol}.png",
        ),
    ]


def test_hugging_face_gpu_backend_requires_spaces() -> None:
    config = PipelineExecutionConfig(backend_mode="huggingface_gpu")
    backend = resolve_backend(config)
    availability = backend.availability(Settings())
    assert availability.available is False
    assert "configured" in (availability.reason or "").lower()


def test_public_backend_uses_configured_spaces() -> None:
    settings = Settings()
    config = PipelineExecutionConfig(
        backend_mode="public_zerogpu",
        public_zerogpu={"spaces": ["example-a", "example-b"], "api_name": "/foo", "prompt": "roof"},
    )
    backend = resolve_backend(config)
    configured = backend.configure_settings(settings)
    assert configured.remote_segmentation_spaces == ("example-a", "example-b")
    assert configured.remote_segmentation_api_name == "/foo"
    assert configured.remote_segmentation_prompt == "roof"


def test_probe_backends_reports_three_modes() -> None:
    availabilities = probe_backends_api(settings=Settings())
    assert {item.mode for item in availabilities} == {"public_zerogpu", "local", "huggingface_gpu", "bandon_mps"}


def test_local_backend_skips_remote_patch_budget() -> None:
    request = _sample_request()
    settings = Settings(
        patch_size=1024,
        stride=768,
        full_limits={"name": "full_run", "label": "Full Run", "max_area_m2": 2_000_000.0, "max_scene_tiles": 400, "max_remote_patches_per_scene": 1},
    )
    config = PipelineExecutionConfig(backend_mode="local")
    validation = validate_request_api(request, settings=settings, execution_config=config)
    assert all("remote SAM3 patches" not in message for message in validation.blocking_errors)


def test_bandon_backend_skips_remote_patch_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    request = _sample_request()
    settings = Settings(
        patch_size=1024,
        stride=768,
        full_limits={"name": "full_run", "label": "Full Run", "max_area_m2": 2_000_000.0, "max_scene_tiles": 400, "max_remote_patches_per_scene": 1},
    )
    monkeypatch.setattr(
        "src.execution_profiles.probe_bandon_runtime",
        lambda configured_settings: type("Probe", (), {"available": True, "message": "ok", "diagnostics": lambda self: {}})(),
    )
    config = PipelineExecutionConfig(model_backend="bandon_mps")
    validation = validate_request_api(request, settings=settings, execution_config=config)
    assert all("remote SAM3 patches" not in message for message in validation.blocking_errors)


def test_local_backend_returns_backend_unavailable_when_runtime_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    request = RunRequest.model_validate(
        {
            **_sample_request().model_dump(mode="json"),
            "change_threshold": 0.5,
            "semantic_threshold": 0.5,
            "min_new_building_pixels": 50,
            "old_building_mask_dilation_pixels": 2,
            "new_building_core_distance_pixels": 2,
            "merge_close_gap_m": 10.0,
            "building_block_gap_m": 25.0,
            "buffer_distances_m": [10.0, 15.0, 20.0],
        }
    )
    monkeypatch.setattr(
        "src.execution_profiles.probe_local_runtime",
        lambda config: LocalRuntimeProbe(available=False, message="missing local runtime"),
    )
    config = PipelineExecutionConfig(backend_mode="local")
    response = run_detection_api(request, settings=Settings(), execution_config=config)
    assert response.success is False
    assert response.error_code == "backend_unavailable"


def test_bandon_backend_returns_backend_unavailable_when_runtime_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    request = RunRequest.model_validate(
        {
            **_sample_request().model_dump(mode="json"),
            "change_threshold": 0.5,
            "semantic_threshold": 0.5,
            "min_new_building_pixels": 50,
            "old_building_mask_dilation_pixels": 2,
            "new_building_core_distance_pixels": 2,
            "merge_close_gap_m": 10.0,
            "building_block_gap_m": 25.0,
            "buffer_distances_m": [10.0, 15.0, 20.0],
        }
    )

    class _Probe:
        available = False
        message = "missing bandon runtime"

        def diagnostics(self):
            return {}

    monkeypatch.setattr("src.execution_profiles.probe_bandon_runtime", lambda settings: _Probe())
    config = PipelineExecutionConfig(model_backend="bandon_mps")
    response = run_detection_api(request, settings=Settings(), execution_config=config)
    assert response.success is False
    assert response.error_code == "backend_unavailable"


def test_bandon_request_hash_context_separates_effective_backend_checkpoint_and_threshold(tmp_path) -> None:
    bandon_checkpoint = tmp_path / "bandon.pth"
    s2_checkpoint = tmp_path / "s2looking.pth"
    bandon_checkpoint.write_bytes(b"bandon-checkpoint")
    s2_checkpoint.write_bytes(b"s2looking-checkpoint")
    backend = resolve_backend(PipelineExecutionConfig(model_backend="bandon_mps"), settings=Settings())

    bandon_settings = Settings(
        inference_backend="bandon_mps",
        bandon_checkpoint_path=bandon_checkpoint,
        default_change_threshold=0.35,
    )
    s2_settings = Settings(
        inference_backend="mtgcdnet_s2looking_mps",
        bandon_checkpoint_path=bandon_checkpoint,
        s2looking_checkpoint_path=s2_checkpoint,
        s2looking_change_threshold=0.50,
    )
    threshold_settings = Settings(
        inference_backend="bandon_mps",
        bandon_checkpoint_path=bandon_checkpoint,
        default_change_threshold=0.45,
    )

    bandon_context = backend.request_hash_context(bandon_settings)
    s2_context = backend.request_hash_context(s2_settings)
    threshold_context = backend.request_hash_context(threshold_settings)

    assert bandon_context["effective_backend"] == "bandon_mps"
    assert s2_context["effective_backend"] == "mtgcdnet_s2looking_mps"
    assert bandon_context["bandon_checkpoint_sha256"] != s2_context["bandon_checkpoint_sha256"]
    assert bandon_context["change_threshold"] != threshold_context["change_threshold"]


def test_prepared_request_hash_changes_when_backend_identity_changes(tmp_path) -> None:
    bandon_checkpoint = tmp_path / "bandon.pth"
    s2_checkpoint = tmp_path / "s2looking.pth"
    bandon_checkpoint.write_bytes(b"bandon-checkpoint")
    s2_checkpoint.write_bytes(b"s2looking-checkpoint")
    backend = resolve_backend(PipelineExecutionConfig(model_backend="bandon_mps"), settings=Settings())
    request = _sample_request()

    bandon_settings = Settings(
        inference_backend="bandon_mps",
        bandon_checkpoint_path=bandon_checkpoint,
        full_limits={"name": "full_run", "label": "Full Run", "max_area_m2": 2_000_000.0, "max_scene_tiles": 400, "max_remote_patches_per_scene": 1},
    )
    s2_settings = Settings(
        inference_backend="mtgcdnet_s2looking_mps",
        bandon_checkpoint_path=bandon_checkpoint,
        s2looking_checkpoint_path=s2_checkpoint,
        s2looking_change_threshold=0.50,
        full_limits={"name": "full_run", "label": "Full Run", "max_area_m2": 2_000_000.0, "max_scene_tiles": 400, "max_remote_patches_per_scene": 1},
    )

    _, bandon_prepared = validate_request(
        request,
        releases=_sample_releases(bandon_settings),
        settings=backend.configure_settings(bandon_settings),
        remote_patch_budget_enabled=False,
        request_hash_context=backend.request_hash_context(bandon_settings),
    )
    _, s2_prepared = validate_request(
        request,
        releases=_sample_releases(s2_settings),
        settings=backend.configure_settings(s2_settings),
        remote_patch_budget_enabled=False,
        request_hash_context=backend.request_hash_context(s2_settings),
    )

    assert bandon_prepared is not None
    assert s2_prepared is not None
    assert bandon_prepared.request_hash != s2_prepared.request_hash
