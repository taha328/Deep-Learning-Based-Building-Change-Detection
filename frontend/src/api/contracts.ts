import { z } from "zod";

export const modeSchema = z.enum(["fast_preview", "full_run"]);
export const modelBackendSchema = z.enum(["sam3", "bandon_mps"]);
export const sam3BackendModeSchema = z.enum(["public_zerogpu", "local", "huggingface_gpu"]);
export const latestImagerySourceSchema = z.enum(["esri_wayback", "mapbox_current"]);

export const releaseSchema = z.object({
  identifier: z.string(),
  release_date: z.string(),
  label: z.string(),
  release_num: z.number().nullable().optional(),
});

export const releasesResponseSchema = z.object({
  releases: z.array(releaseSchema),
});

export const backendAvailabilitySchema = z.object({
  mode: z.enum(["public_zerogpu", "local", "huggingface_gpu", "bandon_mps"]),
  label: z.string(),
  available: z.boolean(),
  enabled_by_default: z.boolean(),
  reason: z.string().nullable().optional(),
  diagnostics: z.record(z.string()),
});

export const pipelineExecutionConfigSchema = z
  .object({
    model_backend: modelBackendSchema,
    backend_mode: z.enum(["public_zerogpu", "local", "huggingface_gpu"]).optional(),
  })
  .passthrough();

export const validationRequestSchema = z.object({
  aoi_geojson: z.record(z.any()),
  t1_release: z.string(),
  t2_release: z.string(),
  mode: modeSchema,
  model_backend: modelBackendSchema.optional(),
  sam3_backend_mode: sam3BackendModeSchema.optional(),
  change_threshold: z.number().optional(),
  semantic_threshold: z.number().optional(),
  min_new_building_pixels: z.number().int().optional(),
  min_new_building_area_m2: z.number().optional(),
  old_building_mask_dilation_pixels: z.number().int().optional(),
  new_building_core_distance_pixels: z.number().int().optional(),
  merge_close_gap_m: z.number().optional(),
  building_block_gap_m: z.number().optional(),
  buffer_distances_m: z.array(z.number()).optional(),
  keep_disjoint_buffer_parts_separate: z.boolean().optional(),
  road_constraint_layer_path: z.string().nullable().optional(),
  latest_source: latestImagerySourceSchema.optional(),
});

export const validationResponseSchema = z.object({
  valid: z.boolean(),
  normalized_aoi: z.record(z.any()).nullable(),
  estimated_tile_count_t1: z.number(),
  estimated_tile_count_t2: z.number(),
  estimated_total_tiles: z.number(),
  estimated_area_m2: z.number(),
  warnings: z.array(z.string()),
  blocking_errors: z.array(z.string()),
  recommended_mode: modeSchema,
});

export const previewImagesSchema = z.object({
  t1_preview_path: z.string().nullable().optional(),
  t2_preview_path: z.string().nullable().optional(),
  change_probability_preview_path: z.string().nullable().optional(),
  change_overlay_preview_path: z.string().nullable().optional(),
  t1_preview_png_data_url: z.string().nullable().optional(),
  t2_preview_png_data_url: z.string().nullable().optional(),
  change_probability_preview_png_data_url: z.string().nullable().optional(),
  change_overlay_preview_png_data_url: z.string().nullable().optional(),
  raster_bounds_wgs84: z.array(z.number()).nullable().optional(),
  raster_bounds_native: z.array(z.number()).nullable().optional(),
  raster_crs: z.string().nullable().optional(),
  raster_transform: z.array(z.number()).nullable().optional(),
  raster_size: z.array(z.number()).nullable().optional(),
});

export const artifactSchema = z.object({
  name: z.string(),
  path: z.string(),
  media_type: z.string(),
  description: z.string(),
});

export const summarySchema = z.object({
  request_hash: z.string(),
  mode: modeSchema,
  model_backend: modelBackendSchema.nullable().optional(),
  result_semantics: z.enum(["new_buildings", "building_change"]).nullable().optional(),
  estimated_area_m2: z.number(),
  tile_count_t1: z.number(),
  tile_count_t2: z.number(),
  total_new_buildings: z.number(),
  total_building_blocks: z.number(),
  total_new_building_area_m2: z.number(),
  total_building_block_area_m2: z.number(),
  total_change_polygons: z.number().optional(),
  total_change_area_m2: z.number().optional(),
  release_date_t1: z.string().nullable().optional(),
  release_date_t2: z.string().nullable().optional(),
  dominant_src_date_t1: z.string().nullable().optional(),
  dominant_src_date_t2: z.string().nullable().optional(),
  dominant_src_res_m_t1: z.number().nullable().optional(),
  dominant_src_res_m_t2: z.number().nullable().optional(),
});

