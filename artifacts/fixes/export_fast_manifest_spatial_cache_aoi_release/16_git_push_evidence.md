# Git Push Evidence

Release code was committed, tagged, and pushed. A follow-up documentation commit then added these release validation evidence files to `main`.

- Release code commit: `1ff1e64310675b6aa8875d23a55050e6b913eae0`
- Evidence follow-up commit: `478f65abd08239d6991f18aa7b9ff38327336b1e`
- Remote branch: `github-source/main`
- Remote branch includes the release code commit and the evidence follow-up commit.
- Annotated tag: `v0.1.4`
- Tag object SHA: `51a7500d6f8cf49918d5793c0a35c0d375849781`
- Peeled tag commit: `1ff1e64310675b6aa8875d23a55050e6b913eae0`

Image publish workflow:

- Run: `https://github.com/taha328/Deep-Learning-Based-Building-Change-Detection/actions/runs/28361847788`
- Overall status: `completed`
- Overall conclusion: `success`
- Backend CPU image job: `success`, `2026-06-29T09:21:45Z` to `2026-06-29T10:01:49Z`
- Frontend image job: `success`, `2026-06-29T09:21:45Z` to `2026-06-29T09:28:11Z`
- Optional CUDA job: `skipped`

Published image manifests:

- `ghcr.io/taha328/building-change-backend:cpu-v0.1.4`
  - Digest: `sha256:a1492d8ed12312a860818599caca96841b43c4c7476ac6bc63d0e37c4f422110`
  - Platforms: `linux/amd64`, `linux/arm64`
- `ghcr.io/taha328/building-change-frontend:v0.1.4`
  - Digest: `sha256:3da40c5cbe055fdbbf1ab17cb01827c5ef1565a54bfc4d80f805ce215187a4b0`
  - Platforms: `linux/amd64`, `linux/arm64`
