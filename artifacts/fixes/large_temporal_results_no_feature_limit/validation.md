# Validation

Casa project manifest after fixes:

- `WB_2024_R02`: complete baseline.
- `WB_2025_R03`: complete; additions plus 10m/15m/20m buffers are file-backed and tilejson-backed.
- `WB_2026_R05`: complete; additions plus 10m/15m/20m buffers are file-backed and tilejson-backed.
- No hull artifact keys are present.

Persisted additions geometry stats:

- `WB_2025_R03`: 17,040 polygon features, 157,087,847 bytes, bounds `[-7.780723571777344, 33.4329052537601, -7.424864172935486, 33.67194350243372]`.
- `WB_2026_R05`: 25,000 polygon features, 260,961,726 bytes, bounds `[-7.710240483283996, 33.57343808567734, -7.423372864723205, 33.67018450822813]`.

Known validation gap:

- `WB_2026_R05` inference reported 80,130 detections, but the current persisted additions artifact has 25,000 features because the full GeoJSONL was cleaned before the full-artifact promotion fix.
