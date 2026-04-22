from __future__ import annotations

from src.config import Settings
from src.routes import _build_execution_config, create_app
from src.schemas import RunRequest


def _sample_request(**overrides):
    payload = {
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
        "change_threshold": 0.5,
        "semantic_threshold": 0.5,
        "buffer_distances_m": [10.0, 15.0, 20.0],
    }
    payload.update(overrides)
    return RunRequest.model_validate(payload)


def test_build_execution_config_uses_bandon_profile() -> None:
    settings = Settings()
    request = _sample_request(model_backend="bandon_mps")
    execution_config = _build_execution_config(request, settings)
    assert execution_config.model_backend == "bandon_mps"


def test_build_execution_config_uses_requested_sam3_mode() -> None:
    settings = Settings()
    request = _sample_request(model_backend="sam3", sam3_backend_mode="huggingface_gpu")
    execution_config = _build_execution_config(request, settings)
    assert execution_config.model_backend == "sam3"
    assert execution_config.backend_mode == "huggingface_gpu"


def test_build_execution_config_falls_back_to_settings_default() -> None:
    settings = Settings(model_backend_default="bandon_mps")
    request = _sample_request()
    execution_config = _build_execution_config(request, settings)
    assert execution_config.model_backend == "bandon_mps"


def test_create_app_exposes_backend_probe_endpoint() -> None:
    app = create_app(Settings())
    config = app.get_config_file()
    api_names = [dependency.get("api_name") for dependency in config.get("dependencies", []) if dependency.get("api_name")]
    assert "probe_backends" in api_names
    assert "list_temporal_projects" in api_names
    assert "get_temporal_project" in api_names
    assert "save_temporal_project" in api_names
    assert "validate_temporal_project" in api_names
    assert "run_temporal_project" in api_names
    assert "import_temporal_override" in api_names
