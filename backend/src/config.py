from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


ModeName = Literal["fast_preview", "full_run"]
InferenceBackendName = Literal["bandon_mps", "mtgcdnet_s2looking_mps"]
PersistenceBackendName = Literal["filesystem", "postgres"]
PostCompletionRequestCleanupMode = Literal["off", "compact_heavy", "delete_full"]


class ModeLimits(BaseModel):
    name: ModeName
    label: str
    max_area_m2: float
    max_scene_tiles: int
    max_inference_patches_per_scene: int


class Settings(BaseModel):
    project_root: Path = Field(default_factory=lambda: Path(__file__).resolve().parents[2])
    runtime_cache_dir: Path = Field(default_factory=lambda: Path(__file__).resolve().parents[1] / "runtime_cache")
    inference_backend: str = "bandon_mps"
    wmts_capabilities_url: str = (
        "https://wayback.maptiles.arcgis.com/arcgis/rest/services/"
        "World_Imagery/MapServer/WMTS/1.0.0/WMTSCapabilities.xml"
    )
    tile_matrix_set: str = "default028mm"
    zoom: int = 18
    min_zoom: int = 17
    wayback_preferred_inference_zoom: int = 18
    request_timeout_sec: int = 120
    download_workers: int = 6
    download_retries: int = 3
    download_retry_backoff_initial_sec: float = 1.0
    download_retry_backoff_max_sec: float = 8.0
    metadata_grid_size: int = 5
    wayback_metadata_workers: int = 10
    wayback_releases_cache_enabled: bool = True
    wayback_releases_cache_ttl_seconds: int = 86400
    wayback_releases_stale_if_error_enabled: bool = True
    wayback_releases_cache_path: Path | None = None
    wayback_capabilities_cache_path: Path | None = None
    wayback_releases_connect_timeout_seconds: int = 20
    wayback_releases_read_timeout_seconds: int = 60
    wayback_releases_retries: int = 4
    wayback_releases_retry_backoff_seconds: float = 1.0
    wayback_http_connect_timeout_seconds: int = 20
    wayback_http_read_timeout_seconds: int = 60
    wayback_http_max_retries: int = 4
    wayback_http_backoff_base_seconds: float = 1.0
    wayback_tile_min_concurrency: int = 4
    wayback_tile_max_concurrency: int = 12
    wayback_tile_progress_every_tiles: int = 50
    wayback_tile_progress_every_seconds: float = 5.0
    wayback_tile_cache_backend: Literal["file", "sqlite"] = "sqlite"
    wayback_tile_sqlite_wal: bool = True
    wayback_tile_sqlite_batch_insert_size: int = 100
    wayback_max_missing_tile_ratio: float = 0.05
    wayback_tile_cache_dir: Path | None = None
    wayback_tile_sqlite_cache_dir: Path | None = None
    wayback_tile_cache_service_enabled: bool = False
    wayback_tile_cache_service_url: str = ""
    wayback_tile_cache_service_kind: str = "mapproxy"
    wayback_heavy_batch_tile_threshold: int = 2000
    wayback_tilemap_preflight_enabled: bool = True
    wayback_metadata_cache_enabled: bool = True
    wayback_metadata_cache_ttl_seconds: int = 604800
    wayback_metadata_cache_dir: Path | None = None
    wayback_tile_preflight_cache_enabled: bool = True
    wayback_tile_preflight_cache_ttl_seconds: int = 604800
    wayback_tile_preflight_cache_dir: Path | None = None
    mapbox_access_token: str | None = None
    mapbox_satellite_tileset: str = "mapbox.satellite"
    mapbox_current_imagery_enabled: bool = False
    mapbox_current_imagery_cache_dir: Path | None = None
    mapbox_current_imagery_format: str = "jpg90"
    mapbox_current_imagery_max_zoom: int = 18
    mapbox_current_imagery_default_zoom: int = 18
    mapbox_current_imagery_timeout_seconds: int = 30
    mapbox_max_tiles_per_request: int = 1024
    mapbox_current_imagery_max_tiles: int = 1024
    reference_tile_cache_dir: Path | None = None
    reference_tile_prewarm_max_tiles: int = 256
    reference_imagery_cache_enabled: bool = True
    reference_imagery_cache_dir: Path | None = None
    reference_imagery_materialization: Literal["hardlink", "symlink", "copy"] = "hardlink"
    patch_size: int = 1024
    stride: int = 768
    inference_tiled_mode_auto: bool = True
    inference_tile_size: int = 1024
    inference_tile_overlap: int = 128
    inference_tile_batch_size: int = 1
    inference_max_in_memory_pixels: int = 25_000_000
    inference_heavy_batch_tile_threshold: int = 2000
    inference_disable_full_preview_png_for_heavy_batch: bool = True
    generate_full_mosaic_png_for_heavy_batch: bool = False
    mosaic_preview_max_dimension: int = 4096
    scene_segmentation_concurrency: int = 2
    default_change_threshold: float = 0.65
    default_semantic_threshold: float = 0.65
    default_min_new_building_pixels: int = 30
    addition_min_area_m2: float = 8.0
    addition_max_existing_overlap_ratio: float = 0.50
    addition_thin_artifact_max_area_m2: float = 80.0
    addition_thinness_min_ratio: float = 0.20
    addition_edge_buffer_m: float = 2.0
    addition_max_edge_overlap_ratio: float = 0.60
    addition_thin_artifact_max_mean_probability: float = 0.75
    default_old_building_mask_dilation_pixels: int = 2
    default_new_building_core_distance_pixels: int = 2
    default_merge_close_gap_m: float = 10.0
    default_building_block_gap_m: float = 25.0
    default_buffer_distances_m: tuple[float, ...] = (10.0, 15.0, 20.0)
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
    bandon_skip_invalid_crops: bool = True
    bandon_skip_outside_aoi_crops: bool = True
    bandon_skip_nodata_crops: bool = True
    bandon_min_valid_ratio_within_aoi: float = 0.01
    s2looking_checkpoint_path: Path | None = None
    s2looking_change_threshold: float = 0.65
    database_url: str = "postgresql+psycopg://building_change:building_change@localhost:5432/building_change"
    database_echo: bool = False
    db_inline_json_max_bytes: int = 256 * 1024
    persistence_backend: PersistenceBackendName = "filesystem"
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str | None = None
    celery_result_backend: str | None = None
    celery_task_default_queue: str = "building_change"
    celery_worker_pool: str = "solo"
    celery_worker_concurrency: int = 1
    celery_task_acks_late: bool = False
    celery_task_reject_on_worker_lost: bool = False
    celery_worker_prefetch_multiplier: int = 1
    jobs_enabled: bool = True
    keep_intermediate_artifacts: bool = False
    materialize_source_imagery_in_requests: bool = False
    post_completion_request_cleanup_enabled: bool = True
    post_completion_request_cleanup_mode: PostCompletionRequestCleanupMode = "compact_heavy"
    post_completion_request_cleanup_grace_seconds: int = 300
    post_completion_request_cleanup_keep_provenance: bool = True
    post_completion_request_cleanup_delete_export_bundle: bool = True
    enable_client_log_relay: bool = True
    temporal_imagery_prefetch_enabled: bool = False
    temporal_imagery_prefetch_workers: int = 2
    temporal_imagery_prefetch_max_pairs: int = 4
    temporal_imagery_prefetch_timeout_seconds: int = 600
    temporal_imagery_prefetch_reduce_provider_workers: bool = True
    reference_layer_max_upload_bytes: int = 2_147_483_648
    reference_layer_browser_geojson_max_bytes: int = 5_000_000
    reference_layer_browser_geojson_max_features: int = 25_000
    reference_layer_large_vector_input_threshold_bytes: int = 50_000_000
    reference_layer_pmtiles_enabled: bool = True
    reference_layer_pmtiles_max_upload_mb: int = 2048
    reference_layer_pmtiles_min_full_layer_mb: int = 25
    reference_layer_pmtiles_tippecanoe_bin: str = "tippecanoe"
    reference_layer_pmtiles_cli_bin: str = "pmtiles"
    reference_layer_pmtiles_max_zoom: int = 14
    reference_layer_pmtiles_min_zoom: int = 0
    reference_layer_pmtiles_default_layer_name: str = "reference_layer"
    reference_layer_pmtiles_build_timeout_seconds: int = 900
    reference_layer_pmtiles_keep_intermediate: bool = False
    cors_allowed_origins: tuple[str, ...] = (
        "http://localhost:5173",
        "http://localhost:5174",
        "http://localhost:5175",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5174",
        "http://127.0.0.1:5175",
    )
    cors_allow_origin_regex: str | None = r"http://(localhost|127\.0\.0\.1):\d+"
    preview_limits: ModeLimits = Field(
        default_factory=lambda: ModeLimits(
            name="fast_preview",
            label="Fast Preview",
            max_area_m2=400_000.0,
            max_scene_tiles=64,
            max_inference_patches_per_scene=6,
        )
    )
    full_limits: ModeLimits = Field(
        default_factory=lambda: ModeLimits(
            name="full_run",
            label="Full Run",
            max_area_m2=1_500_000.0,
            max_scene_tiles=225,
            max_inference_patches_per_scene=12,
        )
    )
    allowed_file_roots: tuple[Path, ...] = ()

    def model_post_init(self, __context: object) -> None:
        if self.wayback_metadata_cache_dir is None:
            self.wayback_metadata_cache_dir = self.runtime_cache_dir / "wayback_metadata_cache"
        if self.wayback_releases_cache_path is None:
            self.wayback_releases_cache_path = self.runtime_cache_dir / "wayback_releases" / "releases_cache.json"
        if self.wayback_capabilities_cache_path is None:
            self.wayback_capabilities_cache_path = self.runtime_cache_dir / "wayback_releases" / "WMTSCapabilities.xml"
        if self.wayback_tile_preflight_cache_dir is None:
            self.wayback_tile_preflight_cache_dir = self.runtime_cache_dir / "wayback_tile_preflight_cache"
        if self.wayback_tile_cache_dir is None:
            self.wayback_tile_cache_dir = self.runtime_cache_dir / "wayback_tiles"
        if self.wayback_tile_sqlite_cache_dir is None:
            self.wayback_tile_sqlite_cache_dir = self.runtime_cache_dir / "wayback_tile_cache"
        if self.wayback_preferred_inference_zoom == 18 and self.zoom != 18:
            self.wayback_preferred_inference_zoom = self.zoom
        if self.mapbox_current_imagery_cache_dir is None:
            self.mapbox_current_imagery_cache_dir = self.runtime_cache_dir / "mapbox_mosaics"
        if self.reference_tile_cache_dir is None:
            self.reference_tile_cache_dir = self.runtime_cache_dir / "reference_tiles"
        if self.reference_imagery_cache_dir is None:
            self.reference_imagery_cache_dir = self.runtime_cache_dir / "imagery_cache"
        if self.reference_tile_prewarm_max_tiles < 1:
            raise ValueError("reference_tile_prewarm_max_tiles must be greater than or equal to 1.")
        if self.mapbox_max_tiles_per_request < 1:
            raise ValueError("mapbox_max_tiles_per_request must be greater than or equal to 1.")
        if self.wayback_http_connect_timeout_seconds < 1:
            raise ValueError("wayback_http_connect_timeout_seconds must be greater than or equal to 1.")
        if self.wayback_http_read_timeout_seconds < 1:
            raise ValueError("wayback_http_read_timeout_seconds must be greater than or equal to 1.")
        if self.wayback_http_max_retries < 0:
            raise ValueError("wayback_http_max_retries must be greater than or equal to 0.")
        if self.wayback_http_backoff_base_seconds < 0:
            raise ValueError("wayback_http_backoff_base_seconds must be greater than or equal to 0.")
        if self.wayback_preferred_inference_zoom < self.min_zoom:
            raise ValueError("wayback_preferred_inference_zoom must be greater than or equal to min_zoom.")
        if self.wayback_tile_min_concurrency < 1:
            raise ValueError("wayback_tile_min_concurrency must be greater than or equal to 1.")
        if self.wayback_tile_max_concurrency < 1:
            raise ValueError("wayback_tile_max_concurrency must be greater than or equal to 1.")
        if self.wayback_tile_max_concurrency < self.wayback_tile_min_concurrency:
            raise ValueError("wayback_tile_max_concurrency must be greater than or equal to wayback_tile_min_concurrency.")
        if self.wayback_tile_progress_every_tiles < 1:
            raise ValueError("wayback_tile_progress_every_tiles must be greater than or equal to 1.")
        if self.wayback_tile_progress_every_seconds <= 0:
            raise ValueError("wayback_tile_progress_every_seconds must be greater than 0.")
        if self.wayback_tile_sqlite_batch_insert_size < 1:
            raise ValueError("wayback_tile_sqlite_batch_insert_size must be greater than or equal to 1.")
        if self.wayback_heavy_batch_tile_threshold < 1:
            raise ValueError("wayback_heavy_batch_tile_threshold must be greater than or equal to 1.")
        if self.inference_tile_size < 128:
            raise ValueError("inference_tile_size must be greater than or equal to 128.")
        if self.inference_tile_overlap < 0:
            raise ValueError("inference_tile_overlap must be greater than or equal to 0.")
        if self.inference_tile_overlap * 2 >= self.inference_tile_size:
            raise ValueError("inference_tile_overlap must be less than half of inference_tile_size.")
        if self.inference_tile_batch_size < 1:
            raise ValueError("inference_tile_batch_size must be greater than or equal to 1.")
        if self.inference_max_in_memory_pixels < 1:
            raise ValueError("inference_max_in_memory_pixels must be greater than or equal to 1.")
        if self.inference_heavy_batch_tile_threshold < 1:
            raise ValueError("inference_heavy_batch_tile_threshold must be greater than or equal to 1.")
        if self.mosaic_preview_max_dimension < 256:
            raise ValueError("mosaic_preview_max_dimension must be greater than or equal to 256.")
        if self.wayback_max_missing_tile_ratio < 0 or self.wayback_max_missing_tile_ratio > 1:
            raise ValueError("wayback_max_missing_tile_ratio must be between 0 and 1.")
        if self.inference_backend not in {"bandon_mps", "mtgcdnet_s2looking_mps"}:
            raise ValueError(
                "APP_INFERENCE_BACKEND must be one of: bandon_mps, mtgcdnet_s2looking_mps."
            )
        if self.s2looking_change_threshold < 0 or self.s2looking_change_threshold > 1:
            raise ValueError("s2looking_change_threshold must be between 0 and 1.")
        if self.s2looking_checkpoint_path is not None:
            s2looking_checkpoint_path = self.s2looking_checkpoint_path.expanduser()
            if not s2looking_checkpoint_path.is_absolute():
                s2looking_checkpoint_path = (self.project_root / s2looking_checkpoint_path).resolve()
            else:
                s2looking_checkpoint_path = s2looking_checkpoint_path.resolve()
            self.s2looking_checkpoint_path = s2looking_checkpoint_path
        if self.inference_backend == "mtgcdnet_s2looking_mps":
            if self.s2looking_checkpoint_path is None:
                raise ValueError(
                    "APP_S2LOOKING_CHECKPOINT_PATH is required when "
                    "APP_INFERENCE_BACKEND=mtgcdnet_s2looking_mps."
                )
            if not self.s2looking_checkpoint_path.is_file():
                raise ValueError(
                    "APP_S2LOOKING_CHECKPOINT_PATH does not point to an existing file: "
                    f"{self.s2looking_checkpoint_path}"
                )
        if not 1 <= self.temporal_imagery_prefetch_workers <= 4:
            raise ValueError("temporal_imagery_prefetch_workers must be between 1 and 4.")
        if self.temporal_imagery_prefetch_max_pairs < 1:
            raise ValueError("temporal_imagery_prefetch_max_pairs must be greater than or equal to 1.")
        if self.temporal_imagery_prefetch_timeout_seconds < 30:
            raise ValueError("temporal_imagery_prefetch_timeout_seconds must be greater than or equal to 30.")
        if self.reference_layer_pmtiles_min_zoom < 0 or self.reference_layer_pmtiles_min_zoom > self.reference_layer_pmtiles_max_zoom:
            raise ValueError("reference_layer_pmtiles_min_zoom must be between 0 and reference_layer_pmtiles_max_zoom.")
        if self.reference_layer_pmtiles_max_zoom > 22:
            raise ValueError("reference_layer_pmtiles_max_zoom must be less than or equal to 22.")
        if self.reference_layer_pmtiles_build_timeout_seconds < 30:
            raise ValueError("reference_layer_pmtiles_build_timeout_seconds must be greater than or equal to 30.")
        if self.reference_layer_pmtiles_max_upload_mb < 1:
            raise ValueError("reference_layer_pmtiles_max_upload_mb must be greater than 0.")
        if self.bandon_min_valid_ratio_within_aoi < 0 or self.bandon_min_valid_ratio_within_aoi > 1:
            raise ValueError("bandon_min_valid_ratio_within_aoi must be between 0 and 1.")
        ratio_values = {
            "addition_max_existing_overlap_ratio": self.addition_max_existing_overlap_ratio,
            "addition_thinness_min_ratio": self.addition_thinness_min_ratio,
            "addition_max_edge_overlap_ratio": self.addition_max_edge_overlap_ratio,
            "addition_thin_artifact_max_mean_probability": self.addition_thin_artifact_max_mean_probability,
        }
        for name, value in ratio_values.items():
            if value < 0 or value > 1:
                raise ValueError(f"{name} must be between 0 and 1.")
        if self.addition_min_area_m2 < 0:
            raise ValueError("addition_min_area_m2 must be greater than or equal to 0.")
        if self.addition_thin_artifact_max_area_m2 < 0:
            raise ValueError("addition_thin_artifact_max_area_m2 must be greater than or equal to 0.")
        if self.addition_edge_buffer_m < 0:
            raise ValueError("addition_edge_buffer_m must be greater than or equal to 0.")
        if self.post_completion_request_cleanup_mode not in {"off", "compact_heavy", "delete_full"}:
            raise ValueError("APP_POST_COMPLETION_REQUEST_CLEANUP_MODE must be one of: off, compact_heavy, delete_full.")
        if self.post_completion_request_cleanup_grace_seconds < 0:
            raise ValueError("APP_POST_COMPLETION_REQUEST_CLEANUP_GRACE_SECONDS must be greater than or equal to 0.")
        self.ensure_runtime_cache_dirs()
        if not self.allowed_file_roots:
            self.allowed_file_roots = (self.request_cache_dir, self.temporal_projects_dir)

    def ensure_runtime_cache_dirs(self) -> None:
        self.runtime_cache_dir.mkdir(parents=True, exist_ok=True)
        self.request_cache_dir.mkdir(parents=True, exist_ok=True)
        self.temporal_projects_dir.mkdir(parents=True, exist_ok=True)
        self.wayback_mosaic_cache_dir.mkdir(parents=True, exist_ok=True)
        self.wayback_releases_cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.wayback_capabilities_cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.wayback_metadata_cache_dir.mkdir(parents=True, exist_ok=True)
        self.wayback_tile_preflight_cache_dir.mkdir(parents=True, exist_ok=True)
        self.wayback_tile_cache_dir.mkdir(parents=True, exist_ok=True)
        self.wayback_tile_sqlite_cache_dir.mkdir(parents=True, exist_ok=True)
        self.mapbox_current_imagery_cache_dir.mkdir(parents=True, exist_ok=True)
        self.reference_tile_cache_dir.mkdir(parents=True, exist_ok=True)
        self.reference_imagery_cache_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_cache_dir.mkdir(parents=True, exist_ok=True)

    @property
    def request_cache_dir(self) -> Path:
        return self.runtime_cache_dir / "requests"

    @property
    def temporal_projects_dir(self) -> Path:
        return self.runtime_cache_dir / "temporal_projects"

    @property
    def wayback_mosaic_cache_dir(self) -> Path:
        return self.runtime_cache_dir / "wayback_mosaics"

    @property
    def tmp_cache_dir(self) -> Path:
        return self.runtime_cache_dir / "tmp"

    @property
    def tile_zoom(self) -> int:
        return self.zoom

    @property
    def wayback_default_zoom(self) -> int:
        return self.zoom

    def get_mode_limits(self, mode: ModeName) -> ModeLimits:
        return self.preview_limits if mode == "fast_preview" else self.full_limits


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    return float(value) if value else default


