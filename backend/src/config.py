from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


ModeName = Literal["fast_preview", "full_run"]
ModelBackendName = Literal["sam3", "bandon_mps"]


class ModeLimits(BaseModel):
    name: ModeName
    label: str
    max_area_m2: float
    max_scene_tiles: int
    max_remote_patches_per_scene: int


class Settings(BaseModel):
    project_root: Path = Field(default_factory=lambda: Path(__file__).resolve().parents[2])
    runtime_cache_dir: Path = Field(default_factory=lambda: Path(__file__).resolve().parents[1] / "runtime_cache")
    model_backend_default: ModelBackendName = "sam3"
    wmts_capabilities_url: str = (
        "https://wayback.maptiles.arcgis.com/arcgis/rest/services/"
        "World_Imagery/MapServer/WMTS/1.0.0/WMTSCapabilities.xml"
    )
    tile_matrix_set: str = "default028mm"
    zoom: int = 19
    request_timeout_sec: int = 120
    download_workers: int = 6
    download_retries: int = 3
    download_retry_backoff_initial_sec: float = 1.0
    download_retry_backoff_max_sec: float = 8.0
    metadata_grid_size: int = 5
    wayback_metadata_workers: int = 10
    wayback_tilemap_preflight_enabled: bool = True
    patch_size: int = 1024
    stride: int = 768
    scene_segmentation_concurrency: int = 2
    arosics_enabled: bool = True
    arosics_fallback_to_reprojection: bool = True
    arosics_grid_res: int = 128
    arosics_window_size: int = 256
    arosics_max_shift: int = 10
    arosics_min_reliability: int = 60
    arosics_tieP_filter_level: int = 3
    arosics_resamp_alg_calc: str = "cubic"
    arosics_resamp_alg_deshift: str = "cubic"
    arosics_align_grids: bool = True
    arosics_match_gsd: bool = False
    arosics_cpus: int = 2
    default_change_threshold: float = 0.65
    default_semantic_threshold: float = 0.50
    default_min_new_building_pixels: int = 50
    default_old_building_mask_dilation_pixels: int = 2
    default_new_building_core_distance_pixels: int = 2
    default_merge_close_gap_m: float = 10.0
    default_building_block_gap_m: float = 25.0
    default_buffer_distances_m: tuple[float, ...] = (10.0, 15.0, 20.0)
    remote_segmentation_provider_max_concurrent_requests: int = 1
    remote_segmentation_max_parallel_patches: int = 4
    remote_segmentation_space: str = "prithivMLmods/SAM3-Demo"
    remote_segmentation_spaces: tuple[str, ...] = (
        "prithivMLmods/SAM3-Demo",
        "Arrcttacsrks/SAM3-Demo",
        "Translsis/SAM3-Demo",
        "thilanC/SAM3-Demo",
        "Zhongyuan1995/SAM3-Demo",
    )
    remote_segmentation_api_name: str = "/run_image_segmentation"
    remote_segmentation_prompt: str = "building"
    remote_segmentation_hf_token: str | None = None
    remote_segmentation_timeout_sec: int = 240
    remote_segmentation_retries: int = 3
    remote_segmentation_client_refresh_retries: int = 2
    remote_segmentation_provider_patience_sec: int = 600
    remote_segmentation_refreshable_provider_cooldown_sec: int = 90
    remote_segmentation_failure_cooldown_sec: int = 45
    remote_segmentation_quota_cooldown_sec: int = 180
    remote_segmentation_invalid_provider_cooldown_sec: int = 900
    bandon_repo_dir: Path = Field(default_factory=lambda: Path(__file__).resolve().parents[2] / "vendor" / "BANDON-mps")
    bandon_env_prefix: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parents[2] / "vendor" / "BANDON-mps" / ".conda-macos-mps"
    )
    bandon_config_path: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parents[2] / "vendor" / "BANDON-mps" / "workdirs_bandon" / "MTGCDNet" / "config.py"
    )
    bandon_checkpoint_path: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parents[2] / "vendor" / "BANDON-mps" / "checkpoints" / "mtgcdnet_iter_40000.pth"
    )
    bandon_device: Literal["mps", "cpu"] = "mps"
    bandon_allow_mps_fallback: bool = False
    preview_limits: ModeLimits = Field(
        default_factory=lambda: ModeLimits(
            name="fast_preview",
            label="Fast Preview",
            max_area_m2=400_000.0,
            max_scene_tiles=64,
            max_remote_patches_per_scene=6,
        )
    )
    full_limits: ModeLimits = Field(
        default_factory=lambda: ModeLimits(
            name="full_run",
            label="Full Run",
            max_area_m2=1_500_000.0,
            max_scene_tiles=225,
            max_remote_patches_per_scene=12,
        )
    )
    gradio_allowed_paths: tuple[Path, ...] = ()

    def model_post_init(self, __context: object) -> None:
        self.runtime_cache_dir.mkdir(parents=True, exist_ok=True)
        outputs_dir = self.runtime_cache_dir / "requests"
        outputs_dir.mkdir(parents=True, exist_ok=True)
        temporal_dir = self.runtime_cache_dir / "temporal_projects"
        temporal_dir.mkdir(parents=True, exist_ok=True)
        if not self.gradio_allowed_paths:
            self.gradio_allowed_paths = (outputs_dir, temporal_dir)
        configured_spaces = self.remote_segmentation_spaces or (self.remote_segmentation_space,)
        if self.remote_segmentation_space and self.remote_segmentation_space not in configured_spaces:
            configured_spaces = (self.remote_segmentation_space, *configured_spaces)
        self.remote_segmentation_spaces = _dedupe_strs(configured_spaces)

    @property
    def request_cache_dir(self) -> Path:
        return self.runtime_cache_dir / "requests"

    @property
    def temporal_projects_dir(self) -> Path:
        return self.runtime_cache_dir / "temporal_projects"

    def get_mode_limits(self, mode: ModeName) -> ModeLimits:
        return self.preview_limits if mode == "fast_preview" else self.full_limits


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    return float(value) if value else default


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value else default


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _tuple_float_env(name: str, default: tuple[float, ...]) -> tuple[float, ...]:
    value = os.getenv(name)
    if not value:
        return default
    return tuple(float(part.strip()) for part in value.split(",") if part.strip())


