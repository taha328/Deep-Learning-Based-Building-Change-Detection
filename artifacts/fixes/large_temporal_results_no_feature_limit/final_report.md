# Final Report

## Problem summary

Casa stopped after a large completed temporal pair instead of continuing to the next milestone. Hull artifacts also risked showing large AOI-like overlays.

## Root causes

- Large results were treated as a failure/partial-finalization condition.
- Reuse planning depended too heavily on request-cache availability.
- Deprecated hull artifacts remained in backend/frontend contracts.
- Tiled browser GeoJSON caps could leak into temporal artifacts.

## 5000 limit removal

The 5,000 threshold now controls only inline derived geometry. It no longer stops temporal project execution.

## Large result strategy

Large additions and buffers stay file-backed and vector-tile-backed. Inline derived cumulative/effective geometry is skipped with explicit logs.

## Hull removal

Convex/concave hull backend, frontend, tests, export, cleanup, and compact metadata paths were removed. Legacy persisted fields are stripped on load.

## Frontend overlay fix

Hull layers and controls were removed. Browser smoke found no hull text/controls and no console errors.

## Casa resume

The project resumed and completed `WB_2026_R05`. Missing `WB_2025_R03` buffers were generated afterward from file-backed additions.

## Files changed

Backend temporal publication/reuse/externalization, tiled processing artifact writing, vector envelope generation, schemas, exports, cleanup, compact metadata, tests, and frontend temporal map contracts/UI.

## Tests/validation

Full backend suite passed on rerun: 577 passed, 5 skipped. Frontend tests/build passed.

## Rollback

Revert the commit and restore previous runtime project metadata/artifacts from backup if needed. No unrelated benchmark deletions were staged by this fix.

## Remaining risks

The already-cleaned Casa `WB_2026_R05` additions artifact is capped at 25,000 persisted features while inference reported 80,130 detections. The code now prevents recurrence, but this specific artifact requires rerunning that pair or external deleted-file recovery for full restoration.
