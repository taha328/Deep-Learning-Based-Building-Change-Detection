# Feature Limit Inspection

- The 5,000-feature threshold is still used as an inline-derived-geometry threshold.
- It is no longer a temporal-project failure condition.
- Large additions now continue as file-backed artifacts with vector-tile metadata.
- Optional inline cumulative/effective derived geometry is skipped for large results and logged as `TEMPORAL_OPTIONAL_EXPORT_SKIPPED_LARGE_RESULT`.
- Remaining `feature_limit_exceeded` text is limited to DB geometry-column storage skipping in `backend/src/repositories/temporal_project_repository.py`; it does not fail temporal project execution.

Additional bug found during validation: tiled run responses cap browser GeoJSON at 25,000 features. The fix now writes the on-disk `building_change_polygons.geojson` from the full GeoJSONL stream and promotes that full file into temporal milestone artifacts.
