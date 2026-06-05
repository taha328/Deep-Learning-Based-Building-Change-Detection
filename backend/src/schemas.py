from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from src.execution_profiles import PipelineExecutionConfig


ModeName = Literal["fast_preview", "full_run"]
InferenceBackendName = Literal["bandon_mps", "mtgcdnet_s2looking_mps"]
LatestImagerySource = Literal["esri_wayback", "mapbox_current"]
ReferenceLayerKind = Literal["vector", "raster"]
ReferenceGeometryType = Literal["point", "line", "polygon", "mixed", "raster"]
ReferenceLayerScope = Literal["aoi_clipped", "full_layer"]
ReferenceLayerStorageStrategy = Literal["geojson", "gpkg", "postgis", "pmtiles", "mbtiles", "cog", "raster_tiles"]


class GeoJSONGeometry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    coordinates: Any


class GeoJSONFeature(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["Feature"] = "Feature"
    geometry: GeoJSONGeometry
    properties: dict[str, Any] = Field(default_factory=dict)


class GeoJSONFeatureCollection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: list[GeoJSONFeature] = Field(default_factory=list)


class ReleaseMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identifier: str
    release_date: str
    label: str
    release_num: int | None = None


class ReleaseListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    releases: list[ReleaseMetadata]
    source_status: Literal["live", "stale", "stale_memory"] = "live"
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    fetched_at: str | None = None


class ReferenceLayerStyle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    color: str = "#0ea5e9"
    line_width: float = 2.0
    fill_color: str = "#0ea5e9"
    fill_opacity: float = 0.25
    outline_color: str = "#0369a1"
    point_radius: float = 5.0


class ReferenceLayer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layer_id: str
    project_id: str
    name: str
    original_filename: str
    original_format: str
    layer_kind: ReferenceLayerKind
    geometry_type: ReferenceGeometryType
    scope: ReferenceLayerScope
    storage_strategy: ReferenceLayerStorageStrategy
    crs: str | None = None
    bounds_wgs84: list[float] | None = None
    feature_count: int | None = None
    file_size_bytes: int
    source_path: str | None = None
    display_path: str | None = None
    display_url: str | None = None
    pmtiles_url: str | None = None
    tilejson_url: str | None = None
    tiles_url_template: str | None = None
    source_layer: str | None = None
    style: ReferenceLayerStyle = Field(default_factory=ReferenceLayerStyle)
    visible: bool = True
    opacity: float = 0.85
    created_at: str
    updated_at: str
    warnings: list[str] = Field(default_factory=list)


class ReferenceLayerPreflightResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    original_filename: str
    original_format: str
    layer_kind: ReferenceLayerKind
    geometry_type: ReferenceGeometryType
    scope: ReferenceLayerScope
    storage_strategy: ReferenceLayerStorageStrategy
    crs: str | None = None
    bounds_wgs84: list[float] | None = None
    feature_count: int | None = None
    file_size_bytes: int
    tool_status: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class ReferenceLayerPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    visible: bool | None = None
    opacity: float | None = None
    style: ReferenceLayerStyle | None = None


class ValidationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    aoi_geojson: dict[str, Any]
    t1_release: str
    t2_release: str
    mode: ModeName
    inference_backend: InferenceBackendName | None = None
    change_threshold: float | None = None
    semantic_threshold: float | None = None
    min_new_building_pixels: int | None = None
    min_new_building_area_m2: float | None = None
    old_building_mask_dilation_pixels: int | None = None
    new_building_core_distance_pixels: int | None = None
    merge_close_gap_m: float | None = None
    building_block_gap_m: float | None = None
    buffer_distances_m: list[float] | None = None
    keep_disjoint_buffer_parts_separate: bool = True
    road_constraint_layer_path: str | None = None
    latest_source: LatestImagerySource = "esri_wayback"
    existing_footprint_geojson: dict[str, Any] | None = None

    @field_validator("buffer_distances_m")
    @classmethod
    def validate_buffer_distances(cls, value: list[float] | None) -> list[float] | None:
        if value is None:
            return value
        if len(value) > 8:
            raise ValueError("At most 8 buffer distances are supported.")
        return value


class ValidationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    valid: bool
    normalized_aoi: dict[str, Any] | None
    estimated_tile_count_t1: int
    estimated_tile_count_t2: int
    estimated_total_tiles: int
    estimated_area_m2: float
    warnings: list[str] = Field(default_factory=list)
    blocking_errors: list[str] = Field(default_factory=list)
    recommended_mode: ModeName
    details: dict[str, Any] = Field(default_factory=dict)


class RunRequest(ValidationRequest):
    model_config = ConfigDict(extra="forbid")

    change_threshold: float = 0.60
    semantic_threshold: float = 0.50
    old_building_mask_dilation_pixels: int = 2
    new_building_core_distance_pixels: int = 2
    merge_close_gap_m: float = 10.0
    building_block_gap_m: float = 25.0
    buffer_distances_m: list[float] = Field(default_factory=lambda: [10.0, 15.0, 20.0])
    keep_disjoint_buffer_parts_separate: bool = True


class PreviewImages(BaseModel):
    model_config = ConfigDict(extra="forbid")

    t1_preview_path: str | None = None
    t2_preview_path: str | None = None
    change_probability_preview_path: str | None = None
    change_overlay_preview_path: str | None = None
    t1_preview_png_data_url: str | None = None
    t2_preview_png_data_url: str | None = None
    change_probability_preview_png_data_url: str | None = None
    change_overlay_preview_png_data_url: str | None = None
    raster_bounds_wgs84: list[float] | None = None
    raster_bounds_native: list[float] | None = None
    raster_crs: str | None = None
    raster_transform: list[float] | None = None
    raster_size: list[int] | None = None


class ArtifactEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    path: str
    media_type: str
    description: str
    key: str | None = None
    feature_count: int | None = None
    size_bytes: int | None = None
    source_mtime_ns: int | None = None
    qgis_cache_key: str | None = None
    bbox: list[float] | None = None
    sha256: str | None = None
    artifact_url: str | None = None
    geojson_url: str | None = None
    download_url: str | None = None
    gpkg_url: str | None = None
    qgis_preferred_url: str | None = None
    qgis_preferred_format: str | None = None
    qgis_compatible: bool = False
    tilejson_url: str | None = None
    tiles_url_template: str | None = None
    vector_source_layer: str | None = None


class SummaryStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_hash: str
    mode: ModeName
    model_backend: str | None = None
    result_semantics: Literal["new_buildings", "building_change"] | None = None
    estimated_area_m2: float
    tile_count_t1: int
    tile_count_t2: int
    total_new_buildings: int
    total_building_blocks: int
    total_new_building_area_m2: float
    total_building_block_area_m2: float
    total_change_polygons: int = 0
    total_change_area_m2: float = 0.0
    release_date_t1: str | None = None
    release_date_t2: str | None = None
    dominant_src_date_t1: str | None = None
    dominant_src_date_t2: str | None = None
    dominant_src_res_m_t1: float | None = None
    dominant_src_res_m_t2: float | None = None
    release_date: str | None = None
    dominant_src_date: str | None = None
    dominant_src_res_m: float | None = None


class DiagnosticMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cache_hit: bool
    stage_seconds: dict[str, float] = Field(default_factory=dict)
    tile_counts: dict[str, int] = Field(default_factory=dict)
    patch_count: int = 0
    thresholds: dict[str, float] = Field(default_factory=dict)
    min_new_building_pixels: int = 0
    alignment: dict[str, Any] = Field(default_factory=dict)
    backend: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    coverage: dict[str, Any] = Field(default_factory=dict)


class TabularMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary_rows: list[dict[str, Any]] = Field(default_factory=list)
    change_rows: list[dict[str, Any]] = Field(default_factory=list)
    new_building_rows: list[dict[str, Any]] = Field(default_factory=list)
    building_block_rows: list[dict[str, Any]] = Field(default_factory=list)
    buffer_rows: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)


class RunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: bool = True
    error_code: str | None = None
    error_message: str | None = None
    summary: SummaryStats | None = None
    preview_images: PreviewImages | None = None
    change_polygons_geojson: dict[str, Any] | None = None
    new_buildings_geojson: dict[str, Any] | None = None
    segmentation_geojson: dict[str, Any] | None = None
    building_blocks_geojson: dict[str, Any] | None = None
    buffer_layers_geojson: dict[str, dict[str, Any]] = Field(default_factory=dict)
    tabular_metrics: TabularMetrics | None = None
    artifacts: list[ArtifactEntry] = Field(default_factory=list)
    downloadable_zip_path: str | None = None
    diagnostics: DiagnosticMetadata | None = None


TemporalSourceMode = Literal["automated", "manual_override", "hybrid_reviewed"]
TemporalMilestoneStatus = Literal["pending", "validated", "complete", "error"]
TemporalSemantics = Literal["expansion_only"]


class TemporalArtifactEntry(ArtifactEntry):
    model_config = ConfigDict(extra="forbid")


class TemporalMilestoneMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    added_area_m2: float = 0.0
    total_area_m2: float = 0.0
    additions_feature_count: int = 0
    effective_feature_count: int = 0
    building_level_available: bool = False
    added_block_count: int = 0
    cumulative_block_count: int = 0
    added_block_area_m2: float = 0.0
    cumulative_block_area_m2: float = 0.0
    growth_envelope_area_m2: float = 0.0


