export type RunPhase = "idle" | "queued" | "running" | "complete" | "error";

export interface WaybackTileProgressDetails {
  releaseIdentifier: string | null;
  preferredZoom: number | null;
  effectiveZoom: number | null;
  fallbackApplied: boolean;
  processedTileCount: number;
  totalTileCount: number;
  cacheHitCount: number;
  downloadedTileCount: number;
  missingTileCount: number;
  failedTileCount: number;
  retryCount: number;
  throttleCount: number;
  timeoutCount: number;
  tileRatePerSec: number | null;
  etaSeconds: number | null;
}

export interface RunProgressState {
  phase: RunPhase;
  percent: number;
  stageLabel: string;
  detail: string;
  queuePosition: number | null;
  etaSeconds: number | null;
  eventId: string | null;
  rawEvent: string | null;
  updatedAt: number | null;
  tileDetails: WaybackTileProgressDetails | null;
}

export interface PipelineStage {
  key: string;
  label: string;
  minPercent: number;
  translationKey: string;
}

type TranslateFn = (key: string) => string;

// Static labels for backward compatibility, but translationKey should be used for display
export const PIPELINE_STAGES: PipelineStage[] = [
  { key: "queue", label: "Queued", minPercent: 0, translationKey: "pipeline.queued" },
  { key: "metadata", label: "Metadata", minPercent: 5, translationKey: "pipeline.metadata" },
  { key: "preflight", label: "Tile availability", minPercent: 12, translationKey: "pipeline.preflight" },
  { key: "imagery", label: "Download", minPercent: 18, translationKey: "pipeline.imagery" },
  { key: "alignment", label: "Alignment", minPercent: 35, translationKey: "pipeline.alignment" },
  { key: "segmentation", label: "Inference", minPercent: 45, translationKey: "pipeline.segmentation" },
  { key: "postprocess", label: "Post-processing", minPercent: 72, translationKey: "pipeline.postprocess" },
  { key: "vectorize", label: "Vectorization", minPercent: 82, translationKey: "pipeline.vectorize" },
  { key: "export", label: "Export", minPercent: 92, translationKey: "pipeline.export" },
];

const DEFAULT_IDLE_STATUS = "Draw an AOI and validate the request.";

function getString(record: Record<string, unknown>, ...keys: string[]): string | null {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string" && value.length > 0) {
      return value;
    }
  }
  return null;
}

function getNumber(record: Record<string, unknown>, ...keys: string[]): number | null {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "number" && Number.isFinite(value)) {
      return value;
    }
  }
  return null;
}

function clampPercent(value: number): number {
  return Math.max(0, Math.min(100, value));
}

export function createIdleRunProgress(): RunProgressState {
  return {
    phase: "idle",
    percent: 0,
    stageLabel: "Idle",
    detail: DEFAULT_IDLE_STATUS,
    queuePosition: null,
    etaSeconds: null,
    eventId: null,
    rawEvent: null,
    updatedAt: null,
    tileDetails: null,
  };
}

export function createActiveRunProgress(): RunProgressState {
  return {
    phase: "queued",
    percent: 0,
    stageLabel: "Queued",
    detail: "Submitting request to the backend queue.",
    queuePosition: null,
    etaSeconds: null,
    eventId: null,
    rawEvent: null,
    updatedAt: Date.now(),
    tileDetails: null,
  };
}

export function createCompletedRunProgress(): RunProgressState {
  return {
    phase: "complete",
    percent: 100,
    stageLabel: "Completed",
    detail: "Artifacts are ready.",
    queuePosition: null,
    etaSeconds: null,
    eventId: null,
    rawEvent: "process_completed",
    updatedAt: Date.now(),
    tileDetails: null,
  };
}

export function createErrorRunProgress(message: string): RunProgressState {
  return {
    phase: "error",
    percent: 100,
    stageLabel: "Run failed",
    detail: message,
    queuePosition: null,
    etaSeconds: null,
    eventId: null,
    rawEvent: "error",
    updatedAt: Date.now(),
    tileDetails: null,
  };
}

