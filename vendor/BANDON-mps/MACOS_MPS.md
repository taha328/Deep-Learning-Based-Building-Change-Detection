# BANDON / MTGCDNet on macOS Apple Silicon MPS

This fork is an inference-only macOS Apple Silicon port of the official BANDON repository focused on the official MTGCDNet checkpoint.

Scope:

- Supported:
  - single-device inference
  - Apple Silicon `mps`
  - explicit `cpu`
  - official MTGCDNet config and official checkpoint
- Not supported in this fork:
  - distributed training
  - CUDA shell scripts as the primary interface
  - claiming generic macOS support for all BANDON models

## Why this fork exists

The upstream BANDON README documents a CUDA-era tested stack:

- `torch 1.9.1+cu111`
- `torchvision 0.10.1+cu111`
- `mmcv-full 1.7.0`

That stack predates Apple Silicon MPS support. PyTorch introduced the MPS backend in PyTorch 1.12. The active upstream MTGCDNet inference path also contains CUDA-specific assumptions such as hardcoded `.cuda()` tensor/model moves.

Official references used for this port:

- BANDON repo: [fitzpchao/BANDON](https://github.com/fitzpchao/BANDON)
- PyTorch MPS notes: [docs.pytorch.org/docs/stable/notes/mps](https://docs.pytorch.org/docs/stable/notes/mps)
- PyTorch MPS package reference: [docs.pytorch.org/docs/stable/mps.html](https://docs.pytorch.org/docs/stable/mps.html)
- PyTorch MPS environment variables: [docs.pytorch.org/docs/stable/mps_environment_variables.html](https://docs.pytorch.org/docs/stable/mps_environment_variables.html)
- PyTorch 1.12 release note: [pytorch.org/blog/pytorch-1-12-released](https://pytorch.org/blog/pytorch-1-12-released/)
- MMCV 1.7.0 build docs: [mmcv.readthedocs.io/en/v1.7.0/get_started/build.html](https://mmcv.readthedocs.io/en/v1.7.0/get_started/build.html)

## Verified environment

This fork was verified locally on an Apple Silicon MacBook with the PyTorch MPS backend available.

Verified MPS checks:

- `torch.backends.mps.is_built() == True`
- `torch.backends.mps.is_available() == True`

## Create the environment

From the repository root:

```bash
conda env create -f environment-macos-mps.yml -p ./.conda-macos-mps
conda run -p ./.conda-macos-mps python -m pip install --upgrade pip
conda run -p ./.conda-macos-mps python -m pip install --no-build-isolation -r requirements-macos-mps.txt
```

Notes:

- The verified path here uses `mmcv==1.7.0` from pip without enabling `mmcv-full` CUDA ops.
- If your machine requires a local MMCV build instead of using the wheel, follow the official MMCV 1.7.0 build guide linked above. That was not required for the verified inference run in this fork.

## Verify MPS before running the model

```bash
conda run -p ./.conda-macos-mps python -c "import torch, mmcv, torchvision; print('torch', torch.__version__); print('torchvision', torchvision.__version__); print('mmcv', mmcv.__version__); print('mps_built', torch.backends.mps.is_built()); print('mps_available', torch.backends.mps.is_available())"
```

Expected result on a correctly configured Apple Silicon machine:

- `mps_built True`
- `mps_available True`

## Download the official MTGCDNet checkpoint

The official BANDON README links the MTGCDNet checkpoint through Google Drive. Download it with:

```bash
mkdir -p checkpoints
conda run -p ./.conda-macos-mps python -m gdown --fuzzy "https://drive.google.com/file/d/17KMvDbVDa8b7mwH7JTZ0iXwurSJsOqFe/view?usp=drive_link" -O checkpoints/mtgcdnet_iter_40000.pth
```

## Run inference

Use the new single-device inference entrypoint:

```bash
mkdir -p outputs/run1
conda run -p ./.conda-macos-mps python tools/infer_mps.py \
  --config workdirs_bandon/MTGCDNet/config.py \
  --checkpoint checkpoints/mtgcdnet_iter_40000.pth \
  --image-a /absolute/path/to/image_a.png \
  --image-b /absolute/path/to/image_b.png \
  --device mps \
  --outdir outputs/run1
```

Inputs:

- `image-a` and `image-b` must be same-size RGB images
- this runner is inference-only and processes one pair at a time

Outputs:

- `change_probability.npy`
- `change_probability.png`
- `change_mask.png`
- `change_overlay.png`
- `run_metadata.json`

## MPS-specific behavior in this fork

This fork keeps the official MTGCDNet architecture and official checkpoint, but it patches the inference runner for Apple Silicon:

1. It replaces hardcoded CUDA-only moves in the active change-detection inference path with device-aware `.to(device)` behavior.
2. It converts `SyncBN` to plain `BN` for single-device inference, matching the repo’s own non-DDP logic.
3. It avoids importing optional decode heads and backbones that are irrelevant to MTGCDNet inference and that pull in unsupported macOS dependencies.
4. It avoids `MMDataParallel` in the final inference runner.

### Important MPS limitation handled here

PyTorch MPS still has operator limitations. During verification, MTGCDNet failed on native MPS with:

- `Adaptive pool MPS: input sizes must be divisible by output sizes`

The failure came from MTGCDNet's PSP pooling bins `(1, 2, 3, 6)` combined with the original BANDON test sliding crop size `513x513`, which produces an MPS-incompatible feature-map divisibility case for `adaptive_avg_pool2d`.

This fork does not change the model architecture. Instead, `tools/infer_mps.py` adjusts the sliding test config only for MPS runs:

- original crop size: `513x513`
- original stride: `337x337`
- patched MPS-safe crop size: `480x480`
- patched MPS-safe stride: `312x312`

That change was verified to run natively on `mps` with:

- `--device mps`
- no `PYTORCH_ENABLE_MPS_FALLBACK`
- no silent CPU fallback

## Optional fallback mode

PyTorch documents the optional environment variable `PYTORCH_ENABLE_MPS_FALLBACK=1` for unsupported MPS ops. This fork exposes that only as an explicit opt-in:

```bash
conda run -p ./.conda-macos-mps python tools/infer_mps.py \
  --checkpoint checkpoints/mtgcdnet_iter_40000.pth \
  --image-a /absolute/path/to/image_a.png \
  --image-b /absolute/path/to/image_b.png \
  --device mps \
  --allow-mps-fallback \
  --outdir outputs/run_with_fallback
```

This is not enabled by default and must not be confused with full native MPS execution.

## What was actually verified

Verified locally on Apple Silicon:

- repository imports on the patched inference path
- official checkpoint download
- official checkpoint load
- real inference pass using:
  - `tools/infer_mps.py`
  - `--device mps`
  - no MPS fallback enabled
- output files written successfully

## Remaining limitations

- This fork is inference-only. Training and the original CUDA-oriented `tools/test.py` / distributed utilities were not ported.
- Some non-active training or auxiliary modules still contain CUDA-only code. They are outside the verified MTGCDNet inference path.
- The MPS-safe sliding window logic is a compatibility patch for PyTorch MPS operator limits. It preserves the official model and checkpoint, but it is still a fork-specific runtime adaptation rather than upstream support.
