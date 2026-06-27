# Full Pair Persistent Runner Acceptance Decision

Decision: PASS

Persistent runner default: kept

Rollback: set `APP_BANDON_INFERENCE_MODE=cli_per_tile` if the persistent worker fails to start, crashes, shows platform-specific memory growth, or needs emergency isolation.

Criteria:
- full_real_pair_completed: True
- processed_tiles_equal_total_tiles: True
- worker_crash_false: True
- tile_failure_count_zero: True
- model_load_count_total_lte_one: True
- no_silent_fallback_or_mixed_mode: True
- memory_growth_detected_false: True
- timing_summary_exists: True
- outputs_readable_and_complete: True
