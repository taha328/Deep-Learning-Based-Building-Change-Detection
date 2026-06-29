# AOI Overlay State Design

Added explicit `aoiOverlayMode` store state:

- `hidden`
- `project_aoi_draw`
- `project_aoi_edit`
- `project_aoi_import`
- `export_custom_zone_draw`
- `export_custom_zone_preview`

Map visibility is now derived from pure helpers in `map-drawing.ts`:

- Parent AOI is visible only when `aoiOverlayMode !== "hidden"`.
- Custom/export AOI is visible only for custom-zone export modes and only when `exportGeometry` exists.

`MapView` applies this guard to `aoi-fill`, `aoi-line`, `export-perimeter-fill`, and `export-perimeter-line` after source syncs and layer-state updates. Result-layer toggles no longer affect AOI visibility. Temporal and pairwise panels hide AOI when leaving the AOI workflow, while custom-zone export drawing/modal workflows keep it visible until success, cancel, close, or failure cleanup clears the export geometry.

