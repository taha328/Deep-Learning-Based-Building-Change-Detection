# BANDON Inference Mode Benchmark

| metric | cli_per_tile | persistent_runner |
|---|---:|---:|
| processed_tiles | 200.0 | 200.0 |
| total_wall_time_seconds | 1384.694 | 75.0095 |
| tiles_per_second | 0.144436 | 2.666342 |
| seconds_per_tile_mean | 6.921995 | 0.371398 |
| seconds_per_tile_median | 6.886056 | 0.336599 |
| seconds_per_tile_p90 | 7.15847 | 0.373599 |
| seconds_per_tile_p95 | 7.982692 | 0.38312 |
| seconds_per_tile_max | 9.8572 | 6.831537 |
| model_load_count_total | 200.0 | 1.0 |
| checkpoint_load_count_total | 200.0 | 1.0 |
| checkpoint_load_ms_total | 141258.19 | 693.442 |
| forward_ms_total | 432460.256 | 10501.1495 |
| subprocess_wall_ms_total | 1362503.994 | 0.0 |
| persistent_worker_wall_ms_total | 0.0 | 47062.7315 |
| peak_rss_mb | 1090.859375 | 156.796875 |

## Speedup

- total wall speedup: 18.460248
- tiles/sec speedup: 18.46037
- model load reduction: 200.0
- latency reduction percent: 94.635

## Memory Stability

- rss_start_mb: 1070.25
- rss_end_mb: 1121.375
- rss_peak_mb: 1125.266
- rss_max_minus_min_mb: 55.016000000000076
- rss_slope_mb_per_100_tiles: 7.221132
- memory_growth_detected: False

## Output Equivalence

- max_abs_diff: 0.0
- mean_abs_diff: 0.0
- p99_abs_diff: 0.0
- binary_mask_mismatch_count: 0
- polygon_count_delta: 0
- polygon_total_area_delta_m2: 0.0
