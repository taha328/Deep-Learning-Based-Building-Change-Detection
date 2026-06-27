# BANDON Inference Mode Benchmark

| metric | cli_per_tile | persistent_runner |
|---|---:|---:|
| processed_tiles | 1 | 1 |
| total_wall_time_seconds | 8.239 | 6.752 |
| tiles_per_second | 0.12137 | 0.148111 |
| seconds_per_tile_mean | 8.234742 | 6.399113 |
| seconds_per_tile_median | 8.234742 | 6.399113 |
| seconds_per_tile_p95 | 8.234742 | 6.399113 |
| model_load_count_total | 1 | 1 |
| checkpoint_load_ms_total | 776.663 | 657.769 |
| forward_ms_total | 2347.147 | 2209.107 |
| subprocess_wall_ms_total | 7801.797 | 0.0 |
| persistent_request_ms_total | 0.0 | 2392.996 |
| peak_rss_mb | 133.984375 | 138.359375 |

## Speedup

- total wall speedup: 1.220231
- tiles/sec speedup: 1.220326
- model load reduction: 1.0

## Output Equivalence

- max_abs_diff: 0.0
- mean_abs_diff: 0.0
- p99_abs_diff: 0.0
- binary_mask_mismatch_count: 0
- polygon_count_delta: 0
- polygon_total_area_delta_m2: 0.0
