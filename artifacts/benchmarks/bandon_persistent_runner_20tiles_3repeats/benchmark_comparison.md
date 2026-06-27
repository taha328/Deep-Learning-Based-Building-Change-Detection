# BANDON Inference Mode Benchmark

| metric | cli_per_tile | persistent_runner |
|---|---:|---:|
| processed_tiles | 20.0 | 20.0 |
| total_wall_time_seconds | 142.353667 | 13.690667 |
| tiles_per_second | 0.140772 | 1.466061 |
| seconds_per_tile_mean | 7.116219 | 0.663013 |
| seconds_per_tile_median | 7.108825 | 0.330313 |
| seconds_per_tile_p95 | 7.986559 | 0.703772 |
| model_load_count_total | 20.0 | 1.0 |
| checkpoint_load_ms_total | 15397.838667 | 728.961333 |
| forward_ms_total | 43661.780333 | 3069.163333 |
| subprocess_wall_ms_total | 140100.990333 | 0.0 |
| persistent_request_ms_total | 0.0 | 6789.244 |
| peak_rss_mb | 229.421875 | 229.421875 |

## Speedup

- total wall speedup: 10.397862
- tiles/sec speedup: 10.414436
- model load reduction: 20.0

## Output Equivalence

- max_abs_diff: 0.0
- mean_abs_diff: 0.0
- p99_abs_diff: 0.0
- binary_mask_mismatch_count: 0
- polygon_count_delta: 0
- polygon_total_area_delta_m2: 0.0
