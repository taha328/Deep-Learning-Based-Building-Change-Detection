# Before State

Date: 2026-06-28

Project validated: `temporal-tanger-city-mqnueqrr-llwf6o`

The temporal results export path depended on result layers already being hydrated into `TemporalProject` milestone payloads. Large result artifacts are intentionally kept file-backed by the project loader, so additions and building-change buffer layers could be absent from `milestone.additions_geojson` and `milestone.buffer_layers_geojson` even when canonical GeoJSON artifacts existed on disk.

Observed real project artifact state before the export fix:

| Release | Additions artifact | Buffer artifacts | Inline export payload |
| --- | ---: | ---: | --- |
| `WB_2019_R03` | 51 bytes, empty | unavailable for baseline | empty |
| `WB_2021_R04` | 21,953,983 bytes | 35,736,294 / 32,787,500 / 31,687,635 bytes | absent |
| `WB_2023_R02` | 19,934,658 bytes | 36,829,127 / 34,237,599 / 33,291,146 bytes | absent |
| `WB_2025_R03` | 19,426,107 bytes | 35,391,758 / 32,849,935 / 31,849,553 bytes | absent |
| `WB_2026_R05` | 20,053,689 bytes | 34,922,620 / 32,262,965 / 31,196,226 bytes | absent |

Failure mode: export code could classify these real file-backed layers as missing or empty because it did not resolve the canonical artifact files when inline milestone payloads were unavailable.
