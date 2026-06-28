# Shared Resolver Design

The fix routes temporal results exports through `resolve_temporal_project_artifact_path`, the same project artifact resolver used by the rest of the temporal project artifact lifecycle.

Key behavior:

- Export hydration now runs in `backend/src/services/temporal_exports.py` before derived geometry and perimeter clipping.
- For each completed milestone, the exporter checks additions plus `10m`, `15m`, and `20m` building-change buffers.
- If an inline payload already exists, it is preserved and logged as a non-empty project payload when applicable.
- If a payload is missing, the exporter resolves the canonical artifact path using `access_mode="export_results"` and reads valid GeoJSON FeatureCollections directly from disk.
- Empty baseline or missing unsupported layers are logged as controlled `EXPORT_LAYER_EMPTY_SKIPPED` events, not false missing-layer failures.
- `backend/src/services/temporal_projects.py` now emits `EXPORT_ARTIFACT_RESOLVED` for export access modes, including path, bytes, media type, and release identifier.

This keeps export behavior aligned with the existing project artifact contract while supporting large file-backed artifacts that are intentionally not embedded in project JSON.
