# Docker CPU Pipeline Validation

Date: 2026-06-29

Target:

- Public raw-installed release bundle
- Frontend: `http://127.0.0.1:8080`
- Backend: `http://127.0.0.1:8000`
- Backend image: `ghcr.io/taha328/building-change-backend:cpu-v0.1.5`

## Runtime

`./scripts/validate-runtime.sh` reported:

- `available=true`
- `checkpoint_path=/models/bandon/mtgcdnet_iter_40000.pth`
- `config_path=/app/vendor/BANDON-mps/workdirs_bandon/MTGCDNet/config.py`
- `device_requested=auto`
- `device_resolved=cpu`
- `cuda_available=false`
- `mps_available=false`
- `torch_version=2.12.1+cpu`
- `mmcv_version=1.7.0`

## Detection Smoke Job

Bundled smoke job:

- job id: `job-aad23e11a3e7467394c1865309a9577e`
- status: `completed`
- request hash: `25d8e23d09315e8fd2cdc44b`
- resolved device: `cpu`
- artifact count: `14`
- retrieved PNG: `/data/runtime_cache/requests/25d8e23d09315e8fd2cdc44b/t1_preview.png`

## Temporal CPU Job, Tiny AOI

Project:

- project id: `release-a2z-20260629`
- AOI: `[-7.0,33.0]` to `[-6.9975,33.0025]`
- releases: `WB_2026_R04` to `WB_2026_R05`
- estimated tiles: `18`

Job:

- job id: `job-9fa7249c80af4007823c554324ed969c`
- status: `completed`
- result run id: `temporal-release-a2z-20260629-bce1adb744a84bdd8c53d813467cba02`
- pair request hash: `8d20c914c82894b7795ec08c`
- complete milestones: `2`
- result artifacts on target milestone: `2`

The tiny AOI completed successfully but produced zero additions, so it was used as an installer/runtime temporal check, not as the shapefile export acceptance run.

## Temporal CPU Job, Non-empty Export AOI

Project:

- project id: `release-a2z-bouskoura-20260629`
- AOI: `[-7.536,33.366]` to `[-7.533,33.369]`
- releases: `WB_2025_R03` to `WB_2026_R05`
- estimated tiles: `24`

Job:

- job id: `job-d954dc5e24e6492eba8ddf98e367ac17`
- status: `completed`
- result run id: `temporal-release-a2z-bouskoura-20260629-195a732cabdb476d9ae870cba0dcd477`
- pair request hash: `23023e30a328b4c9a29240d7`
- complete milestones: `2`
- `WB_2026_R05` additions feature count: `25`
- `WB_2026_R05` artifact count: `5`

This run was used for full export and frontend result-layer validation.
