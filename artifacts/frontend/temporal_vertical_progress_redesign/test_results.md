# Test Results

Date: 2026-06-27

## Frontend Unit Tests

Command:

```sh
npm test --prefix frontend
```

Result:

- `117` passed
- `0` failed
- Log: `frontend_test.log`

New coverage includes:

- finalization remains active when `pair_fraction=1.0` but the job is still running
- done is only returned after completed/succeeded job state
- queued state shows `En attente`
- failed finalization marks the publication stage failed
- French vertical timeline labels are exposed by the mapping

## Frontend Build

Command:

```sh
npm run build --prefix frontend
```

Result: passed.

Note: Vite emitted the existing chunk-size warning for large bundles.

Log: `frontend_build.log`

## Backend Tests

Command:

```sh
backend/.venv/bin/python -m pytest backend/tests -q
```

Result:

- `563` passed
- `5` skipped
- `4` warnings
- Log: `backend_pytest.log`

Backend execution behavior was not changed; this suite was run to satisfy the full prompt validation set.
