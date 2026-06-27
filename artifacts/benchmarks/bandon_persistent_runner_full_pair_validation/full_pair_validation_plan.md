# Full Pair Persistent Runner Validation Plan

- Created at: 2026-06-27T07:08:27.518456Z
- Source request: `7f0f8a6b47f2018148907f36`
- Run id: `full_pair_persistent_7f0f8a6b47f2018148907f36_20260627T070827Z`
- Pair: `WB_2026_R03` -> `WB_2026_R05`
- Output directory: `/Users/tahaelouali/Developer/Building_change_app/artifacts/benchmarks/bandon_persistent_runner_full_pair_validation/persistent_runner/20260627T070827Z`
- Runtime directory: `/Users/tahaelouali/Developer/Building_change_app/artifacts/benchmarks/bandon_persistent_runner_full_pair_validation/runtime/20260627T070827Z`
- Environment: `APP_BANDON_INFERENCE_MODE=persistent_runner`, `APP_INFERENCE_TIMING_ENABLED=true`, `APP_INFERENCE_VERBOSE_TILE_LOGS=false`
- Tile size: `1024`
- Overlap: `128`
- Threshold: `0.35`
- Device requested: `auto`
- Checkpoint: `/Users/tahaelouali/Developer/Building_change_app/vendor/BANDON-mps/checkpoints/mtgcdnet_iter_40000.pth`
- Checkpoint sha256: `e1f5c92cb0951fecea1ba74bc5fa86db013d6f8085c2741fe7d60f84e9505813`
- Source raster size: `30464x21504` `EPSG:3857`
- Expected tile count from prior same-pair metadata: `1120` total / `1120` selected

The run uses real persisted Wayback COG imagery and valid masks from the selected production request. Raw full-size outputs are kept under the ignored `persistent_runner/` run directory; root JSON/Markdown summaries are preserved for Git.
