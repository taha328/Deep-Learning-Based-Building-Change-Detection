# Manifest And Cache Design

Implemented a per-project export artifact manifest at `exports/export_artifact_manifest.json` with version `temporal-results-export-artifact-manifest-v1`.

The manifest records release id, layer type, artifact key, resolved path, media type, feature count, bbox, geometry types, file size, mtime, sha256, and source mtime where available. It is written atomically through a `.partial` file and fingerprinted from its entries.

Cache metadata now includes the manifest fingerprint. Fast-cache validation loads only export metadata plus the lightweight manifest, validates project JSON and artifact fingerprints, detects newly-created artifacts for entries that were previously missing, and skips full project hydration on valid project-AOI and custom-zone hits.

Custom export filenames now use a stable canonical GeoJSON geometry hash, for example `results.geojson.custom-884a673367c7`, so repeated exports of the same custom zone immediately hit cache across formats.