export const diagnosticsSchema = z.object({
  cache_hit: z.boolean(),
  stage_seconds: z.record(z.number()),
  tile_counts: z.record(z.number()),
  patch_count: z.number(),
  thresholds: z.record(z.number()),
  min_new_building_pixels: z.number(),
  alignment: z.record(z.any()).optional(),
  backend: z.record(z.any()).optional(),
  warnings: z.array(z.string()).optional(),
  coverage: z.record(z.any()).optional(),
});

export const tabularMetricsSchema = z.object({
  summary_rows: z.array(z.record(z.any())),
  change_rows: z.array(z.record(z.any())).optional(),
  new_building_rows: z.array(z.record(z.any())),
  building_block_rows: z.array(z.record(z.any())),
  buffer_rows: z.record(z.array(z.record(z.any()))),
});

export const runResponseSchema = z.object({
  success: z.boolean(),
  error_code: z.string().nullable().optional(),
  error_message: z.string().nullable().optional(),
  summary: summarySchema.nullable().optional(),
  preview_images: previewImagesSchema.nullable().optional(),
  change_polygons_geojson: z.record(z.any()).nullable().optional(),
  new_buildings_geojson: z.record(z.any()).nullable().optional(),
  building_blocks_geojson: z.record(z.any()).nullable().optional(),
  buffer_layers_geojson: z.record(z.record(z.any())),
  tabular_metrics: tabularMetricsSchema.nullable().optional(),
  artifacts: z.array(artifactSchema),
  downloadable_zip_path: z.string().nullable().optional(),
  diagnostics: diagnosticsSchema.nullable().optional(),
});

export const jobStatusSchema = z.preprocess(
  (value) => (value === "complete" ? "completed" : value),
  z.enum(["queued", "running", "completed", "failed", "cancel_requested", "cancelled"]),
);

export const jobStartResponseSchema = z.object({
  job_id: z.string(),
  celery_task_id: z.string().nullable().optional(),
  job_kind: z.string(),
  status: jobStatusSchema,
});

export const jobResponseSchema = z.object({
  job_id: z.string(),
  celery_task_id: z.string().nullable().optional(),
  job_kind: z.string(),
  status: jobStatusSchema,
  project_id: z.string().nullable().optional(),
  request_hash: z.string().nullable().optional(),
  progress: z.number().int().nullable().optional(),
  stage: z.string().nullable().optional(),
  message: z.string().nullable().optional(),
  error_code: z.string().nullable().optional(),
  error_message: z.string().nullable().optional(),
  result_run_id: z.string().nullable().optional(),
  raw_request: z.record(z.any()).nullable().optional(),
  raw_result: z.record(z.any()).nullable().optional(),
  cancel_requested: z.boolean().default(false),
  created_at: z.string(),
  updated_at: z.string(),
  started_at: z.string().nullable().optional(),
  completed_at: z.string().nullable().optional(),
});

export const temporalSourceModeSchema = z.enum(["automated", "manual_override", "hybrid_reviewed"]);
export const temporalMilestoneStatusSchema = z.enum(["pending", "validated", "complete", "error"]);
export const temporalSemanticsSchema = z.enum(["expansion_only"]);

export const temporalArtifactSchema = artifactSchema.extend({});

export const temporalMilestoneMetricsSchema = z.object({
  added_area_m2: z.number(),
  total_area_m2: z.number(),
  additions_feature_count: z.number().int(),
  effective_feature_count: z.number().int(),
  building_level_available: z.boolean(),
  added_block_count: z.number().int().default(0),
  cumulative_block_count: z.number().int().default(0),
  added_block_area_m2: z.number().default(0),
  cumulative_block_area_m2: z.number().default(0),
  growth_envelope_area_m2: z.number().default(0),
});

export const temporalReferenceImagerySchema = z.object({
  image_path: z.string().nullable().optional(),
  image_png_data_url: z.string().nullable().optional(),
  raster_bounds_wgs84: z.array(z.number()).nullable().optional(),
});

export const temporalMilestoneSchema = z.object({
  release_identifier: z.string(),
  release_date: z.string().nullable().optional(),
  status: temporalMilestoneStatusSchema.default("pending"),
  source_mode: temporalSourceModeSchema.default("automated"),
  warnings: z.array(z.string()).default([]),
  error_message: z.string().nullable().optional(),
  pair_request_hash: z.string().nullable().optional(),
  automated_additions_geojson: z.record(z.any()).nullable().optional(),
  automated_candidate_footprint_geojson: z.record(z.any()).nullable().optional(),
  automated_building_blocks_geojson: z.record(z.any()).nullable().optional(),
  manual_override_geojson: z.record(z.any()).nullable().optional(),
  additions_geojson: z.record(z.any()).nullable().optional(),
  effective_building_blocks_geojson: z.record(z.any()).nullable().optional(),
  effective_footprint_geojson: z.record(z.any()).nullable().optional(),
  buffer_layers_geojson: z.record(z.record(z.any())).default({}),
  cumulative_union_geojson: z.record(z.any()).nullable().optional(),
  cumulative_convex_hull_geojson: z.record(z.any()).nullable().optional(),
  cumulative_growth_blocks_geojson: z.record(z.any()).nullable().optional(),
  cumulative_growth_envelope_geojson: z.record(z.any()).nullable().optional(),
  reference_imagery: temporalReferenceImagerySchema.nullable().optional(),
  metrics: temporalMilestoneMetricsSchema.nullable().optional(),
  artifacts: z.array(temporalArtifactSchema).default([]),
});

