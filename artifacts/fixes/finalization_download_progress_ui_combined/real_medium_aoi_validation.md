# Real Medium AOI Validation

## Project

- Project id: `temporal-medium-bouznika-fix-9318797a`
- Name in UI: `Temporal mosaic · medium bouznika fix validation`
- AOI bounds: `[-7.761840820312501, 33.51735430695927, -7.695922851562498, 33.55970664841196]`
- Milestones: `WB_2024_R03`, `WB_2025_R04`, `WB_2026_R05`
- Temporal pairs: `WB_2024_R03 -> WB_2025_R04`, `WB_2025_R04 -> WB_2026_R05`
- Change threshold: `0.3`
- Cleanup: disabled for validation evidence preservation.

## Run Evidence

The initial imagery estimate was high, but actual selected inference tiles landed in the required medium range:

- Pair 1 request hash: `3c344e0cdba6b85a7386c058`
- Pair 1 pair-request hash: `d950d1075e1f5251f287d95c`
- Pair 1 tile count: `totalTiles=208`, `selectedTiles=208`, `processedTiles=208`
- Pair 1 inference output: `features=1104`, duration about `758.65 s`
- Pair 2 request hash: `9b21d536a8ce1b2551b27c9f`
- Pair 2 pair-request hash: `676635f9e05842abee62549d`
- Pair 2 tile count: `totalTiles=208`, `selectedTiles=208`, `processedTiles=208`
- Pair 2 inference output: `features=1258`, duration about `746.60 s`
- End-to-end run duration: about `2173.68 s`

One interrupted validation attempt created an empty stale Wayback mosaic lock directory. I removed only that empty lock:

```text
backend/runtime_cache/wayback_mosaics/072239a003fbd59c7dbdc799ad328a30.lock
```

No valid project output was deleted.

## Finalization Evidence

- Pair 1 finalization produced `670` additions and `670` features in each `10m`, `15m`, and `20m` buffer layer.
- Pair 2 finalization produced `1124` additions and `1124` features in each `10m`, `15m`, and `20m` buffer layer.
- Logs included `TEMPORAL_GROWTH_ENVELOPE_DISABLED ... reason=product_removed` for the relevant releases.
- No `TypeError float(None)`, hull error, or envelope-required error appeared.
- Project summary after completion: `complete_milestone_count=3`.
- All milestones have `growth_envelope_present=false`, `growth_envelope_area_m2=0.0`, and no `cumulative_growth_envelope_geojson`.

## API Evidence

- Compact project endpoint returned `200 OK`, `loading_mode=compact`, `milestone_count=3`, and no `Résultats non disponibles` state.
- Project detail endpoint returned the expected project id, name, milestones, AOI, and reference-layer metadata.
- Medium project-AOI GeoJSON export:
  - Status: `200 OK`
  - Content length: `44137688`
  - Content type: `application/geo+json`
  - Filename: `resultats_temporal-medium-bouznika-fix-9318797a.geojson`
  - JSON validation: valid output with `7176` features
- Medium custom-zone shapefile export:
  - Status: `200 OK`
  - Content length: `584509`
  - Content type: `application/zip`
  - ZIP validation: `unzip -t` passed
- Medium outside-zone export:
  - Status: `400`
  - Code: `invalid_export_perimeter`
  - Message: `La zone sélectionnée est hors de l’AOI du projet.`

## Browser And UI Evidence

- Vite validation frontend: `http://127.0.0.1:5174`
- Validation backend: `http://127.0.0.1:8010`
- Vite proxy fix confirmed: after patching `frontend/vite.config.js`, `/api/temporal-projects` from `5174` returned the validation backend project list and included `temporal-medium-bouznika-fix-9318797a`.
- Clean profile theme check:
  - `htmlClass=""`
  - `localStorage["app-theme"]="light"`
  - body background: `oklch(0.99 0 0)`
- UI selected the medium project and debug snapshots showed:
  - `projectId=temporal-medium-bouznika-fix-9318797a`
  - `selectedRelease=WB_2026_R05`
  - `layerContracts=Array(9)`
  - `bufferLayers=Array(6)`
- UI flags after project selection:
  - `hasUnavailable=false`
  - `hasWB2026=true`
  - `hasDownload=true`
  - `hasAdditions=true`

## Browser Export Validation

- Actual browser modal path tested: `Télécharger les résultats` -> default `Tout le projet` -> `ESRI Shapefile (.zip)` -> `Télécharger`.
- Suggested filename: `resultats_temporal-medium-bouznika-fix-9318797a_results_shapefile.zip`
- Saved size: `972066839` bytes
- Browser download failure: `null`
- ZIP validation: `unzip -t` passed.
- Repeat identical project-AOI shapefile request used fast cache:
  - Status: `200`
  - Headers returned in `9.28 ms`
  - Content length: `972066839`
  - Content type: `application/zip`
  - `Access-Control-Expose-Headers: Content-Disposition, Content-Length, Content-Type`

## Screenshots

Screenshots are local validation evidence and intentionally not staged because the screenshot directory is about `10 MB`.

- `/Users/tahaelouali/Developer/Building_change_app/artifacts/fixes/finalization_download_progress_ui_combined/screenshots/07_medium_light_default.png`
- `/Users/tahaelouali/Developer/Building_change_app/artifacts/fixes/finalization_download_progress_ui_combined/screenshots/08_medium_project_results_available.png`
- `/Users/tahaelouali/Developer/Building_change_app/artifacts/fixes/finalization_download_progress_ui_combined/screenshots/09_medium_export_modal_project_shapefile.png`
- `/Users/tahaelouali/Developer/Building_change_app/artifacts/fixes/finalization_download_progress_ui_combined/screenshots/02_simplified_progress_timeline_component.png`
- `/Users/tahaelouali/Developer/Building_change_app/artifacts/fixes/finalization_download_progress_ui_combined/screenshots/06_imported_outside_zone_error_readable.png`

## Warnings

- The AOI estimate warned about size before run, but the actual inference tile count was `208` selected tiles per pair, which is within the requested medium validation range.
- The shapefile package is large because it includes shapefile layers and referenced raster imagery. The cache-hit header latency is still near-instant.