def _tuple_str_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = os.getenv(name)
    if not value:
        return default
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _optional_str_env(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


def _dedupe_strs(values: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return tuple(ordered)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    base = Settings()
    primary_remote_space = os.getenv("APP_REMOTE_SEGMENTATION_SPACE", base.remote_segmentation_space)
    configured_remote_spaces = _tuple_str_env(
        "APP_REMOTE_SEGMENTATION_SPACES",
        base.remote_segmentation_spaces,
    )
    if primary_remote_space and primary_remote_space not in configured_remote_spaces:
        configured_remote_spaces = (primary_remote_space, *configured_remote_spaces)
    return Settings(
        project_root=base.project_root,
        runtime_cache_dir=Path(os.getenv("APP_RUNTIME_CACHE_DIR", str(base.runtime_cache_dir))),
        model_backend_default=os.getenv("APP_MODEL_BACKEND_DEFAULT", base.model_backend_default),  # type: ignore[arg-type]
        wmts_capabilities_url=os.getenv("APP_WMTS_CAPABILITIES_URL", base.wmts_capabilities_url),
        tile_matrix_set=os.getenv("APP_TILE_MATRIX_SET", base.tile_matrix_set),
        zoom=_int_env("APP_TILE_ZOOM", base.zoom),
        request_timeout_sec=_int_env("APP_REQUEST_TIMEOUT_SEC", base.request_timeout_sec),
        download_workers=_int_env("APP_DOWNLOAD_WORKERS", base.download_workers),
        download_retries=_int_env("APP_DOWNLOAD_RETRIES", base.download_retries),
        download_retry_backoff_initial_sec=_float_env(
            "APP_DOWNLOAD_RETRY_BACKOFF_INITIAL_SEC",
            base.download_retry_backoff_initial_sec,
        ),
        download_retry_backoff_max_sec=_float_env(
            "APP_DOWNLOAD_RETRY_BACKOFF_MAX_SEC",
            base.download_retry_backoff_max_sec,
        ),
        metadata_grid_size=_int_env("APP_METADATA_GRID_SIZE", base.metadata_grid_size),
        wayback_metadata_workers=_int_env("APP_WAYBACK_METADATA_WORKERS", base.wayback_metadata_workers),
        wayback_tilemap_preflight_enabled=_bool_env(
            "APP_WAYBACK_TILEMAP_PREFLIGHT_ENABLED",
            base.wayback_tilemap_preflight_enabled,
        ),
        patch_size=_int_env("APP_PATCH_SIZE", base.patch_size),
        stride=_int_env("APP_STRIDE", base.stride),
        scene_segmentation_concurrency=_int_env(
            "APP_SCENE_SEGMENTATION_CONCURRENCY",
            base.scene_segmentation_concurrency,
        ),
        arosics_enabled=_bool_env("APP_AROSICS_ENABLED", base.arosics_enabled),
        arosics_fallback_to_reprojection=_bool_env(
            "APP_AROSICS_FALLBACK_TO_REPROJECTION",
            base.arosics_fallback_to_reprojection,
        ),
        arosics_grid_res=_int_env("APP_AROSICS_GRID_RES", base.arosics_grid_res),
        arosics_window_size=_int_env("APP_AROSICS_WINDOW_SIZE", base.arosics_window_size),
        arosics_max_shift=_int_env("APP_AROSICS_MAX_SHIFT", base.arosics_max_shift),
        arosics_min_reliability=_int_env(
            "APP_AROSICS_MIN_RELIABILITY",
            base.arosics_min_reliability,
        ),
        arosics_tieP_filter_level=_int_env(
            "APP_AROSICS_TIEP_FILTER_LEVEL",
            base.arosics_tieP_filter_level,
        ),
        arosics_resamp_alg_calc=os.getenv(
            "APP_AROSICS_RESAMP_ALG_CALC",
            base.arosics_resamp_alg_calc,
        ),
        arosics_resamp_alg_deshift=os.getenv(
            "APP_AROSICS_RESAMP_ALG_DESHIFT",
            base.arosics_resamp_alg_deshift,
        ),
        arosics_align_grids=_bool_env(
            "APP_AROSICS_ALIGN_GRIDS",
            base.arosics_align_grids,
        ),
        arosics_match_gsd=_bool_env(
            "APP_AROSICS_MATCH_GSD",
            base.arosics_match_gsd,
        ),
        arosics_cpus=_int_env("APP_AROSICS_CPUS", base.arosics_cpus),
        default_change_threshold=_float_env("APP_CHANGE_THRESHOLD", base.default_change_threshold),
        default_semantic_threshold=_float_env("APP_SEMANTIC_THRESHOLD", base.default_semantic_threshold),
        default_min_new_building_pixels=_int_env(
            "APP_MIN_NEW_BUILDING_PIXELS",
            base.default_min_new_building_pixels,
        ),
        default_old_building_mask_dilation_pixels=_int_env(
            "APP_OLD_BUILDING_MASK_DILATION_PIXELS",
            base.default_old_building_mask_dilation_pixels,
        ),
        default_new_building_core_distance_pixels=_int_env(
            "APP_NEW_BUILDING_CORE_DISTANCE_PIXELS",
            base.default_new_building_core_distance_pixels,
        ),
        default_merge_close_gap_m=_float_env(
            "APP_MERGE_CLOSE_GAP_M",
            base.default_merge_close_gap_m,
        ),
        default_building_block_gap_m=_float_env(
            "APP_BUILDING_BLOCK_GAP_M",
            base.default_building_block_gap_m,
        ),
        default_buffer_distances_m=_tuple_float_env(
            "APP_BUFFER_DISTANCES_M",
            base.default_buffer_distances_m,
        ),
        remote_segmentation_space=os.getenv(
            "APP_REMOTE_SEGMENTATION_SPACE",
            base.remote_segmentation_space,
        ),
        remote_segmentation_spaces=configured_remote_spaces,
        remote_segmentation_api_name=os.getenv(
            "APP_REMOTE_SEGMENTATION_API_NAME",
            base.remote_segmentation_api_name,
        ),
        remote_segmentation_prompt=os.getenv(
            "APP_REMOTE_SEGMENTATION_PROMPT",
            base.remote_segmentation_prompt,
        ),
        remote_segmentation_hf_token=_optional_str_env(
            "APP_REMOTE_SEGMENTATION_HF_TOKEN",
            "HF_TOKEN",
            "HUGGINGFACEHUB_API_TOKEN",
        ),
        remote_segmentation_timeout_sec=_int_env(
            "APP_REMOTE_SEGMENTATION_TIMEOUT_SEC",
            base.remote_segmentation_timeout_sec,
        ),
        remote_segmentation_provider_max_concurrent_requests=_int_env(
            "APP_REMOTE_SEGMENTATION_PROVIDER_MAX_CONCURRENT_REQUESTS",
            base.remote_segmentation_provider_max_concurrent_requests,
        ),
        remote_segmentation_max_parallel_patches=_int_env(
            "APP_REMOTE_SEGMENTATION_MAX_PARALLEL_PATCHES",
            base.remote_segmentation_max_parallel_patches,
        ),
        remote_segmentation_retries=_int_env(
            "APP_REMOTE_SEGMENTATION_RETRIES",
            base.remote_segmentation_retries,
        ),
        remote_segmentation_client_refresh_retries=_int_env(
            "APP_REMOTE_SEGMENTATION_CLIENT_REFRESH_RETRIES",
            base.remote_segmentation_client_refresh_retries,
        ),
        remote_segmentation_provider_patience_sec=_int_env(
            "APP_REMOTE_SEGMENTATION_PROVIDER_PATIENCE_SEC",
            base.remote_segmentation_provider_patience_sec,
        ),
        remote_segmentation_refreshable_provider_cooldown_sec=_int_env(
            "APP_REMOTE_SEGMENTATION_REFRESHABLE_PROVIDER_COOLDOWN_SEC",
            base.remote_segmentation_refreshable_provider_cooldown_sec,
        ),
        remote_segmentation_failure_cooldown_sec=_int_env(
            "APP_REMOTE_SEGMENTATION_FAILURE_COOLDOWN_SEC",
            base.remote_segmentation_failure_cooldown_sec,
        ),
        remote_segmentation_quota_cooldown_sec=_int_env(
            "APP_REMOTE_SEGMENTATION_QUOTA_COOLDOWN_SEC",
            base.remote_segmentation_quota_cooldown_sec,
        ),
        remote_segmentation_invalid_provider_cooldown_sec=_int_env(
            "APP_REMOTE_SEGMENTATION_INVALID_PROVIDER_COOLDOWN_SEC",
            base.remote_segmentation_invalid_provider_cooldown_sec,
        ),
        bandon_repo_dir=Path(os.getenv("APP_BANDON_REPO_DIR", str(base.bandon_repo_dir))),
        bandon_env_prefix=Path(os.getenv("APP_BANDON_ENV_PREFIX", str(base.bandon_env_prefix))),
        bandon_config_path=Path(os.getenv("APP_BANDON_CONFIG_PATH", str(base.bandon_config_path))),
        bandon_checkpoint_path=Path(os.getenv("APP_BANDON_CHECKPOINT_PATH", str(base.bandon_checkpoint_path))),
        bandon_device=os.getenv("APP_BANDON_DEVICE", base.bandon_device),  # type: ignore[arg-type]
        bandon_allow_mps_fallback=_bool_env(
            "APP_BANDON_ALLOW_MPS_FALLBACK",
            base.bandon_allow_mps_fallback,
        ),
        preview_limits=ModeLimits(
            name="fast_preview",
            label="Fast Preview",
            max_area_m2=_float_env("APP_FAST_PREVIEW_MAX_AREA_M2", base.preview_limits.max_area_m2),
            max_scene_tiles=_int_env(
                "APP_FAST_PREVIEW_MAX_SCENE_TILES",
                base.preview_limits.max_scene_tiles,
            ),
            max_remote_patches_per_scene=_int_env(
                "APP_FAST_PREVIEW_MAX_REMOTE_PATCHES",
                base.preview_limits.max_remote_patches_per_scene,
            ),
        ),
        full_limits=ModeLimits(
            name="full_run",
            label="Full Run",
            max_area_m2=_float_env("APP_FULL_RUN_MAX_AREA_M2", base.full_limits.max_area_m2),
            max_scene_tiles=_int_env("APP_FULL_RUN_MAX_SCENE_TILES", base.full_limits.max_scene_tiles),
            max_remote_patches_per_scene=_int_env(
                "APP_FULL_RUN_MAX_REMOTE_PATCHES",
                base.full_limits.max_remote_patches_per_scene,
            ),
        ),
    )