export const temporalProjectSchema = z.object({
  project_id: z.string(),
  name: z.string(),
  project_dir: z.string().nullable().optional(),
  semantics: temporalSemanticsSchema.default("expansion_only"),
  aoi_geojson: z.record(z.any()).nullable().optional(),
  milestones: z.array(temporalMilestoneSchema).default([]),
  created_at: z.string(),
  updated_at: z.string(),
  execution_config: pipelineExecutionConfigSchema.nullable().optional(),
  warnings: z.array(z.string()).default([]),
  validation_blocking_errors: z.array(z.string()).default([]),
  download_bundle_path: z.string().nullable().optional(),
  latest_source: latestImagerySourceSchema.default("esri_wayback"),
});

export const temporalProjectSummarySchema = z.object({
  project_id: z.string(),
  name: z.string(),
  project_dir: z.string().nullable().optional(),
  project_kind: z.enum(["pairwise", "temporal"]).optional(),
  display_name: z.string().optional(),
  semantics: temporalSemanticsSchema.default("expansion_only"),
  milestone_count: z.number().int(),
  complete_milestone_count: z.number().int(),
  created_at: z.string(),
  updated_at: z.string(),
  download_bundle_path: z.string().nullable().optional(),
});

export const temporalProjectSaveResponseSchema = z.object({
  project_id: z.string(),
  updated_at: z.string(),
  download_bundle_path: z.string().nullable().optional(),
});

export const temporalPairEstimateSchema = z.object({
  from_release_identifier: z.string(),
  to_release_identifier: z.string(),
  estimated_tile_count_t1: z.number(),
  estimated_tile_count_t2: z.number(),
  estimated_total_tiles: z.number(),
  warnings: z.array(z.string()).default([]),
  blocking_errors: z.array(z.string()).default([]),
});

export const temporalProjectValidationResponseSchema = z.object({
  valid: z.boolean(),
  project: temporalProjectSchema,
  warnings: z.array(z.string()).default([]),
  blocking_errors: z.array(z.string()).default([]),
  estimated_total_tiles: z.number().default(0),
  pair_estimates: z.array(temporalPairEstimateSchema).default([]),
});

export const temporalProjectRunResponseSchema = z.object({
  success: z.boolean(),
  error_message: z.string().nullable().optional(),
  project: temporalProjectSchema,
});

export type ModeName = z.infer<typeof modeSchema>;
export type ModelBackendName = z.infer<typeof modelBackendSchema>;
export type Sam3BackendMode = z.infer<typeof sam3BackendModeSchema>;
export type LatestImagerySource = z.infer<typeof latestImagerySourceSchema>;
export type ReleaseMetadata = z.infer<typeof releaseSchema>;
export type BackendAvailability = z.infer<typeof backendAvailabilitySchema>;
export type PipelineExecutionConfig = z.infer<typeof pipelineExecutionConfigSchema>;
export type ValidationRequest = z.infer<typeof validationRequestSchema>;
export type ValidationResponse = z.infer<typeof validationResponseSchema>;
export type RunResponse = z.infer<typeof runResponseSchema>;
export type JobStatus = z.infer<typeof jobStatusSchema>;
export type JobStartResponse = z.infer<typeof jobStartResponseSchema>;
export type JobResponse = z.infer<typeof jobResponseSchema>;
export type TemporalSourceMode = z.infer<typeof temporalSourceModeSchema>;
export type TemporalMilestoneStatus = z.infer<typeof temporalMilestoneStatusSchema>;
export type TemporalSemantics = z.infer<typeof temporalSemanticsSchema>;
export type TemporalArtifact = z.infer<typeof temporalArtifactSchema>;
export type TemporalMilestoneMetrics = z.infer<typeof temporalMilestoneMetricsSchema>;
export type TemporalReferenceImagery = z.infer<typeof temporalReferenceImagerySchema>;
export type TemporalMilestone = z.infer<typeof temporalMilestoneSchema>;
export type TemporalProject = z.infer<typeof temporalProjectSchema>;
export type TemporalProjectSummary = z.infer<typeof temporalProjectSummarySchema>;
export type TemporalProjectSaveResponse = z.infer<typeof temporalProjectSaveResponseSchema>;
export type TemporalPairEstimate = z.infer<typeof temporalPairEstimateSchema>;
export type TemporalProjectValidationResponse = z.infer<typeof temporalProjectValidationResponseSchema>;
export type TemporalProjectRunResponse = z.infer<typeof temporalProjectRunResponseSchema>;
