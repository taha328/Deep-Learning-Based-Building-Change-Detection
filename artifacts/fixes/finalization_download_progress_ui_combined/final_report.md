# Final Report

## Root Causes

1. Temporal finalization still treated the removed growth-envelope product as required. Inference completed successfully, but finalization later called `build_temporal_growth_envelope`, which passed `fill_holes_max_area_m2=None` into `_fill_small_holes`; that helper then called `float(None)` and crashed.
2. Export endpoints returned valid files, but the browser path was brittle around large binary responses, filename headers, CORS-exposed headers, cache validation, and dev proxy timeout/config behavior.
3. The progress timeline rendered descriptions for every stage, error styles reused a light destructive background with light text, and first-load theme selection followed system dark mode instead of defaulting to light.

## Backend Strategy

- Removed growth-envelope generation from the required temporal project finalization and recompute paths.
- Kept project success based on required additions and buffer artifacts, not envelope/hull artifacts.
- Logged `TEMPORAL_GROWTH_ENVELOPE_DISABLED` with `reason=product_removed` where old paths would have produced the removed output.
- Set envelope fields to `None` and `growth_envelope_area_m2=0.0`.
- Hardened `_fill_small_holes` so `max_area_m2=None` means disabled, negative values disable optional hole filling, and non-numeric input raises a clear `ValueError`.
- Preserved large-result and file-backed export behavior from `88966fd`.

## Export Fixes

- Export route returns validated `FileResponse` objects, rejects `.partial`, missing, unreadable, or empty cached files, and sets `Content-Disposition`, `Content-Length`, `Content-Type`, `X-Content-Type-Options`, and exposed CORS headers.
- Export cache metadata now records output path, existence, size, mtime, and created time.
- Project-AOI exports can use a fast cache path before full project hydration when metadata fingerprints match.
- Added logs: `EXPORT_FAST_CACHE_CHECK_START`, `EXPORT_FAST_CACHE_HIT`, `EXPORT_FAST_CACHE_MISS`, `EXPORT_CACHE_MANIFEST_LOADED`, `EXPORT_CACHE_MANIFEST_UPDATED`, `EXPORT_DOWNLOAD_TOTAL_MS`, `EXPORT_FILE_RESPONSE_READY`, `EXPORT_FILE_RESPONSE_HEADERS`, `EXPORT_FILE_RESPONSE_SENT`, and `EXPORT_CACHE_FILE_INVALID`.
- Frontend download helper reads successful responses as blobs, extracts `Content-Disposition` filenames, preserves backend JSON/text errors for non-2xx responses, distinguishes network/proxy/CORS failures, and delays Blob URL revocation.
- Both `frontend/vite.config.ts` and the actually loaded `frontend/vite.config.js` now use the configured backend URL and longer proxy timeouts.

## UI Fixes

- Non-inference progress stages render only titles.
- The inference stage keeps the allowed useful detail/status copy, including `Détection des changements bâtimentaires.`, `Analyse globale en cours`, `Analyse de la période`, and `En cours`.
- Error panels use dark red readable text on light red backgrounds, with dark-mode equivalents.
- First load defaults to light mode when no explicit `app-theme` value exists; stored `light` and `dark` preferences are respected.

## Files Changed

- Backend: `backend/src/domain/vectorize.py`, `backend/src/services/temporal_projects.py`, `backend/src/services/temporal_exports.py`, `backend/src/api/main.py`, `backend/src/api/routes/temporal_projects.py`
- Backend tests: `backend/tests/test_temporal_projects.py`, `backend/tests/test_temporal_results_exports.py`
- Frontend: `frontend/src/lib/download.ts`, `frontend/src/lib/run-progress.ts`, `frontend/src/features/results/TemporalVerticalProgressTimeline.tsx`, `frontend/src/app/ThemeContext.tsx`, `frontend/src/app/theme.ts`, `frontend/vite.config.ts`, `frontend/vite.config.js`
- Frontend error contrast: `frontend/src/features/temporal/TemporalMosaicPanel.tsx`, `frontend/src/features/temporal/ReferenceLayerImportModal.tsx`, `frontend/src/features/settings/SettingsPanel.tsx`, `frontend/src/features/workspace/WorkflowParametersPanel.tsx`
- Frontend tests: `frontend/src/lib/download.test.ts`, `frontend/src/lib/run-progress.test.ts`, `frontend/src/app/theme.test.ts`, `frontend/src/app/error-contrast.test.ts`
- Reports: this directory's markdown files.

## Validation Summary

- Backend tests: `580 passed, 5 skipped, 4 warnings`.
- Frontend tests: `126 passed`.
- Frontend build: passed, with Vite's non-blocking large chunk warning.
- Python compile check for changed backend modules: passed.
- `git diff --check`: passed.
- Failed project `temporal-test-mqy297ia-qoaslk` recovered from existing inference artifacts without rerunning inference.
- Real medium project `temporal-medium-bouznika-fix-9318797a` completed end to end with two temporal pairs and `208` selected inference tiles per pair.
- Medium project browser UI whole-project shapefile download succeeded with filename `resultats_temporal-medium-bouznika-fix-9318797a_results_shapefile.zip`, size `972066839` bytes, and `unzip -t` passed.
- Repeated cached shapefile request returned headers in `9.28 ms`.
- Medium project GeoJSON export produced `7176` features and valid JSON.
- Custom-zone shapefile export passed `unzip -t`.
- Outside-zone export returned controlled `invalid_export_perimeter` JSON instead of `Failed to fetch`.

## Screenshot Evidence

Screenshots are local evidence and are not committed due size:

- `/Users/tahaelouali/Developer/Building_change_app/artifacts/fixes/finalization_download_progress_ui_combined/screenshots/01_light_default_initial.png`
- `/Users/tahaelouali/Developer/Building_change_app/artifacts/fixes/finalization_download_progress_ui_combined/screenshots/02_simplified_progress_timeline_component.png`
- `/Users/tahaelouali/Developer/Building_change_app/artifacts/fixes/finalization_download_progress_ui_combined/screenshots/03_recovered_project_results_available.png`
- `/Users/tahaelouali/Developer/Building_change_app/artifacts/fixes/finalization_download_progress_ui_combined/screenshots/06_imported_outside_zone_error_readable.png`
- `/Users/tahaelouali/Developer/Building_change_app/artifacts/fixes/finalization_download_progress_ui_combined/screenshots/07_medium_light_default.png`
- `/Users/tahaelouali/Developer/Building_change_app/artifacts/fixes/finalization_download_progress_ui_combined/screenshots/08_medium_project_results_available.png`
- `/Users/tahaelouali/Developer/Building_change_app/artifacts/fixes/finalization_download_progress_ui_combined/screenshots/09_medium_export_modal_project_shapefile.png`

## Rollback Plan

Revert this commit to restore prior finalization, export response, and frontend behavior. If rollback is needed only for frontend, the backend envelope removal can remain independently because the removed envelope/hull product is no longer a product requirement.

## Remaining Limitations

- Existing stale envelope/hull metadata may still appear in old project files, but it is ignored for success and not regenerated for new projects by default.
- Large shapefile bundles remain large when raster imagery is packaged; the fix makes cached response startup fast but does not shrink historical export contents.
