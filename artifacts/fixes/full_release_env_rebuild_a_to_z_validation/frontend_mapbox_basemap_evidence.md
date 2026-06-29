# Frontend Mapbox Basemap Evidence

Date: 2026-06-29

Target:

`http://127.0.0.1:8080`

Source:

Public raw-installed Docker release, not a local dev server.

## Runtime Config

`/runtime-config.js` served:

```js
window.BUILDING_CHANGE_RUNTIME_CONFIG = {
  VITE_FASTAPI_BACKEND_URL: window.location.origin,
  MAPBOX_API_KEY: "pk.eyJ1IjoidGFoYWVsIiwiYSI6ImNtbnl6dHdqcjA3Z3EycXNmZHQyM3FkZWQifQ.IDf_zeGoMaPHcrsLOD5q7A",
};
```

## Screenshots

Default frontend basemap:

- `frontend_mapbox_basemap_default.png`
- Satellite imagery visible.
- Mapbox and OpenStreetMap attribution visible.

Completed Bouskoura result over basemap:

- `frontend_bouskoura_results_over_basemap.png`
- Satellite imagery visible.
- Result overlay visible on the map.
- Completed milestone controls visible.
- Export/download results control visible.

## Browser Evidence JSON

- `frontend_mapbox_default_evidence.json`
- `frontend_bouskoura_result_evidence.json`

The loaded-result evidence reported:

- `canvasCount: 1`
- `hasMapboxAttribution: true`
- `hasCompletedMilestone: true`
- `hasExportControl: true`
- `consoleMapboxErrors: []`
- `yellowInlineStyleCandidates: []`
