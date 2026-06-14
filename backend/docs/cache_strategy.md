# Cache Strategy Notes

The current runtime keeps heavy generated outputs on disk and stores durable metadata in PostgreSQL/PostGIS when enabled.
Future cache keys should stay deterministic and include the minimum inputs needed to prove reuse is safe.

- Imagery cache key: source, date or release identifier, zoom/resolution, tile identifier or AOI hash.
- Inference cache key: model backend, model version/checkpoint, image hash, prompt/threshold parameters, and tiling parameters.
- Buffer cache key: project id, milestone/release identifier, source geometry hash, buffer distance, and geometry algorithm version.

Do not use Redis as durable cache or job state. Redis remains the Celery broker/result backend for local async execution.
