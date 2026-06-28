# Performance Design

The export cache now uses explicit metadata for every supported results format and scope.

Cache metadata includes:

- format-specific exporter version;
- shared cache version;
- project JSON fingerprint;
- per-result-artifact fingerprints with path, size, mtime, media type, and recorded SHA-256 when available;
- export scope fingerprint for whole-project or custom imported geometry;
- generated cache key.

This allows repeat requests to use cache hits for both whole-project exports and custom geometry exports. The previous custom path regenerated outputs more often because it did not validate scoped cache metadata the same way.

Real-project timing observations on `temporal-tanger-city-mqnueqrr-llwf6o`:

| Scope | Format | Size | Duration |
| --- | --- | ---: | ---: |
| whole project | shapefile | 5,848,418,425 bytes | 188,380.48 ms |
| custom imported geometry | shapefile | 1,128,701 bytes | 49,557.98 ms |
| custom imported geometry | GeoJSON repeat | 176,440 bytes | 9,117.87 ms |

The repeat custom GeoJSON export preserved file mtime, confirming a real cache hit. The route-level whole-project shapefile POST also hit cache and returned 200.

Current tradeoff: custom exports still hydrate all file-backed result artifacts before clipping. This preserves one shared code path across formats and avoids divergent export semantics, but it means first-time custom exports still pay a read/parse cost for large result artifacts.
