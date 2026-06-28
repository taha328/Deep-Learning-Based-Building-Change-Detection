# Failed Project Recovery

## Project

- Project id: `temporal-test-mqy297ia-qoaslk`
- Job id: `job-1c366434cf134d1ea9a5cf975c64c65e`
- Failed request hash: `e9d601f3c83390a851c90cf4`
- Failure mode before fix: inference completed, then finalization crashed in obsolete growth-envelope generation with `TypeError: float() argument must be a string or a real number, not 'NoneType'`.

## Recovery Path

The existing tiled inference artifacts were recoverable, so inference was not rerun. I invoked `publish_completed_tiled_request` against the completed request with post-completion cleanup disabled for evidence preservation:

```text
publish_completed_tiled_request(
  request_id="e9d601f3c83390a851c90cf4",
  project_id="temporal-test-mqy297ia-qoaslk",
  baseline_release="WB_2024_R05",
  target_release="WB_2026_R05",
  settings.post_completion_request_cleanup_enabled=False,
)
```

## Evidence

- Existing inference evidence: `TILED_INFERENCE_DONE runId=e9d601f3c83390a851c90cf4 processedTiles=25 totalTiles=25 features=73`.
- Fixed finalization logged `TEMPORAL_GROWTH_ENVELOPE_DISABLED ... reason=product_removed`.
- Recovered project summary: `complete_milestone_count=3`.
- Milestone state after recovery:
  - `WB_2024_R05`: `status=complete`, `request_hash=None`, `additions=0`, `growth_envelope_area_m2=0.0`, `cumulative_growth_envelope_geojson=None`
  - `WB_2025_R04`: `status=complete`, `request_hash=ebe9c1de047e165e596f2db1`, `additions=39`, `growth_envelope_area_m2=0.0`, `cumulative_growth_envelope_geojson=None`
  - `WB_2026_R05`: `status=complete`, `request_hash=e9d601f3c83390a851c90cf4`, `additions=70`, `growth_envelope_area_m2=0.0`, `cumulative_growth_envelope_geojson=None`
- `cumulative_growth_envelope.geojson` was not generated for `WB_2026_R05`.
- Frontend validation screenshot: `/Users/tahaelouali/Developer/Building_change_app/artifacts/fixes/finalization_download_progress_ui_combined/screenshots/03_recovered_project_results_available.png`.

## Export Validation On Recovered Project

- Browser whole-project shapefile download succeeded:
  - Filename: `resultats_temporal-test-mqy297ia-qoaslk_results_shapefile.zip`
  - Browser elapsed time: about `2967 ms`
  - ZIP validation: `unzip -t` passed
- Browser whole-project GeoJSON download succeeded:
  - Filename: `resultats_temporal-test-mqy297ia-qoaslk.geojson`
  - Browser elapsed time: about `520 ms`
  - JSON validation: valid FeatureCollection with `436` features
- Custom-zone shapefile export succeeded by direct API:
  - Status: `200 OK`
  - Content length: `443328`
  - ZIP validation: `unzip -t` passed
- Outside custom-zone export returned controlled backend error:
  - Status: `400`
  - Code: `invalid_export_perimeter`
  - Message: `La zone sélectionnée est hors de l’AOI du projet.`

## Result

The failed project was recovered from existing inference artifacts. It now loads with results available, without any growth-envelope artifact being required for completion, layer availability, or export.
