# Local Quality Gate

- Backend tests:
  - Command: `backend/.venv/bin/python -m pytest backend/tests -q`
  - Result: `581 passed, 5 skipped, 4 warnings`
- Frontend tests:
  - Command: `npm test --prefix frontend`
  - Result: `127 passed`
- Frontend build:
  - Command: `npm run build --prefix frontend`
  - Result: passed
  - Warning: existing Vite chunk-size warnings over 500 kB.
- Python compile:
  - Command: `find backend/src -name "*.py" -print0 | xargs -0 backend/.venv/bin/python -m py_compile`
  - Result: passed
- Whitespace check:
  - Command: `git diff --check`
  - Result: passed
