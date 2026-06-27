# Medium Persistent Runner Benchmark Plan

- Output root: `artifacts/benchmarks/bandon_persistent_runner_medium_acceptance`
- Tile size: `1024`
- Overlap: `128`
- Threshold: `0.3`
- Device: `auto`
- Crop: `15616x7936`

## Runnable Commands

```bash
APP_INFERENCE_TIMING_ENABLED=true APP_BANDON_INFERENCE_MODE=cli_per_tile backend/.venv/bin/python scripts/benchmark_bandon_inference_modes.py --modes cli_per_tile --output-root artifacts/benchmarks/bandon_persistent_runner_medium_acceptance --allow-existing --t1-mosaic backend/runtime_cache/wayback_mosaics/e95e492902e2055ff4b745d997b28169/mosaic.tif --t2-mosaic backend/runtime_cache/wayback_mosaics/883764ece0cf4a8ead052769bcd02c10/mosaic.tif --t1-valid-mask backend/runtime_cache/wayback_mosaics/e95e492902e2055ff4b745d997b28169/valid_mask.tif --t2-valid-mask backend/runtime_cache/wayback_mosaics/883764ece0cf4a8ead052769bcd02c10/valid_mask.tif --crop-width 15616 --crop-height 7936 --tile-size 1024 --overlap 128 --threshold 0.3 --device auto --cli-repeats 1
APP_INFERENCE_TIMING_ENABLED=true APP_BANDON_INFERENCE_MODE=persistent_runner backend/.venv/bin/python scripts/benchmark_bandon_inference_modes.py --modes persistent_runner --output-root artifacts/benchmarks/bandon_persistent_runner_medium_acceptance --allow-existing --skip-crop-inputs --t1-mosaic backend/runtime_cache/wayback_mosaics/e95e492902e2055ff4b745d997b28169/mosaic.tif --t2-mosaic backend/runtime_cache/wayback_mosaics/883764ece0cf4a8ead052769bcd02c10/mosaic.tif --t1-valid-mask backend/runtime_cache/wayback_mosaics/e95e492902e2055ff4b745d997b28169/valid_mask.tif --t2-valid-mask backend/runtime_cache/wayback_mosaics/883764ece0cf4a8ead052769bcd02c10/valid_mask.tif --crop-width 15616 --crop-height 7936 --tile-size 1024 --overlap 128 --threshold 0.3 --device auto --persistent-repeats 2
```

The script uses real tiled inference code paths and the real configured BANDON checkpoint.
