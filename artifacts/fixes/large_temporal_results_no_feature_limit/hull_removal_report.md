# Hull Removal Report

Removed convex/concave hull behavior from backend, frontend, tests, and export cleanup paths.

- Backend schema no longer exposes `cumulative_convex_hull_geojson`; legacy persisted hull fields are stripped on load.
- Temporal exports, repository persistence, request cleanup, and compact metadata scripts no longer register hull artifacts.
- Vectorization no longer imports or builds concave hulls for temporal growth envelopes.
- Frontend contracts, map layer registration, layer colors, translations, and temporal panels no longer expose hull layers or controls.
- Tests assert deprecated hull artifacts are absent from lazy fetch and visible layer registries.

Remaining hull strings are compatibility-only schema stripping for old persisted JSON.
