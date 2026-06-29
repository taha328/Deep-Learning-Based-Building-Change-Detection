# Release Build Evidence

GitHub release:

- Release: `v0.1.4`
- URL: `https://github.com/taha328/Deep-Learning-Based-Building-Change-Detection/releases/tag/v0.1.4`
- Draft: `false`
- Prerelease: `false`

Uploaded assets:

- `building-change-app.zip`
  - Size: `682184101`
  - Digest: `sha256:31813f1602aff30b55c90956911fc44c1eb67acac9b537b5b68ad56e42835ca9`
  - State: `uploaded`
- `building-change-model-bandon-mtgcdnet-v0.1.4.zip`
  - Size: `682166706`
  - Digest: `sha256:43b7d9a7b347e1c0e5bf141bcfb6dbe15849b7c6d5aeb70134a6d22fb6275738`
  - State: `uploaded`
- `building-change-model-bandon-mtgcdnet-v0.1.4.sha256`
  - Size: `115`
  - Digest: `sha256:b8fff21240c8086e16461355feb7bca50e70e7ccebcf8277fe388e19a74d08b0`
  - State: `uploaded`
- `building-change-model-bandon-mtgcdnet-v0.1.4.MANIFEST.txt`
  - Size: `1271`
  - Digest: `sha256:d9dfc9d576d55178bf574ab1fa65beb1249fff4e697500860ba4cb5243622f58`
  - State: `uploaded`

Local release bundle validation:

- Command: `./scripts/package-release.sh`
- Result: passed
- Local bundle: `dist/building-change-app.zip`
- Bundle `.env` image tags:
  - `BACKEND_IMAGE=ghcr.io/taha328/building-change-backend:cpu-v0.1.4`
  - `FRONTEND_IMAGE=ghcr.io/taha328/building-change-frontend:v0.1.4`
  - CUDA tag remains `cuda-v0.1.0`.
