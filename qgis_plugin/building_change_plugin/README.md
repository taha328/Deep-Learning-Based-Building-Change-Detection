# Building Change Detection QGIS Plugin

This is a normal QGIS plugin for the `Building_change_app` backend API.

The plugin does not run inference locally and does not duplicate backend processing. It sends AOI and temporal project requests to the backend, polls jobs with QGIS background tasks, and loads backend-generated imagery, rasters, vectors, and exports into the current QGIS project.

## Workflow

1. Start the backend API.
2. Open the plugin dock in QGIS.
3. Set the backend URL, defaulting to `http://127.0.0.1:8000`.
4. Check backend health.
5. Select or draw an AOI.
6. Load Wayback releases and choose milestones.
7. Validate and run the temporal project.
8. Load generated result layers.
9. Download Excel or KML exports.

The backend remains the source of truth for model selection, inference, postprocessing, vectorization, metrics, and exports.
