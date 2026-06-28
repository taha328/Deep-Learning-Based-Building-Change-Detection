# Large Result Design

Large temporal outputs are handled as file-backed artifacts:

- Additions and buffers are written under `backend/runtime_cache/temporal_projects/<project>/milestones/<release>/`.
- Large GeoJSON artifacts are externalized from project metadata and exposed through vector-tile tilejson URLs.
- Inline cumulative/effective geometry is skipped when it would exceed `temporal_derived_geometry_max_features`.
- Completed file-backed milestones can be reused even when request-cache entries were compacted.
- Buffer layers can be generated from file-backed additions after cache cleanup.
- Tiled browser response caps no longer cap temporal artifacts; publication promotes the full request GeoJSON artifact.

For already-cleaned Casa 2026 output, the full GeoJSONL was deleted before this last fix, so that specific milestone remains capped unless rerun.