class TemporalReferenceImagery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image_path: str | None = None
    image_png_data_url: str | None = None
    raster_bounds_wgs84: list[float] | None = None
    storage_strategy: Literal["image_overlay", "cog", "raster_tiles"] | None = None
    cog_path: str | None = None
    cog_url: str | None = None
    tilejson_url: str | None = None
    tiles_url_template: str | None = None
    minzoom: int | None = None
    maxzoom: int | None = None
    tile_size: int | None = None
    reference_imagery_key: str | None = None
    canonical_cog_path: str | None = None
    materialization_method: str | None = None


class TemporalMilestone(BaseModel):
    model_config = ConfigDict(extra="forbid")

    release_identifier: str
    release_date: str | None = None
    status: TemporalMilestoneStatus = "pending"
    source_mode: TemporalSourceMode = "automated"
    warnings: list[str] = Field(default_factory=list)
    error_message: str | None = None
    pair_request_hash: str | None = None
    populated_request_hash: str | None = None
    request_workspace_path: str | None = None
    automated_additions_geojson: dict[str, Any] | None = None
    automated_candidate_footprint_geojson: dict[str, Any] | None = None
    automated_building_blocks_geojson: dict[str, Any] | None = None
    manual_override_geojson: dict[str, Any] | None = None
    additions_geojson: dict[str, Any] | None = None
    effective_building_blocks_geojson: dict[str, Any] | None = None
    effective_footprint_geojson: dict[str, Any] | None = None
    buffer_layers_geojson: dict[str, dict[str, Any]] = Field(default_factory=dict)
    cumulative_union_geojson: dict[str, Any] | None = None
    cumulative_convex_hull_geojson: dict[str, Any] | None = None
    cumulative_growth_blocks_geojson: dict[str, Any] | None = None
    cumulative_growth_envelope_geojson: dict[str, Any] | None = None
    reference_imagery: TemporalReferenceImagery | None = None
    metrics: TemporalMilestoneMetrics | None = None
    artifacts: list[TemporalArtifactEntry] = Field(default_factory=list)


class TemporalProject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    name: str
    project_dir: str | None = None
    semantics: TemporalSemantics = "expansion_only"
    aoi_geojson: dict[str, Any] | None = None
    milestones: list[TemporalMilestone] = Field(default_factory=list)
    created_at: str
    updated_at: str
    execution_config: PipelineExecutionConfig | None = None
    warnings: list[str] = Field(default_factory=list)
    validation_blocking_errors: list[str] = Field(default_factory=list)
    download_bundle_path: str | None = None
    latest_source: LatestImagerySource = "esri_wayback"


class TemporalProjectResponse(TemporalProject):
    model_config = ConfigDict(extra="forbid")

    has_reference_layers: bool = False
    reference_layer_count: int = 0


class TemporalProjectSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    name: str
    project_dir: str | None = None
    project_kind: Literal["pairwise", "temporal"]
    display_name: str
    semantics: TemporalSemantics = "expansion_only"
    milestone_count: int
    complete_milestone_count: int
    created_at: str
    updated_at: str
    download_bundle_path: str | None = None


class TemporalPairEstimate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_release_identifier: str
    to_release_identifier: str
    estimated_tile_count_t1: int
    estimated_tile_count_t2: int
    estimated_total_tiles: int
    warnings: list[str] = Field(default_factory=list)
    blocking_errors: list[str] = Field(default_factory=list)


class TemporalProjectValidationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    valid: bool
    project: TemporalProject
    warnings: list[str] = Field(default_factory=list)
    blocking_errors: list[str] = Field(default_factory=list)
    estimated_total_tiles: int = 0
    pair_estimates: list[TemporalPairEstimate] = Field(default_factory=list)


class TemporalProjectRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: bool = True
    error_message: str | None = None
    project: TemporalProject


class TemporalProjectReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str


class TemporalProjectSaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: TemporalProject

    @field_validator("project", mode="before")
    @classmethod
    def strip_derived_project_fields(cls, value: Any) -> Any:
        if isinstance(value, dict):
            sanitized = dict(value)
            sanitized.pop("has_reference_layers", None)
            sanitized.pop("reference_layer_count", None)
            return sanitized
        return value


class TemporalProjectSaveResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    updated_at: str
    download_bundle_path: str | None = None


class TemporalOverrideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    release_identifier: str
    override_geojson: dict[str, Any]