export function updateRunProgressFromEvent(
  previous: RunProgressState,
  event: Record<string, unknown>,
): RunProgressState {
  const next = { ...previous };
  next.updatedAt = Date.now();
  next.eventId = getString(event, "event_id") ?? next.eventId;

  const queuePosition = getNumber(event, "queue_position", "position", "rank");
  const etaSeconds = getNumber(event, "rank_eta", "eta");
  if (queuePosition !== null) {
    next.queuePosition = queuePosition > 0 ? queuePosition : null;
  }
  if (etaSeconds !== null) {
    next.etaSeconds = etaSeconds > 0 && next.queuePosition !== null ? etaSeconds : null;
  }

  const eventName = getString(event, "original_msg", "msg", "message");
  const stage = getString(event, "stage");
  if (eventName || stage) {
    next.rawEvent = eventName ?? stage;
  }

  const progressItems = Array.isArray(event.progress_data)
    ? event.progress_data.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object")
    : [];
  const latestProgress = progressItems.at(-1) ?? null;
  const progressValue = latestProgress ? getNumber(latestProgress, "progress") : null;
  const progressDesc = latestProgress ? getString(latestProgress, "desc") : null;

  if (progressValue !== null) {
    next.percent = clampPercent(progressValue * 100);
  }
  if (progressDesc) {
    next.stageLabel = progressDesc;
    next.detail = progressDesc;
  }

  const hasRealProgress = progressValue !== null || Boolean(progressDesc) || next.percent > 0;

  switch (eventName) {
    case "estimation":
      next.phase = "queued";
      next.stageLabel = "Queued";
      next.detail = "Waiting for a worker slot.";
      next.percent = 0;
      next.queuePosition = next.queuePosition !== null && next.queuePosition > 0 ? next.queuePosition : null;
      next.etaSeconds = next.queuePosition !== null ? next.etaSeconds : null;
      break;
    case "process_starts":
      next.phase = "running";
      next.stageLabel = "Starting run";
      next.detail = "Backend worker started processing your request.";
      next.percent = Math.max(next.percent, 1);
      next.queuePosition = null;
      next.etaSeconds = null;
      break;
    case "progress":
      next.phase = "running";
      if (!progressDesc) {
        next.stageLabel = "Processing";
        next.detail = "The backend is advancing through the pipeline.";
      }
      next.queuePosition = null;
      next.etaSeconds = null;
      break;
    case "heartbeat":
      next.phase = hasRealProgress ? "running" : next.phase === "idle" ? "running" : next.phase;
      next.stageLabel = previous.stageLabel || "Processing";
      next.detail = previous.detail || "Waiting for the next backend update.";
      break;
    case "process_completed":
      next.phase = "complete";
      next.stageLabel = "Completed";
      next.detail = "Artifacts are ready.";
      next.percent = 100;
      next.queuePosition = null;
      next.etaSeconds = null;
      break;
    default:
      break;
  }

  if (hasRealProgress && next.phase === "queued" && eventName !== "estimation") {
    next.phase = "running";
    next.queuePosition = null;
    next.etaSeconds = null;
  }

  if (stage === "error") {
    next.phase = "error";
    next.stageLabel = "Run failed";
    next.detail = getString(event, "message") ?? next.detail;
  } else if (stage === "complete" || stage === "succeeded") {
    next.phase = "complete";
    next.stageLabel = "Completed";
    next.detail = "Artifacts are ready.";
    next.percent = 100;
    next.queuePosition = null;
    next.etaSeconds = null;
  }

  return next;
}

function translateStageLabel(label: string, t: TranslateFn): string {
  switch (label) {
    case "Queued":
      return t("status.waiting");
    case "Starting run":
    case "Processing":
      return t("status.active");
    case "Completed":
      return t("status.completed");
    case "Run failed":
      return t("status.stage_failed");
    case "Idle":
      return t("status.idle");
    default:
      return label;
  }
}

export function formatRunStatus(progress: RunProgressState, t: TranslateFn = (value) => value): string {
  const fragments: string[] = [translateStageLabel(progress.stageLabel, t)];
  if (progress.queuePosition !== null && progress.queuePosition > 0 && progress.phase === "queued") {
    fragments.push(`${t("status.queue")} ${progress.queuePosition}`);
  }
  if (
    progress.queuePosition !== null &&
    progress.queuePosition > 0 &&
    progress.etaSeconds !== null &&
    progress.etaSeconds > 0 &&
    progress.phase === "queued"
  ) {
    fragments.push(`${t("progress.eta")} ${Math.round(progress.etaSeconds)}s`);
  }
  return fragments.join(" | ");
}

export function getStageState(progress: RunProgressState, stage: PipelineStage): "complete" | "current" | "pending" {
  if (progress.percent >= stage.minPercent + 10 || (progress.phase === "complete" && progress.percent >= stage.minPercent)) {
    return "complete";
  }
  if (progress.percent >= stage.minPercent) {
    return "current";
  }
  return "pending";
}
