# Final Report

Implementation, local tests, real export validation, UI validation, release publishing, raw installer validation, and Docker CPU pipeline validation are complete.

Release:

- Commit: `1ff1e64310675b6aa8875d23a55050e6b913eae0`
- Tag: `v0.1.4`
- Release URL: `https://github.com/taha328/Deep-Learning-Based-Building-Change-Detection/releases/tag/v0.1.4`
- Image publish workflow: `https://github.com/taha328/Deep-Learning-Based-Building-Change-Detection/actions/runs/28361847788`
- Backend image: `ghcr.io/taha328/building-change-backend:cpu-v0.1.4`
- Frontend image: `ghcr.io/taha328/building-change-frontend:v0.1.4`

Validation summary:

- Frontend tests/build passed.
- Full backend test suite passed.
- Real temporal export validation passed:
  - custom GeoJSON cache hit after generation: about `3.17 ms`
  - custom shapefile cache hit after generation: about `1.54 ms`
  - fast cache hits logged without full project hydration
  - shapefile `.qix` spatial indexes created
- Browser UI validation passed:
  - AOI hidden after result-layer toggles
  - AOI/export perimeter visible in custom-zone preview
  - AOI hidden again after custom clear
- Raw installer command passed:
  - `curl -fsSL https://raw.githubusercontent.com/taha328/Deep-Learning-Based-Building-Change-Detection/main/install.sh | bash`
- Docker CPU health/runtime/smoke validation passed:
  - `device_resolved=cpu`
  - smoke request hash `25d8e23d09315e8fd2cdc44b`
  - `14` artifacts
  - PNG artifact retrieved successfully

Cleanup:

- The raw-installed Docker stack was stopped with `./scripts/stop.sh`.
- Final `docker ps` showed no running containers.

Detailed evidence files:

- `16_git_push_evidence.md`
- `17_release_build_evidence.md`
- `18_raw_installer_validation.md`
- `19_docker_cpu_pipeline_validation.md`
