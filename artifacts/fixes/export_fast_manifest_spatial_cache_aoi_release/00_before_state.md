# Before State

- Branch: `codex/inference-persistent-runner-benchmarks`
- Starting HEAD: `827f940eb6c6dbe7ea79eaa436f4c01807a1a220`
- Target installer repo remote: `github-source` -> `https://github.com/taha328/Deep-Learning-Based-Building-Change-Detection.git`
- `origin` also pointed at a repo with the same `main` HEAD before this change: `https://github.com/taha328/building_change_app.git`
- Existing unrelated dirty worktree entries included deleted benchmark artifacts under `artifacts/benchmarks/**` and unrelated untracked diagnostics/ops folders. Those were not reverted.

Inspected code paths:

- Backend export/cache/QGIS writer: `backend/src/services/temporal_exports.py`
- Export route: `backend/src/api/routes/temporal_projects.py`
- AOI state: `frontend/src/app/store.ts`
- Map layers: `frontend/src/features/map/MapView.tsx`
- Drawing helpers/tests: `frontend/src/features/map/map-drawing.ts`
- Temporal export UI: `frontend/src/features/temporal/TemporalMosaicPanel.tsx`

