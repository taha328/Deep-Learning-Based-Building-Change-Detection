# Frontend Red/Yellow Overlay Report

Root cause addressed:

- Deprecated hull artifacts and hull layer switches could expose large AOI-like fills.
- Backend large-result publication could keep oversized inline cumulative/effective payloads in metadata.

Fixes:

- Removed hull artifacts from frontend contracts and map layer registration.
- Removed hull toggles/presentation fields.
- Kept additions and buffer layers explicit and color-scoped by release.
- Backend strips large inline temporal result payloads and keeps large geometry file-backed.

Validation:

- `npm test -- --test-reporter=spec`: passed, 118 tests.
- `npm run build`: passed.
- Browser smoke at `http://127.0.0.1:5173/`: app loaded, console warnings/errors empty, visible DOM had no convex/concave/hull text or controls.
