# Overview Button Removal

Files changed:

- `frontend/src/features/temporal/TemporalMosaicPanel.tsx`
- `frontend/src/features/temporal/latest-source-removal.test.ts`

Exact buttons removed from the overview section:

- `temporal.save_button` (`Save` / `Enregistrer`)
- `temporal.export_button` (`Export` / `Exporter`)

Scope confirmation:

- Backend save/export APIs were not removed.
- The frontend project bundle export remains available from the dedicated Downloads panel.
- Temporal results export/download controls remain available in the results/download workflow.
- QGIS/Shapefile and other temporal result export code paths were not removed.

Regression coverage:

- Added `overview omits save and export buttons while download exports remain available`.
