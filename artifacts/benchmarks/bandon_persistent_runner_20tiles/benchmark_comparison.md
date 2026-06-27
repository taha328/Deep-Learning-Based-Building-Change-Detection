# BANDON Inference Mode Benchmark

| metric | cli_per_tile | persistent_runner |
|---|---:|---:|
| processed_tiles | 20 | 20 |
| total_wall_time_seconds | 134.544 | 13.332 |
| tiles_per_second | 0.148651 | 1.500143 |
| seconds_per_tile_mean | 6.725837 | 0.645076 |
| seconds_per_tile_median | 6.818012 | 0.326668 |
| seconds_per_tile_p95 | 6.922546 | 0.693537 |
| model_load_count_total | 20 | 1 |
| checkpoint_load_ms_total | 13494.78 | 687.186 |
| forward_ms_total | 41622.105 | 3195.835 |
| subprocess_wall_ms_total | 132105.818 | 0.0 |
| persistent_request_ms_total | 0.0 | 6888.107 |
| peak_rss_mb | 229.578125 | 229.578125 |

## Speedup

- total wall speedup: 10.091809
- tiles/sec speedup: 10.091711
- model load reduction: 20.0

## Output Equivalence

- max_abs_diff: 0.0
- mean_abs_diff: 0.0
- p99_abs_diff: 0.0
- binary_mask_mismatch_count: 0
- polygon_count_delta: 0
- polygon_total_area_delta_m2: 0.0
