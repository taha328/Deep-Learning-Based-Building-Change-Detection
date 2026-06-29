# Release Build Evidence

Date: 2026-06-29

## Git

- Source commit pushed to `main`: `b346acb4d7e66e979de5707269fca231027890ef`
- Annotated tag: `v0.1.5`
- Tag object: `00ed53c86c333a4c197b9a26853b2d023190e22e`
- Tag target commit: `b346acb4d7e66e979de5707269fca231027890ef`

## Local Validation Before Release

- `python3 -m py_compile scripts/verify-release-bundle.py`: passed
- `npm --prefix frontend run build`: passed
- `backend/.venv/bin/python -m pytest -q backend/tests/test_config.py`: 21 passed
- `./scripts/package-release.sh`: passed
- `npm --prefix frontend test -- --run`: 128 passed
- `backend/.venv/bin/python -m pytest -q backend/tests`: 582 passed, 5 skipped

## GHCR Image Workflow

- Run: `28371668036`
- URL: `https://github.com/taha328/Deep-Learning-Based-Building-Change-Detection/actions/runs/28371668036`
- Overall conclusion: `success`
- Frontend job: success, `2026-06-29T12:22:29Z` to `2026-06-29T12:28:45Z`
- Backend CPU job: success, `2026-06-29T12:22:30Z` to `2026-06-29T13:03:42Z`
- Optional CUDA job: skipped

## Published Images

Frontend:

- `ghcr.io/taha328/building-change-frontend:v0.1.5`
- index digest: `sha256:9a0c217db3f79a785a98c7ea07e2d6adf97df7bfc6800e81f3d6775a158b9481`
- platforms: `linux/amd64`, `linux/arm64`

Backend CPU:

- `ghcr.io/taha328/building-change-backend:cpu-v0.1.5`
- index digest: `sha256:05818cf6088ce485e55a3918b5f9d8131cd4f1f3041f150420b7d4cafbf18fac`
- platforms: `linux/amd64`, `linux/arm64`

## GitHub Release

- Release URL: `https://github.com/taha328/Deep-Learning-Based-Building-Change-Detection/releases/tag/v0.1.5`
- Draft: false
- Prerelease: false

Assets:

- `building-change-app.zip`
  - size: `682185156`
  - digest: `sha256:fa89608fcfa2a70d7ed01052b5733c37d5b1c0031032f0f169aa4130fe09bf4a`
- `building-change-model-bandon-mtgcdnet-v0.1.5.zip`
  - size: `682166705`
  - digest: `sha256:6c5ef7b73ffb865f251052d5983c97853e2a239b0979f7df9d8fc73befe39592`
- `building-change-model-bandon-mtgcdnet-v0.1.5.sha256`
  - size: `115`
  - digest: `sha256:2d6a22a10ce4fb2e541144472900e4e91865daf03c93008480e828b351342922`
- `building-change-model-bandon-mtgcdnet-v0.1.5.MANIFEST.txt`
  - size: `1271`
  - digest: `sha256:e638f776fed8a90dbf2bbbf474d5527171b85a06f517fa60733310c563469590`
