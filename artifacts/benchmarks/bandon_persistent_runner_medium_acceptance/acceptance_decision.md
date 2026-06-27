# Acceptance Decision

Decision: **PASS**

| criterion | result |
|---|---:|
| persistent_runner benchmark used 200-500 real tiles | PASS |
| speedup_total_wall >= 5.0 OR speedup_tiles_per_second >= 5.0 | PASS |
| persistent_runner model_load_count_total <= 1 | PASS |
| max_abs_diff == 0.0 | PASS |
| binary_mask_mismatch_count == 0 | PASS |
| memory_growth_detected == false | PASS |
| worker_crash == false | PASS |
| backend tests pass | PASS |

- CLI tiles: `200.0`
- Persistent tiles: `200.0`
- Wall speedup: `18.460248`
- Tiles/sec speedup: `18.46037`
- Persistent model loads: `1.0`
- Max abs diff: `0.0`
- Binary mask mismatches: `0`
- Memory growth detected: `False`
- Backend tests: `555 passed, 5 skipped, 4 warnings`
