# Building Change Detection Pipeline

This pipeline detects and measures building changes across historical Esri Wayback imagery. Users define an area of interest (AOI), select timeline milestones, run MTGCD-Net inference, review detected additions and growth metrics, and export GIS-ready results.

## Install

The packaged Docker release is the recommended install path. It avoids native GIS, PostgreSQL/PostGIS, Redis, Python, and Node.js setup on your host machine.

### Windows PowerShell - Recommended Docker Install

```powershell
irm https://raw.githubusercontent.com/taha328/Deep-Learning-Based-Building-Change-Detection/main/install.ps1 | iex
```

Requires Windows 10/11 64-bit, internet access, and Docker Desktop installed and running with Docker Compose.

### macOS / Linux - Recommended Docker Install

```bash
curl -fsSL https://raw.githubusercontent.com/taha328/Deep-Learning-Based-Building-Change-Detection/main/install.sh | bash
```

Requires Docker installed and running with Docker Compose.

Open:

- Application: `http://127.0.0.1:8080`
- API documentation: `http://127.0.0.1:8000/docs`

The release package includes the application services and authorized model artifact. CPU Docker is the supported packaged runtime. CUDA is optional and is not production-certified.

### Windows PowerShell - Native Install Without Docker

Native Windows mode is intended for development, debugging, and machines where Docker Desktop is not available. Open PowerShell as Administrator, then run:

```powershell
git clone https://github.com/taha328/Deep-Learning-Based-Building-Change-Detection.git
cd Deep-Learning-Based-Building-Change-Detection
powershell -ExecutionPolicy Bypass -File scripts\setup-windows-native.ps1
```

After setup:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\health-windows-native.ps1
powershell -ExecutionPolicy Bypass -File scripts\start-windows-native.ps1
powershell -ExecutionPolicy Bypass -File scripts\stop-windows-native.ps1
```

This path installs and verifies Git, Python 3.11, Node.js, PostgreSQL 16, PostGIS, Memurai, backend dependencies, frontend dependencies, migrations, and the BANDON model directly on Windows. See [docs/windows-native-setup.md](docs/windows-native-setup.md) for flags, troubleshooting, and service details.

## Requirements

- Docker Engine or Docker Desktop with Docker Compose for the recommended packaged runtime
- `linux/amd64` or `linux/arm64` for Docker images
- At least 16 GB RAM; 24 GB or more is recommended for CPU inference
- Sufficient disk space for imagery caches, project artifacts, and exports
- Native Windows mode requires an elevated PowerShell session and installs host services; see [docs/windows-native-setup.md](docs/windows-native-setup.md)

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

## Operational Notes

Wayback metadata preflight uses adaptive concurrency by default. It starts at 10 workers and downshifts through `10 -> 8 -> 6 -> 4` when tilemap checks show repeated retry-like connection instability. Set `APP_WAYBACK_METADATA_WORKERS_ADAPTIVE_ENABLED=false` to restore fixed-worker behavior, then tune `APP_WAYBACK_METADATA_WORKERS` for the network.

Large Wayback mosaics are written as BigTIFF-safe, tiled, compressed GeoTIFFs through temporary partial files. Outputs are validated before cache publication so classic TIFF 4 GiB overflows and corrupt partial mosaics are not reused.

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
