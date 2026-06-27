# Full Temporal Project Persistent Runner Validation Report

## 1. Preflight

- Branch: `codex/inference-persistent-runner-benchmarks`
- Commit: `84ee5fdcbacb7b6eca441002d789c4ce8d7fea3f`
- Git status: `clean before validation; backend/benchmarks/validate_temporal_project_persistent_runner.py added as validation utility`
- Backend preflight tests: `rtk env APP_BANDON_INFERENCE_MODE=persistent_runner APP_INFERENCE_TIMING_ENABLED=true APP_INFERENCE_VERBOSE_TILE_LOGS=false backend/.venv/bin/python -m pytest backend/tests -q => 560 passed, 5 skipped, 4 warnings`
- Persistent runner default: `True`
- CLI rollback available: `True`
- Medium artifacts present: `True`
- Full-pair artifacts present: `True`

## 2. Temporal Project Setup

- Project ID: `temporal-persistent-runner-tiled-validation-20260627T084048Z`
- Job ID: `temporal-validation-temporal-persistent-runner-tiled-validation-20260627T084048Z`
- Milestones: `WB_2018_R03, WB_2021_R04, WB_2026_R05`
- Pair count: `2`
- AOI area m2: `4149625.3`
- Tile size / overlap: `1024 / 128`
- Threshold: `0.35`
- Checkpoint: `/Users/tahaelouali/Developer/Building_change_app/vendor/BANDON-mps/checkpoints/mtgcdnet_iter_40000.pth`
- Device: `auto`
- API used: `src.core_api.run_temporal_project_api`

## 3. Project-Level Performance

- Status: `success`
- Total wall time seconds: `431.703`
- Total tiles across pairs: `60`
- Tiles/sec project: `0.1389842845489173`

## 4. Per-Pair Lifecycle

| Pair | From | To | Run ID | Tiles | Wall s | Tiles/s | Model loads | Checkpoint loads | Reuse ratio | Status |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 1 | WB_2018_R03 | WB_2021_R04 | `1dab0a57fc2e07716170d389` | 30 | 116.239 | 0.258 | 1.0 | 1.0 | 0.967 | complete |
| 2 | WB_2021_R04 | WB_2026_R05 | `c3cc7bd1b1612bc9947dd4ca` | 30 | 114.887 | 0.261 | 1.0 | 1.0 | 0.967 | complete |

## 5. Fallback/Mixed-Mode Verification

- Fallback or mixed mode detected: `False`
- Project model load count total: `2.0`
- Number of pairs: `2`

## 6. Memory Stability Across Pairs

- RSS start MB: `229.172`
- RSS end MB: `981.656`
- RSS peak MB: `1170.937`
- Worker RSS peak by pair MB: `[667.578, 654.609]`
- RSS slope MB/pair: `-12.968999999999937`
- RSS slope MB/1000 tiles: `-432.2999999999979`
- Memory growth detected: `False`

## 7. Pair Output Validation

- Pair output validation passed: `True`

## 8. Final Export Validation

- Export validation passed: `True`
- geojson: `/Users/tahaelouali/Developer/Building_change_app/artifacts/benchmarks/bandon_persistent_runner_full_temporal_project_validation/runtime/tiled_20260627T084048Z/temporal_projects/temporal-persistent-runner-tiled-validation-20260627T084048Z/exports/results.geojson` size `19168327` readable `True`
- qgis_bundle: `/Users/tahaelouali/Developer/Building_change_app/artifacts/benchmarks/bandon_persistent_runner_full_temporal_project_validation/runtime/tiled_20260627T084048Z/temporal_projects/temporal-persistent-runner-tiled-validation-20260627T084048Z/Persistent_runner_tiled_temporal_validation_2018-03_2026-05_export_QGIS.zip` size `42139799` readable `True`
- tsv: `/Users/tahaelouali/Developer/Building_change_app/artifacts/benchmarks/bandon_persistent_runner_full_temporal_project_validation/runtime/tiled_20260627T084048Z/temporal_projects/temporal-persistent-runner-tiled-validation-20260627T084048Z/exports/results_powerbi.tsv` size `1531` readable `True`
- xlsx: `/Users/tahaelouali/Developer/Building_change_app/artifacts/benchmarks/bandon_persistent_runner_full_temporal_project_validation/runtime/tiled_20260627T084048Z/temporal_projects/temporal-persistent-runner-tiled-validation-20260627T084048Z/exports/results.xlsx` size `14577` readable `True`

## 9. Tests

- Preflight: `rtk env APP_BANDON_INFERENCE_MODE=persistent_runner APP_INFERENCE_TIMING_ENABLED=true APP_INFERENCE_VERBOSE_TILE_LOGS=false backend/.venv/bin/python -m pytest backend/tests -q => 560 passed, 5 skipped, 4 warnings`
- Post-validation backend: `rtk env APP_BANDON_INFERENCE_MODE=persistent_runner APP_INFERENCE_TIMING_ENABLED=true APP_INFERENCE_VERBOSE_TILE_LOGS=false backend/.venv/bin/python -m pytest backend/tests -q => 560 passed, 5 skipped, 4 warnings`
- Post-validation frontend tests: `rtk npm test --prefix frontend => 112 passed`
- Post-validation frontend build: `rtk npm run build --prefix frontend => passed (large chunk warning)`
- Post-validation py_compile: `rtk backend/.venv/bin/python -m py_compile backend/src/domain/*.py backend/src/config.py backend/benchmarks/validate_temporal_project_persistent_runner.py => passed`

## 10. Acceptance Decision

- Decision: `PASS`
- PASS: Temporal project contains at least 2 pairs.
- PASS: Temporal project completes successfully.
- PASS: All pairs complete successfully.
- PASS: processed_tiles == total_tiles for every pair.
- PASS: project_model_load_count_total <= number_of_pairs.
- PASS: checkpoint_load_count_total <= number_of_pairs.
- PASS: No pair uses cli_per_tile.
- PASS: No fallback/mixed mode occurs.
- PASS: No worker crash occurs.
- PASS: No progressive memory growth across pairs is detected.
- PASS: All pair outputs are complete and readable.
- PASS: Final temporal project outputs are complete and readable.
- PASS: Final exports are generated/readable/valid.

## 11. Rollback Instructions

Set `APP_BANDON_INFERENCE_MODE=cli_per_tile` to use the per-tile subprocess inference path if persistent worker behavior regresses.

## 12. Remaining Risks

- Larger AOIs and many more temporal pairs can still put pressure on disk and MPS memory.
- CUDA/Linux deployment should be validated separately if production moves off this MPS environment.
- Export edge cases with very large feature collections remain worth separate stress testing.
