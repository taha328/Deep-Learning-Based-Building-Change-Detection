# Real Project Validation

Project: `temporal-tanger-city-mqnueqrr-llwf6o`

Validation completed against the local runtime project at:

`/Users/tahaelouali/Developer/Building_change_app/backend/runtime_cache/temporal_projects/temporal-tanger-city-mqnueqrr-llwf6o`

Whole-project export results:

| Format | Bytes | Duration |
| --- | ---: | ---: |
| `xlsx` | 325,421 | 26,059.19 ms |
| `kml` | 89,567,531 | 39,564.79 ms |
| `geojson` | 216,195,222 | 32,516.15 ms |
| `topojson` | 23,515,520 | 36,498.94 ms |
| `json` | 41,304 | 23,765.35 ms |
| `tsv` | 2,095 | 51,941.50 ms |
| `shapefile` | 5,848,418,425 | 188,380.48 ms |

Custom imported-scope export results:

| Format | Bytes | Duration |
| --- | ---: | ---: |
| `xlsx` | 7,840 | 50,768.98 ms |
| `kml` | 73,093 | 50,754.96 ms |
| `geojson` | 176,440 | 50,457.69 ms |
| `topojson` | 249 | 50,944.50 ms |
| `json` | 42,032 | 53,043.92 ms |
| `tsv` | 1,335 | 52,899.27 ms |
| `shapefile` | 1,128,701 | 49,557.98 ms |

Acceptance checks:

- Shapefile POST to `/api/temporal-projects/temporal-tanger-city-mqnueqrr-llwf6o/exports/results` with project AOI returned `200 OK`, `application/zip`, 5,848,418,425 bytes.
- Empty/outside custom scope returned `400` with code `invalid_export_perimeter` and message `La zone sélectionnée est hors de l’AOI du projet.`
- Custom GeoJSON repeat returned the same file with unchanged mtime.
- Whole-project shapefile contained 20 `.shp` layers and a QGZ project file.
- File-backed export logs showed non-empty layer resolution for `WB_2021_R04`, `WB_2023_R02`, `WB_2025_R03`, and `WB_2026_R05`.
- No false `EXPORT_LAYER_MISSING` was emitted for the resolved file-backed result layers.

Note: release display labels in shapefile filenames are quarter labels derived from release dates. For example, `WB_2021_R04` maps to `2021_Q1` in generated layer filenames.
