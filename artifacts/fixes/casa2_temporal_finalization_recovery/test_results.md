# Casa 2 Temporal Finalization Test Results

Date: 2026-06-27

## Passed

- `backend/.venv/bin/python -m pytest backend/tests -q`
  - Result: `563 passed, 5 skipped, 4 warnings in 35.77s`
  - Log: `backend_pytest_full.log`
- `npm test --prefix frontend`
  - Result: `112 passed`
  - Log: `frontend_test.log`
- `npm run build --prefix frontend`
  - Result: passed
  - Note: Vite emitted the existing chunk-size warning for `MapView`.
  - Log: `frontend_build.log`

## Focused Regression Coverage

- Completed request publication skips heavy buffer regeneration for oversized geometry and remains idempotent.
- Temporal worker refuses to mark a successful response complete when project finalization did not publish durable metrics and artifacts.
- Compact temporal project loading now reports `complete_milestone_count` from returned milestone statuses instead of counting reference imagery as completion.

## Runtime Verification

- Redis queue length after verification: `0`
- Redis `unacked` count after verification: `0`
- Redis `unacked_index` count after verification: `0`
- Dev backend, frontend, and Celery processes were stopped after UI/API verification.
