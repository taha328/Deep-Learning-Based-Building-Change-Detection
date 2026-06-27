# Fix Design

## Goals

1. Recover Casa 2 from request `43a37e757f6111e5929acc40` without rerunning inference.
2. Prevent future jobs from remaining indefinitely `running/saving_artifacts` after request-level success.
3. Preserve existing behavior for small runs that can generate buffer layers quickly.
4. Make large-run finalization durable even when optional buffer layers are unavailable.

## Design

### 1. Treat Buffer Regeneration As Optional For Large Outputs

When a cached response or completed tiled request has no `buffer_layers_geojson`, the finalizer may regenerate buffers from additions only if the feature count is below a bounded threshold.

For larger feature counts, finalization skips buffer regeneration, records a milestone warning, and still persists:

- request hash fields
- additions GeoJSON
- cumulative/effective footprint
- metrics
- `additions.geojson`
- compact metadata
- project summary
- DB milestone/metric/artifact rows in Postgres mode

This prevents a successful request from being trapped behind optional expensive GEOS work.

### 2. Preserve Small-Run Buffer Behavior

Existing small temporal runs still generate and publish `building_change_buffer_10m.geojson`, `15m`, and `20m` artifacts. The threshold only prevents unbounded large-run regeneration when the request did not already produce buffers.

### 3. Validate Finalized Project State Before Job Completion

Before a temporal job is marked completed, the Celery task validates that the response project has at least one completed milestone with non-null metrics and at least one registered artifact. If validation fails, the job is marked failed with an actionable finalization error instead of being marked completed with empty project state.

### 4. Recovery Uses Existing Publication Service

Casa 2 recovery uses `publish_completed_tiled_request(...)` after the fix. That path remains idempotent:

- it rewrites the same milestone artifacts under the temporal project directory
- artifact metadata is replaced by key/path rather than appended blindly
- Postgres project persistence rewrites the project, milestone, metric, geometry, and artifact rows from the final project model

### 5. Job/Run Persistence

After successful recovery, a temporal run record is persisted and the original job is marked completed only after the compact project payload proves milestone-level metrics/artifacts are present.

## Non-Goals

- No frontend workaround.
- No Casa 2-specific production branching.
- No deletion of request outputs.
- No rerun of the 6090-tile inference.
- No Redis flush.
