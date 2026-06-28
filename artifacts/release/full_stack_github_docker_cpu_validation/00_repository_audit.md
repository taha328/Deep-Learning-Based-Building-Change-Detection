# Repository Audit

- Repository root: `/Users/tahaelouali/Developer/Building_change_app`
- Current branch: `codex/inference-persistent-runner-benchmarks`
- Starting HEAD: `0d899c420a85b9489d1eeb431ff2d2e90413c3e3`
- Target repository remote is configured as `github-source`: `https://github.com/taha328/Deep-Learning-Based-Building-Change-Detection.git`
- Existing `origin` points to a different URL: `https://github.com/taha328/building_change_app.git`

Relevant source changes present for this release:

- Frontend overview `Save` and `Export` button removal.
- Prior validated frontend/backend changes currently in the worktree for threshold defaults, AOI rendering, and QGIS startup extents.
- Deployment env threshold alignment to `APP_CHANGE_THRESHOLD=0.50`.

Excluded/generated worktree noise:

- Pre-existing deleted files under `artifacts/benchmarks/**` remain unstaged.
- Untracked `artifacts/diagnostics/` and `artifacts/ops/` remain unstaged.
- Generated screenshots/JSON under `artifacts/fixes/qgis_extent_threshold_aoi_combined/` are local evidence and remain unstaged.
- Runtime cache, Docker volumes, exports, model outputs, ZIPs, and screenshots are excluded from commit.

Detected release/build/install process:

- Release bundle script: `scripts/package-release.sh`
- Release bundle verification: `scripts/verify-release-bundle.py`
- Bash installer: `install.sh`, downloads `building-change-app.zip` from the latest GitHub release.
- Deployment compose file: `deploy/docker-compose.yml`
- Deployment env template: `deploy/.env.example`
- Image publishing workflow: `.github/workflows/publish-images.yml`
- Release bundle workflow: `.github/workflows/publish-release-bundle.yml`
- Installer validation workflow: `.github/workflows/validate-installers.yml`

Risks before release:

- Local branch is not `main`; push may require merging or pushing HEAD to `github-source main`.
- Direct push may fail if credentials or branch protection block it.
- Release/package update may require GitHub Actions or GHCR permissions.
- Full Docker CPU inference can be slow and depends on Docker availability and public/private package access.