def _float_env_any(names: tuple[str, ...], default: float) -> float:
    for name in names:
        value = os.getenv(name)
        if value:
            return float(value)
    return default


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value else default


def _int_env_any(names: tuple[str, ...], default: int) -> int:
    for name in names:
        value = os.getenv(name)
        if value:
            return int(value)
    return default


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _bool_env_any(names: tuple[str, ...], default: bool) -> bool:
    for name in names:
        value = os.getenv(name)
        if value is not None:
            return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


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


def _load_env_file(path: Path, protected_keys: set[str]) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in protected_keys:
            continue
        os.environ[key] = value.strip().strip('"').strip("'")


def _load_backend_env_files() -> None:
    protected_keys = set(os.environ)
    backend_root = Path(__file__).resolve().parents[1]
    _load_env_file(backend_root / ".env", protected_keys)
    _load_env_file(backend_root / ".env.local", protected_keys)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    _load_backend_env_files()
    base = Settings()
    preferred_wayback_zoom = _int_env_any(
        ("APP_WAYBACK_PREFERRED_INFERENCE_ZOOM", "APP_WAYBACK_DEFAULT_ZOOM", "APP_TILE_ZOOM"),
        base.wayback_preferred_inference_zoom,
    )
    return Settings(
        project_root=base.project_root,
        runtime_cache_dir=Path(os.getenv("APP_RUNTIME_CACHE_DIR", str(base.runtime_cache_dir))),
        inference_backend=os.getenv("APP_INFERENCE_BACKEND", base.inference_backend),  # type: ignore[arg-type]
        wmts_capabilities_url=os.getenv("APP_WMTS_CAPABILITIES_URL", base.wmts_capabilities_url),
        tile_matrix_set=os.getenv("APP_TILE_MATRIX_SET", base.tile_matrix_set),
        zoom=preferred_wayback_zoom,
        min_zoom=_int_env("APP_TILE_MIN_ZOOM", base.min_zoom),
        wayback_preferred_inference_zoom=preferred_wayback_zoom,
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
        wayback_releases_cache_enabled=_bool_env_any(
            ("WAYBACK_RELEASES_CACHE_ENABLED", "APP_WAYBACK_RELEASES_CACHE_ENABLED"),
            base.wayback_releases_cache_enabled,
        ),
        wayback_releases_cache_ttl_seconds=_int_env(
            "WAYBACK_RELEASES_CACHE_TTL_SECONDS",
            base.wayback_releases_cache_ttl_seconds,
        ),
        wayback_releases_stale_if_error_enabled=_bool_env_any(
            ("WAYBACK_RELEASES_STALE_IF_ERROR_ENABLED", "APP_WAYBACK_RELEASES_STALE_IF_ERROR_ENABLED"),
            base.wayback_releases_stale_if_error_enabled,
        ),
        wayback_releases_cache_path=(
            Path(cache_path_env)
            if (cache_path_env := _optional_str_env("WAYBACK_RELEASES_CACHE_PATH", "APP_WAYBACK_RELEASES_CACHE_PATH"))
            else None
        ),
        wayback_capabilities_cache_path=(
            Path(cache_path_env)
            if (
                cache_path_env := _optional_str_env(
                    "WAYBACK_CAPABILITIES_CACHE_PATH",
                    "APP_WAYBACK_CAPABILITIES_CACHE_PATH",
                )
            )
            else None
        ),
        wayback_releases_connect_timeout_seconds=_int_env_any(
            ("APP_WAYBACK_HTTP_CONNECT_TIMEOUT_SECONDS", "WAYBACK_RELEASES_CONNECT_TIMEOUT_SECONDS"),
            base.wayback_releases_connect_timeout_seconds,
        ),
        wayback_releases_read_timeout_seconds=_int_env_any(
            ("APP_WAYBACK_HTTP_READ_TIMEOUT_SECONDS", "WAYBACK_RELEASES_READ_TIMEOUT_SECONDS"),
            base.wayback_releases_read_timeout_seconds,
        ),
        wayback_releases_retries=_int_env_any(
            ("APP_WAYBACK_HTTP_MAX_RETRIES", "WAYBACK_RELEASES_RETRIES"),
            base.wayback_releases_retries,
        ),
        wayback_releases_retry_backoff_seconds=_float_env(
            "APP_WAYBACK_HTTP_BACKOFF_BASE_SECONDS",
            _float_env("WAYBACK_RELEASES_RETRY_BACKOFF_SECONDS", base.wayback_releases_retry_backoff_seconds),
        ),
        wayback_http_connect_timeout_seconds=_int_env_any(
            ("APP_WAYBACK_TILE_CONNECT_TIMEOUT", "APP_WAYBACK_HTTP_CONNECT_TIMEOUT_SECONDS"),
            base.wayback_http_connect_timeout_seconds,
        ),
        wayback_http_read_timeout_seconds=_int_env_any(
            ("APP_WAYBACK_TILE_READ_TIMEOUT", "APP_WAYBACK_HTTP_READ_TIMEOUT_SECONDS"),
            base.wayback_http_read_timeout_seconds,
        ),
        wayback_http_max_retries=_int_env_any(
            ("APP_WAYBACK_TILE_MAX_RETRIES", "APP_WAYBACK_HTTP_MAX_RETRIES"),
            base.wayback_http_max_retries,
        ),
        wayback_http_backoff_base_seconds=_float_env_any(
            ("APP_WAYBACK_TILE_BACKOFF_BASE", "APP_WAYBACK_HTTP_BACKOFF_BASE_SECONDS"),
            base.wayback_http_backoff_base_seconds,
        ),
        wayback_tile_min_concurrency=_int_env(
            "APP_WAYBACK_TILE_MIN_CONCURRENCY",
            base.wayback_tile_min_concurrency,
        ),
        wayback_tile_max_concurrency=_int_env(
            "APP_WAYBACK_TILE_MAX_CONCURRENCY",
            base.wayback_tile_max_concurrency,
        ),
        wayback_tile_progress_every_tiles=_int_env(
            "APP_WAYBACK_TILE_PROGRESS_EVERY_TILES",
            base.wayback_tile_progress_every_tiles,
        ),
        wayback_tile_progress_every_seconds=_float_env(
            "APP_WAYBACK_TILE_PROGRESS_EVERY_SECONDS",
            base.wayback_tile_progress_every_seconds,
        ),
        wayback_tile_cache_backend=os.getenv("APP_WAYBACK_TILE_CACHE_BACKEND", base.wayback_tile_cache_backend),  # type: ignore[arg-type]
        wayback_tile_sqlite_wal=_bool_env(
            "APP_WAYBACK_TILE_SQLITE_WAL",
            base.wayback_tile_sqlite_wal,
        ),
        wayback_tile_sqlite_batch_insert_size=_int_env(
            "APP_WAYBACK_TILE_SQLITE_BATCH_INSERT_SIZE",
            base.wayback_tile_sqlite_batch_insert_size,
        ),
        wayback_max_missing_tile_ratio=_float_env(
            "APP_WAYBACK_MAX_MISSING_TILE_RATIO",
            base.wayback_max_missing_tile_ratio,
        ),
        wayback_tile_cache_dir=(
            Path(cache_dir_env)
            if (cache_dir_env := _optional_str_env("WAYBACK_TILE_CACHE_DIR", "APP_WAYBACK_TILE_CACHE_DIR"))
            else None
        ),
        wayback_tile_sqlite_cache_dir=(
            Path(cache_dir_env)
            if (cache_dir_env := _optional_str_env("APP_WAYBACK_TILE_SQLITE_CACHE_DIR"))
            else None
        ),
        wayback_tile_cache_service_enabled=_bool_env(
            "APP_WAYBACK_TILE_CACHE_SERVICE_ENABLED",
            base.wayback_tile_cache_service_enabled,
        ),
        wayback_tile_cache_service_url=os.getenv(
            "APP_WAYBACK_TILE_CACHE_SERVICE_URL",
            base.wayback_tile_cache_service_url,
        ),
        wayback_tile_cache_service_kind=os.getenv(
            "APP_WAYBACK_TILE_CACHE_SERVICE_KIND",
            base.wayback_tile_cache_service_kind,
        ),
        wayback_heavy_batch_tile_threshold=_int_env(
            "APP_WAYBACK_HEAVY_BATCH_TILE_THRESHOLD",
            base.wayback_heavy_batch_tile_threshold,
        ),
        wayback_tilemap_preflight_enabled=_bool_env(
            "APP_WAYBACK_TILEMAP_PREFLIGHT_ENABLED",
            base.wayback_tilemap_preflight_enabled,
        ),
        wayback_metadata_cache_enabled=_bool_env_any(
            ("WAYBACK_METADATA_CACHE_ENABLED", "APP_WAYBACK_METADATA_CACHE_ENABLED"),
            base.wayback_metadata_cache_enabled,
        ),
        wayback_metadata_cache_ttl_seconds=_int_env(
            "WAYBACK_METADATA_CACHE_TTL_SECONDS",
            base.wayback_metadata_cache_ttl_seconds,
        ),
        wayback_metadata_cache_dir=(
            Path(cache_dir_env)
            if (cache_dir_env := _optional_str_env("WAYBACK_METADATA_CACHE_DIR", "APP_WAYBACK_METADATA_CACHE_DIR"))
            else None
        ),
        wayback_tile_preflight_cache_enabled=_bool_env_any(
            ("WAYBACK_TILE_PREFLIGHT_CACHE_ENABLED", "APP_WAYBACK_TILE_PREFLIGHT_CACHE_ENABLED"),
            base.wayback_tile_preflight_cache_enabled,
        ),
        wayback_tile_preflight_cache_ttl_seconds=_int_env(
            "WAYBACK_TILE_PREFLIGHT_CACHE_TTL_SECONDS",
            base.wayback_tile_preflight_cache_ttl_seconds,
        ),
        wayback_tile_preflight_cache_dir=(
            Path(cache_dir_env)
            if (
                cache_dir_env := _optional_str_env(
                    "WAYBACK_TILE_PREFLIGHT_CACHE_DIR",
                    "APP_WAYBACK_TILE_PREFLIGHT_CACHE_DIR",
                )
            )
            else None
        ),
        mapbox_access_token=_optional_str_env("MAPBOX_ACCESS_TOKEN"),
        mapbox_satellite_tileset=os.getenv("MAPBOX_SATELLITE_TILESET", base.mapbox_satellite_tileset),
        mapbox_current_imagery_enabled=_bool_env(
            "MAPBOX_CURRENT_IMAGERY_ENABLED",
            base.mapbox_current_imagery_enabled,
        ),
        mapbox_current_imagery_cache_dir=(
            Path(cache_dir_env)
            if (cache_dir_env := _optional_str_env("MAPBOX_CURRENT_IMAGERY_CACHE_DIR"))
            else None
        ),
        mapbox_current_imagery_format=os.getenv(
            "MAPBOX_CURRENT_IMAGERY_FORMAT",
            base.mapbox_current_imagery_format,
        ),
        mapbox_current_imagery_max_zoom=_int_env(
            "MAPBOX_CURRENT_IMAGERY_MAX_ZOOM",
            base.mapbox_current_imagery_max_zoom,
        ),
        mapbox_current_imagery_default_zoom=_int_env(
            "MAPBOX_CURRENT_IMAGERY_DEFAULT_ZOOM",
            base.mapbox_current_imagery_default_zoom,
        ),
        mapbox_current_imagery_timeout_seconds=_int_env(
            "MAPBOX_CURRENT_IMAGERY_TIMEOUT_SECONDS",
            base.mapbox_current_imagery_timeout_seconds,
        ),
        mapbox_max_tiles_per_request=_int_env(
            "APP_MAPBOX_MAX_TILES_PER_REQUEST",
            _int_env("MAPBOX_CURRENT_IMAGERY_MAX_TILES", base.mapbox_current_imagery_max_tiles),
        ),
        mapbox_current_imagery_max_tiles=_int_env(
            "MAPBOX_CURRENT_IMAGERY_MAX_TILES",
            base.mapbox_current_imagery_max_tiles,
        ),
        reference_tile_cache_dir=(
            Path(cache_dir_env)
            if (cache_dir_env := _optional_str_env("APP_REFERENCE_TILE_CACHE_DIR"))
            else None
        ),
        reference_tile_prewarm_max_tiles=_int_env(
            "APP_REFERENCE_TILE_PREWARM_MAX_TILES",
            base.reference_tile_prewarm_max_tiles,
        ),
        reference_imagery_cache_enabled=_bool_env(
            "APP_REFERENCE_IMAGERY_CACHE_ENABLED",
            base.reference_imagery_cache_enabled,
        ),
        reference_imagery_cache_dir=(
            Path(cache_dir_env)
            if (cache_dir_env := _optional_str_env("APP_REFERENCE_IMAGERY_CACHE_DIR"))
            else None
        ),
        reference_imagery_materialization=os.getenv(
            "APP_REFERENCE_IMAGERY_MATERIALIZATION",
            base.reference_imagery_materialization,
        ),  # type: ignore[arg-type]
        patch_size=_int_env("APP_PATCH_SIZE", base.patch_size),
        stride=_int_env("APP_STRIDE", base.stride),
        inference_tiled_mode_auto=_bool_env(
            "APP_INFERENCE_TILED_MODE_AUTO",
            base.inference_tiled_mode_auto,
        ),
        inference_tile_size=_int_env(
            "APP_INFERENCE_TILE_SIZE",
            base.inference_tile_size,
        ),
        inference_tile_overlap=_int_env(
            "APP_INFERENCE_TILE_OVERLAP",
            base.inference_tile_overlap,
        ),
        inference_tile_batch_size=_int_env(
            "APP_INFERENCE_TILE_BATCH_SIZE",
            base.inference_tile_batch_size,
        ),
        inference_max_in_memory_pixels=_int_env(
            "APP_INFERENCE_MAX_IN_MEMORY_PIXELS",
            base.inference_max_in_memory_pixels,
        ),
        inference_heavy_batch_tile_threshold=_int_env(
            "APP_INFERENCE_HEAVY_BATCH_TILE_THRESHOLD",
            base.inference_heavy_batch_tile_threshold,
        ),
        inference_disable_full_preview_png_for_heavy_batch=_bool_env(
            "APP_INFERENCE_DISABLE_FULL_PREVIEW_PNG_FOR_HEAVY_BATCH",
            base.inference_disable_full_preview_png_for_heavy_batch,
        ),
        generate_full_mosaic_png_for_heavy_batch=_bool_env(
            "APP_GENERATE_FULL_MOSAIC_PNG_FOR_HEAVY_BATCH",
            base.generate_full_mosaic_png_for_heavy_batch,
        ),
        mosaic_preview_max_dimension=_int_env(
            "APP_MOSAIC_PREVIEW_MAX_DIMENSION",
            base.mosaic_preview_max_dimension,
        ),
        scene_segmentation_concurrency=_int_env(
            "APP_SCENE_SEGMENTATION_CONCURRENCY",
            base.scene_segmentation_concurrency,
        ),
        default_change_threshold=_float_env("APP_CHANGE_THRESHOLD", base.default_change_threshold),
        default_semantic_threshold=_float_env("APP_SEMANTIC_THRESHOLD", base.default_semantic_threshold),
        default_min_new_building_pixels=_int_env(
            "APP_MIN_NEW_BUILDING_PIXELS",
            base.default_min_new_building_pixels,
        ),
        addition_min_area_m2=_float_env("APP_ADDITION_MIN_AREA_M2", base.addition_min_area_m2),
        addition_max_existing_overlap_ratio=_float_env(
            "APP_ADDITION_MAX_EXISTING_OVERLAP_RATIO",
            base.addition_max_existing_overlap_ratio,
        ),
        addition_thin_artifact_max_area_m2=_float_env(
            "APP_ADDITION_THIN_ARTIFACT_MAX_AREA_M2",
            base.addition_thin_artifact_max_area_m2,
        ),
        addition_thinness_min_ratio=_float_env(
            "APP_ADDITION_THINNESS_MIN_RATIO",
            base.addition_thinness_min_ratio,
        ),
        addition_edge_buffer_m=_float_env("APP_ADDITION_EDGE_BUFFER_M", base.addition_edge_buffer_m),
        addition_max_edge_overlap_ratio=_float_env(
            "APP_ADDITION_MAX_EDGE_OVERLAP_RATIO",
            base.addition_max_edge_overlap_ratio,
        ),
        addition_thin_artifact_max_mean_probability=_float_env(
            "APP_ADDITION_THIN_ARTIFACT_MAX_MEAN_PROBABILITY",
            base.addition_thin_artifact_max_mean_probability,
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
        bandon_repo_dir=Path(os.getenv("APP_BANDON_REPO_DIR", str(base.bandon_repo_dir))),
        bandon_env_prefix=Path(os.getenv("APP_BANDON_ENV_PREFIX", str(base.bandon_env_prefix))),
        bandon_config_path=Path(os.getenv("APP_BANDON_CONFIG_PATH", str(base.bandon_config_path))),
        bandon_checkpoint_path=Path(os.getenv("APP_BANDON_CHECKPOINT_PATH", str(base.bandon_checkpoint_path))),
        bandon_device=os.getenv("APP_BANDON_DEVICE", base.bandon_device),  # type: ignore[arg-type]
        bandon_allow_mps_fallback=_bool_env(
            "APP_BANDON_ALLOW_MPS_FALLBACK",
            base.bandon_allow_mps_fallback,
        ),
        bandon_skip_invalid_crops=_bool_env(
            "APP_BANDON_SKIP_INVALID_CROPS",
            base.bandon_skip_invalid_crops,
        ),
        bandon_skip_outside_aoi_crops=_bool_env(
            "APP_BANDON_SKIP_OUTSIDE_AOI_CROPS",
            base.bandon_skip_outside_aoi_crops,
        ),
        bandon_skip_nodata_crops=_bool_env(
            "APP_BANDON_SKIP_NODATA_CROPS",
            base.bandon_skip_nodata_crops,
        ),
        bandon_min_valid_ratio_within_aoi=_float_env(
            "APP_BANDON_MIN_VALID_RATIO_WITHIN_AOI",
            base.bandon_min_valid_ratio_within_aoi,
        ),
        s2looking_checkpoint_path=(
            Path(s2looking_checkpoint_env)
            if (s2looking_checkpoint_env := _optional_str_env("APP_S2LOOKING_CHECKPOINT_PATH"))
            else None
        ),
        s2looking_change_threshold=_float_env(
            "APP_S2LOOKING_CHANGE_THRESHOLD",
            base.s2looking_change_threshold,
        ),
        database_url=os.getenv("DATABASE_URL", base.database_url),
        database_echo=_bool_env("DATABASE_ECHO", base.database_echo),
        db_inline_json_max_bytes=_int_env("DB_INLINE_JSON_MAX_BYTES", base.db_inline_json_max_bytes),
        persistence_backend=os.getenv("PERSISTENCE_BACKEND", base.persistence_backend),  # type: ignore[arg-type]
        redis_url=os.getenv("REDIS_URL", base.redis_url),
        celery_broker_url=_optional_str_env("CELERY_BROKER_URL"),
        celery_result_backend=_optional_str_env("CELERY_RESULT_BACKEND"),
        celery_task_default_queue=os.getenv("CELERY_TASK_DEFAULT_QUEUE", base.celery_task_default_queue),
        celery_worker_pool=os.getenv("CELERY_WORKER_POOL", base.celery_worker_pool),
        celery_worker_concurrency=_int_env("CELERY_WORKER_CONCURRENCY", base.celery_worker_concurrency),
        celery_task_acks_late=_bool_env("CELERY_TASK_ACKS_LATE", base.celery_task_acks_late),
        celery_task_reject_on_worker_lost=_bool_env(
            "CELERY_TASK_REJECT_ON_WORKER_LOST",
            base.celery_task_reject_on_worker_lost,
        ),
        celery_worker_prefetch_multiplier=_int_env(
            "CELERY_WORKER_PREFETCH_MULTIPLIER",
            base.celery_worker_prefetch_multiplier,
        ),
        jobs_enabled=_bool_env("JOBS_ENABLED", base.jobs_enabled),
        keep_intermediate_artifacts=_bool_env(
            "KEEP_INTERMEDIATE_ARTIFACTS",
            base.keep_intermediate_artifacts,
        ),
        materialize_source_imagery_in_requests=_bool_env(
            "MATERIALIZE_SOURCE_IMAGERY_IN_REQUESTS",
            base.materialize_source_imagery_in_requests,
        ),
        post_completion_request_cleanup_enabled=_bool_env(
            "APP_POST_COMPLETION_REQUEST_CLEANUP_ENABLED",
            base.post_completion_request_cleanup_enabled,
        ),
        post_completion_request_cleanup_mode=os.getenv(
            "APP_POST_COMPLETION_REQUEST_CLEANUP_MODE",
            base.post_completion_request_cleanup_mode,
        ),  # type: ignore[arg-type]
        post_completion_request_cleanup_grace_seconds=_int_env(
            "APP_POST_COMPLETION_REQUEST_CLEANUP_GRACE_SECONDS",
            base.post_completion_request_cleanup_grace_seconds,
        ),
        post_completion_request_cleanup_keep_provenance=_bool_env(
            "APP_POST_COMPLETION_REQUEST_CLEANUP_KEEP_PROVENANCE",
            base.post_completion_request_cleanup_keep_provenance,
        ),
        post_completion_request_cleanup_delete_export_bundle=_bool_env(
            "APP_POST_COMPLETION_REQUEST_CLEANUP_DELETE_EXPORT_BUNDLE",
            base.post_completion_request_cleanup_delete_export_bundle,
        ),
        enable_client_log_relay=_bool_env(
            "APP_ENABLE_CLIENT_LOG_RELAY",
            base.enable_client_log_relay,
        ),
        temporal_imagery_prefetch_enabled=_bool_env(
            "TEMPORAL_IMAGERY_PREFETCH_ENABLED",
            base.temporal_imagery_prefetch_enabled,
        ),
        temporal_imagery_prefetch_workers=_int_env(
            "TEMPORAL_IMAGERY_PREFETCH_WORKERS",
            base.temporal_imagery_prefetch_workers,
        ),
        temporal_imagery_prefetch_max_pairs=_int_env(
            "TEMPORAL_IMAGERY_PREFETCH_MAX_PAIRS",
            base.temporal_imagery_prefetch_max_pairs,
        ),
        temporal_imagery_prefetch_timeout_seconds=_int_env(
            "TEMPORAL_IMAGERY_PREFETCH_TIMEOUT_SECONDS",
            base.temporal_imagery_prefetch_timeout_seconds,
        ),
        temporal_imagery_prefetch_reduce_provider_workers=_bool_env(
            "TEMPORAL_IMAGERY_PREFETCH_REDUCE_PROVIDER_WORKERS",
            base.temporal_imagery_prefetch_reduce_provider_workers,
        ),
        reference_layer_max_upload_bytes=_int_env(
            "REFERENCE_LAYER_MAX_UPLOAD_BYTES",
            base.reference_layer_max_upload_bytes,
        ),
        reference_layer_browser_geojson_max_bytes=_int_env(
            "REFERENCE_LAYER_BROWSER_GEOJSON_MAX_BYTES",
            base.reference_layer_browser_geojson_max_bytes,
        ),
        reference_layer_browser_geojson_max_features=_int_env(
            "REFERENCE_LAYER_BROWSER_GEOJSON_MAX_FEATURES",
            base.reference_layer_browser_geojson_max_features,
        ),
        reference_layer_large_vector_input_threshold_bytes=_int_env(
            "REFERENCE_LAYER_LARGE_VECTOR_INPUT_THRESHOLD_BYTES",
            base.reference_layer_large_vector_input_threshold_bytes,
        ),
        reference_layer_pmtiles_enabled=_bool_env(
            "REFERENCE_LAYER_PMTILES_ENABLED",
            base.reference_layer_pmtiles_enabled,
        ),
        reference_layer_pmtiles_max_upload_mb=_int_env(
            "REFERENCE_LAYER_PMTILES_MAX_UPLOAD_MB",
            base.reference_layer_pmtiles_max_upload_mb,
        ),
        reference_layer_pmtiles_min_full_layer_mb=_int_env(
            "REFERENCE_LAYER_PMTILES_MIN_FULL_LAYER_MB",
            base.reference_layer_pmtiles_min_full_layer_mb,
        ),
        reference_layer_pmtiles_tippecanoe_bin=os.getenv(
            "REFERENCE_LAYER_PMTILES_TIPPECANOE_BIN",
            base.reference_layer_pmtiles_tippecanoe_bin,
        ),
        reference_layer_pmtiles_cli_bin=os.getenv(
            "REFERENCE_LAYER_PMTILES_CLI_BIN",
            base.reference_layer_pmtiles_cli_bin,
        ),
        reference_layer_pmtiles_max_zoom=_int_env(
            "REFERENCE_LAYER_PMTILES_MAX_ZOOM",
            base.reference_layer_pmtiles_max_zoom,
        ),
        reference_layer_pmtiles_min_zoom=_int_env(
            "REFERENCE_LAYER_PMTILES_MIN_ZOOM",
            base.reference_layer_pmtiles_min_zoom,
        ),
        reference_layer_pmtiles_default_layer_name=os.getenv(
            "REFERENCE_LAYER_PMTILES_DEFAULT_LAYER_NAME",
            base.reference_layer_pmtiles_default_layer_name,
        ),
        reference_layer_pmtiles_build_timeout_seconds=_int_env(
            "REFERENCE_LAYER_PMTILES_BUILD_TIMEOUT_SECONDS",
            base.reference_layer_pmtiles_build_timeout_seconds,
        ),
        reference_layer_pmtiles_keep_intermediate=_bool_env(
            "REFERENCE_LAYER_PMTILES_KEEP_INTERMEDIATE",
            base.reference_layer_pmtiles_keep_intermediate,
        ),
        cors_allowed_origins=_tuple_str_env("CORS_ALLOWED_ORIGINS", base.cors_allowed_origins),
        cors_allow_origin_regex=os.getenv("CORS_ALLOW_ORIGIN_REGEX", base.cors_allow_origin_regex),
        preview_limits=ModeLimits(
            name="fast_preview",
            label="Fast Preview",
            max_area_m2=_float_env("APP_FAST_PREVIEW_MAX_AREA_M2", base.preview_limits.max_area_m2),
            max_scene_tiles=_int_env(
                "APP_FAST_PREVIEW_MAX_SCENE_TILES",
                base.preview_limits.max_scene_tiles,
            ),
            max_inference_patches_per_scene=_int_env(
                "APP_FAST_PREVIEW_MAX_INFERENCE_PATCHES",
                base.preview_limits.max_inference_patches_per_scene,
            ),
        ),
        full_limits=ModeLimits(
            name="full_run",
            label="Full Run",
            max_area_m2=_float_env("APP_FULL_RUN_MAX_AREA_M2", base.full_limits.max_area_m2),
            max_scene_tiles=_int_env("APP_FULL_RUN_MAX_SCENE_TILES", base.full_limits.max_scene_tiles),
            max_inference_patches_per_scene=_int_env(
                "APP_FULL_RUN_MAX_INFERENCE_PATCHES",
                base.full_limits.max_inference_patches_per_scene,
            ),
        ),
    )
