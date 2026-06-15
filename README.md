# Building Change Detection Pipeline

This pipeline detects and measures building changes across historical Esri Wayback imagery. Users define an area of interest (AOI), select timeline milestones, run MTGCD-Net inference, review detected additions and growth metrics, and export GIS-ready results.

## Install

You can install the packaged GitHub release rather than clone and manually configure the repository:

### Windows PowerShell

```powershell
irm https://raw.githubusercontent.com/taha328/Deep-Learning-Based-Building-Change-Detection/main/install.ps1 | iex
```

Requires Windows 10/11 64-bit, internet access, and Docker Desktop installed and running with Docker Compose.

### macOS / Linux

```bash
curl -fsSL https://raw.githubusercontent.com/taha328/Deep-Learning-Based-Building-Change-Detection/main/install.sh | bash
```

Requires Docker installed and running with Docker Compose.

Open:

- Application: `http://127.0.0.1:8080`
- API documentation: `http://127.0.0.1:8000/docs`

The release package includes the application services and authorized model artifact. CPU Docker is the supported packaged runtime. CUDA is optional and is not production-certified.

## Requirements

- Docker Engine or Docker Desktop with Docker Compose
- `linux/amd64` or `linux/arm64`
- At least 16 GB RAM; 24 GB or more is recommended for CPU inference
- Sufficient disk space for imagery caches, project artifacts, and exports

## Pipeline Steps

1. Define an AOI and select Esri Wayback milestones.
2. Retrieve and cache historical imagery.
3. Build mosaics and georeferenced reference COGs.
4. Run MTGCD-Net building change inference between milestones.
5. Clean and vectorize detected changes.
6. Calculate temporal metrics and prepare map layers and GIS exports.

## Outputs

Project artifacts persist in the Docker runtime cache under:

```text
/data/runtime_cache/temporal_projects/<project_id>/
```

Typical outputs include milestone `reference_imagery_cog.tif` files, building-addition GeoJSON, buffer and growth layers, project metadata, and downloadable GIS export bundles. Use the application download workflow to retrieve results from the packaged runtime.

## Why MTGCD-Net

MTGCD-Net is a multi-task guided network designed for building change detection in off-nadir aerial imagery. Viewing-angle and alignment differences can shift roofs, facades, and footprints between dates, creating apparent changes that are not real construction.

The network uses auxiliary roof/facade parsing, roof-to-footprint offset, and bi-temporal roof-matching tasks to guide change detection. It is used here because historical Wayback imagery can contain similar alignment and off-nadir effects, making a geometry-aware building change model more suitable than simple pixel differencing.

## Useful Commands

Run these commands from the installed release directory:

```bash
./scripts/health.sh
./scripts/validate-runtime.sh
./scripts/logs.sh
./scripts/stop.sh
```

Ordinary stops preserve PostgreSQL data, imagery caches, and generated project artifacts.

## Citation

```bibtex
@article{pang2023detecting,
  title={Detecting building changes with off-nadir aerial images},
  author={Pang, Chao and Wu, Jiang and Ding, Jian and Song, Can and Xia, Gui-Song},
  journal={Science China Information Sciences},
  volume={66},
  number={4},
  pages={1--15},
  year={2023},
  publisher={Springer}
}
```
