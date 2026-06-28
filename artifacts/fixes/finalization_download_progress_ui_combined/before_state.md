# Before State

## Repository

- Branch: `codex/inference-persistent-runner-benchmarks`
- HEAD: `88966fd384c4e4388ea4d9ab86655f1b02857db0`
- Pre-existing unrelated worktree changes: deleted benchmark artifacts under `artifacts/benchmarks/`, untracked `artifacts/diagnostics/`, untracked `artifacts/ops/`.

## Failed Project Evidence

- Project: `temporal-test-mqy297ia-qoaslk`
- Job: `job-1c366434cf134d1ea9a5cf975c64c65e`
- Request hash: `e9d601f3c83390a851c90cf4`
- Project summary before fix: `milestone_count=3`, `complete_milestone_count=2`, `download_bundle_path=null`.
- Cached run response is successful and includes `result_semantics=building_change`, `total_change_polygons=73`, `total_change_area_m2=35905.16`.
- Prompt-provided stack trace: `TypeError: float() argument must be a string or a real number, not 'NoneType'`.

## Growth Envelope Call Sites

- `backend/src/services/temporal_projects.py:50` imports `build_temporal_growth_envelope`.
- `backend/src/services/temporal_projects.py:3585` `_milestone_has_derived_geometry_layers` requires `milestone.cumulative_growth_envelope_geojson is not None`.
- `backend/src/services/temporal_projects.py:3931` `_refresh_temporal_derived_geometry_layers` calls `build_temporal_growth_envelope`.
- `backend/src/services/temporal_projects.py:5335` file-backed milestone population seeds `cumulative_growth_envelope_geojson` from buffer layers.
- `backend/src/services/temporal_projects.py:5826` cached pair project construction seeds `cumulative_growth_envelope_geojson` from buffer layers.
- `backend/src/services/temporal_projects.py:6258` large-result recompute path seeds `cumulative_growth_envelope_geojson` from buffer layers.
- `backend/src/services/temporal_projects.py:6334` `_recompute_project_outputs_from_index` calls `build_temporal_growth_envelope`.

## Exact Optional Setting That Becomes `None`

- `backend/src/domain/vectorize.py` defines `_build_growth_envelope_geometry(... fill_holes_max_area_m2: float | None)`.
- `build_temporal_growth_envelope` passes `fill_holes_max_area_m2=None` by default.
- `_build_growth_envelope_geometry` calls `_fill_small_holes(candidate, max_area_m2=fill_holes_max_area_m2)`.
- `_fill_small_holes` then does `float(max_area_m2)`, so `float(None)` crashes.

## Export Download Baseline

- UI path: `frontend/src/features/temporal/TemporalMosaicPanel.tsx` `handleDownloadResults`.
- It POSTs to `/api/temporal-projects/{project_id}/exports/results` through `downloadFileFromRequest` with `{ format, perimeter }`.
- Download helper: `frontend/src/lib/download.ts`.
- Existing success path uses `response.blob()`, but does not extract `Content-Disposition`, does not distinguish network/CORS/proxy failures, and the error path always attempts `response.json()`.
- Backend route: `backend/src/api/routes/temporal_projects.py` returns `FileResponse` without explicit exposed headers.
- Global CORS middleware in `backend/src/api/main.py` does not expose `Content-Disposition`, `Content-Length`, or `Content-Type`.
- Prompt curl payload with `scopeType` is not accepted by current schema. Baseline POST wrote a 127-byte JSON error:
  `extra_forbidden` at `body.scopeType`.

## Progress UI Baseline

- Stage descriptions are defined in `frontend/src/lib/run-progress.ts` in `TEMPORAL_VERTICAL_TIMELINE_STAGES`.
- Descriptions are rendered for every stage in `frontend/src/features/results/TemporalVerticalProgressTimeline.tsx`.
- Non-inference stages currently show subtitles such as project preparation, tile verification, vectorization, publication, exports, metadata write, cleanup, and done.

## Error Contrast And Theme Baseline

- `--destructive-foreground` is a very light token in `frontend/src/styles/globals.css`.
- Multiple light error banners combine `bg-destructive/10` with `text-destructive-foreground`, including `TemporalMosaicPanel`, `ReferenceLayerImportModal`, `SettingsPanel`, and `WorkflowParametersPanel`.
- Theme initialization in `frontend/src/app/ThemeContext.tsx` defaults to system theme when no persisted `app-theme` value exists, and SSR fallback returns `dark`.
