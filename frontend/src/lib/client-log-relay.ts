import { getFastApiBaseUrl } from "@/lib/env";
import { isDevClientLogEnabled } from "@/lib/client-log-config";

const ENABLE_CLIENT_LOG_RELAY = isDevClientLogEnabled(import.meta.env);
const MAX_PAYLOAD_LENGTH = 20_000;
const TEMPORAL_REFERENCE_PREFIX = "TEMPORAL_REFERENCE_";
const TEMPORAL_ADDED_PREFIX = "TEMPORAL_ADDED_";
const TEMPORAL_OUTPUT_PREFIX = "TEMPORAL_OUTPUT_";
const TEMPORAL_ACTIVE_PREFIX = "TEMPORAL_ACTIVE_";
const TEMPORAL_VECTOR_PREFIX = "TEMPORAL_VECTOR_";
const TEMPORAL_VECTOR_TILE_PREFIX = "TEMPORAL_VECTOR_TILE_";
const TEMPORAL_GEOJSON_PREFIX = "TEMPORAL_GEOJSON_";
const TEMPORAL_BASELINE_PREFIX = "TEMPORAL_BASELINE_";
const TEMPORAL_EMPTY_BASELINE_PREFIX = "TEMPORAL_EMPTY_BASELINE_";
const TEMPORAL_CUMULATIVE_PREFIX = "TEMPORAL_CUMULATIVE_";
const TEMPORAL_RENDER_PREFIX = "TEMPORAL_RENDER_";
const TEMPORAL_STALE_PROJECT_PREFIX = "TEMPORAL_STALE_PROJECT_";
const TEMPORAL_SCREENSHOT_PREFIX = "TEMPORAL_SCREENSHOT_";
const RUN_CACHE_POLL_PREFIX = "RUN_CACHE_POLL_";
const REFERENCE_LAYER_PANEL_PREFIX = "REFERENCE_LAYER_PANEL_";
const RELAY_DEDUPE_MS = 5_000; // Suppress identical events for 5 seconds
const MAX_DEDUPE_ENTRIES = 100;

// Track recent identical events for dev-only rate limiting
const recentEventKeysRef: Map<string, number> = new Map();

function sanitizePayload(payload: Record<string, unknown>): Record<string, unknown> {
  try {
    const serialized = JSON.stringify(payload);
    if (serialized.length <= MAX_PAYLOAD_LENGTH) {
      return payload;
    }
    return {
      ...payload,
      truncated: true,
      payloadPreview: serialized.slice(0, MAX_PAYLOAD_LENGTH),
    };
  } catch {
    return {
      error: "payload_not_serializable",
    };
  }
}

function getEventSignature(event: string, payload: Record<string, unknown>): string {
  const projectId = (payload.projectId as string) ?? "";
  const releaseIdentifier = (payload.releaseIdentifier as string) ?? "";
  const switchKey = (payload.switchKey as string) ?? "";
  const reason = (payload.reason as string) ?? "";
  const layerId = (payload.layerId as string) ?? "";
  const layerKey = (payload.layerKey as string) ?? "";
  const kind = (payload.kind as string) ?? "";
  const artifactKey = (payload.artifactKey as string) ?? "";
  return `${event}:${projectId}:${releaseIdentifier}:${switchKey}:${reason}:${layerId}:${layerKey}:${kind}:${artifactKey}`;
}

function shouldRateLimitEvent(signature: string): boolean {
  const now = Date.now();
  const lastTimestamp = recentEventKeysRef.get(signature);

  if (lastTimestamp === undefined) {
    recentEventKeysRef.set(signature, now);
    // Prune old entries to keep map bounded
    if (recentEventKeysRef.size > MAX_DEDUPE_ENTRIES) {
      const oldestKey = Array.from(recentEventKeysRef.entries()).sort(([, a], [, b]) => a - b)[0];
      if (oldestKey) {
        recentEventKeysRef.delete(oldestKey[0]);
      }
    }
    return false;
  }

  const elapsed = now - lastTimestamp;
  if (elapsed < RELAY_DEDUPE_MS) {
    return true; // Rate limit: too soon
  }

  // Update timestamp for this event
  recentEventKeysRef.set(signature, now);
  return false; // Allow after debounce window
}

export function relayClientLog(event: string, payload: Record<string, unknown>): void {
  if (
    !ENABLE_CLIENT_LOG_RELAY
  ) {
    return;
  }

  console.info(event, payload);

  if (
      !event.startsWith(TEMPORAL_REFERENCE_PREFIX) &&
      !event.startsWith(TEMPORAL_ADDED_PREFIX) &&
      !event.startsWith(TEMPORAL_OUTPUT_PREFIX) &&
      !event.startsWith(TEMPORAL_ACTIVE_PREFIX) &&
      !event.startsWith(TEMPORAL_VECTOR_PREFIX) &&
      !event.startsWith(TEMPORAL_VECTOR_TILE_PREFIX) &&
      !event.startsWith(TEMPORAL_GEOJSON_PREFIX) &&
      !event.startsWith(TEMPORAL_BASELINE_PREFIX) &&
      !event.startsWith(TEMPORAL_EMPTY_BASELINE_PREFIX) &&
      !event.startsWith(TEMPORAL_CUMULATIVE_PREFIX) &&
      !event.startsWith(TEMPORAL_RENDER_PREFIX) &&
      !event.startsWith(TEMPORAL_STALE_PROJECT_PREFIX) &&
      !event.startsWith(TEMPORAL_SCREENSHOT_PREFIX) &&
      !event.startsWith(RUN_CACHE_POLL_PREFIX) &&
      !event.startsWith(REFERENCE_LAYER_PANEL_PREFIX)
  ) {
    return;
  }

  // Defensive rate limiting: suppress identical events within 5 seconds
  const signature = getEventSignature(event, payload);
  if (shouldRateLimitEvent(signature)) {
    return;
  }

  void fetch(new URL("/api/dev/client-log", getFastApiBaseUrl()).toString(), {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      event,
      payload: sanitizePayload(payload),
      timestamp: new Date().toISOString(),
      source: "frontend",
    }),
    keepalive: true,
  }).catch(() => {
    // Dev-only relay; never block UI or surface relay errors.
  });
}
