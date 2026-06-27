# Full Pair Persistent Runner Validation Report

## 1. Git and preservation

- Branch commit before validation: `eb37cd807f78601f9eafb9f933de6f0b8859affb`
- Benchmark artifacts preserved: `artifacts/benchmarks/bandon_persistent_runner_medium_acceptance/`
- Raw benchmark tile outputs preserved locally and ignored from Git by `.gitignore`.

## 2. Full-pair run setup

- Project: `temporal-tanger-mqrokfqx-hpimht` (`Tanger`)
- Source request: `7f0f8a6b47f2018148907f36`
- Run id: `full_pair_persistent_7f0f8a6b47f2018148907f36_20260627T070827Z`
- Pair: `WB_2026_R03` -> `WB_2026_R05`
- Source raster size: `30464x21504` `EPSG:3857`
- Tile size / overlap: `1024` / `128`
- Threshold: `0.35`
- Checkpoint: `/Users/tahaelouali/Developer/Building_change_app/vendor/BANDON-mps/checkpoints/mtgcdnet_iter_40000.pth`
- Checkpoint sha256: `e1f5c92cb0951fecea1ba74bc5fa86db013d6f8085c2741fe7d60f84e9505813`
- Device requested: `auto`
- Environment: `APP_BANDON_INFERENCE_MODE=persistent_runner`, `APP_INFERENCE_TIMING_ENABLED=true`, `APP_INFERENCE_VERBOSE_TILE_LOGS=false`
- Output directory: `/Users/tahaelouali/Developer/Building_change_app/artifacts/benchmarks/bandon_persistent_runner_full_pair_validation/persistent_runner/20260627T070827Z`

## 3. Progress and timing

- Processed / total tiles: `1120` / `1120`
- Total wall time seconds: `2728.152`
- Tiles per second: `0.410534`
- Seconds per tile mean / median / p90 / p95 / max: `2.433392` / `3.343095` / `3.605454` / `3.685431` / `8.813744`
- Timing summary: `/Users/tahaelouali/Developer/Building_change_app/artifacts/benchmarks/bandon_persistent_runner_full_pair_validation/timing_summary.json`

## 4. Model lifecycle

- Model load count total: `1`
- Checkpoint load count total: `1`
- Model reused ratio: `0.999107`
- Fallback or mixed mode detected: `False`

## 5. Memory stability

- RSS start MB: `125.703125`
- RSS after model load MB: `439.688`
- RSS midrun MB: `588.75`
- RSS end MB: `444.156`
- RSS peak MB: `665.219`
- RSS slope MB per 1000 tiles: `3.9928507596068075`
- Memory growth detected: `False`
- Worker crash: `False`

## 6. Output validation

- Probability raster: `/Users/tahaelouali/Developer/Building_change_app/artifacts/benchmarks/bandon_persistent_runner_full_pair_validation/persistent_runner/20260627T070827Z/prediction_change_probability.tif`
- Mask raster: `/Users/tahaelouali/Developer/Building_change_app/artifacts/benchmarks/bandon_persistent_runner_full_pair_validation/persistent_runner/20260627T070827Z/prediction_change_mask.tif`
- GeoJSONL: `/Users/tahaelouali/Developer/Building_change_app/artifacts/benchmarks/bandon_persistent_runner_full_pair_validation/persistent_runner/20260627T070827Z/prediction_change_polygons.geojsonl`
- Feature count: `1797`
- Output checksums recorded in output validation report.
- Compact project outputs: not expected for direct tiled full-pair validation; source project/request metadata recorded.
- Output validation report: `/Users/tahaelouali/Developer/Building_change_app/artifacts/benchmarks/bandon_persistent_runner_full_pair_validation/output_validation_report.json`

## 7. Acceptance decision

Decision: `PASS`

Persistent runner remains default: `True`

## 8. Rollback instructions

Set `APP_BANDON_INFERENCE_MODE=cli_per_tile` to force the previous per-tile runner. Use it if persistent worker startup fails, a worker crash occurs, sustained memory growth is observed, or platform migration exposes a persistent-process issue.

## 9. Tests

Pre-validation checks passed:

- `rtk backend/.venv/bin/python -m pytest backend/tests -q`: 560 passed, 5 skipped
- `rtk npm test --prefix frontend`: 112 passed
- `rtk npm run build --prefix frontend`: passed
- `rtk backend/.venv/bin/python -m py_compile backend/src/domain/*.py backend/src/config.py`: passed

Post-run checks passed:

- `rtk backend/.venv/bin/python -m pytest backend/tests -q`: 560 passed, 5 skipped
- `rtk npm test --prefix frontend`: 112 passed
- `rtk npm run build --prefix frontend`: passed
- `rtk backend/.venv/bin/python -m py_compile backend/src/domain/*.py backend/src/config.py`: passed

## 10. Remaining risks

- Longer multi-pair temporal jobs can still exercise cumulative memory and cache behavior beyond this single full pair.
- Larger AOIs than 1,120 selected tiles may show different disk and memory pressure.
- Production CUDA/Linux migration should repeat this validation on target hardware.
