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

export interface TemporalPairProgressDetails {
  currentPairIndex: number | null;
  totalPairCount: number | null;
  pairFraction: number | null;
  pairStage: string | null;
  fromReleaseIdentifier: string | null;
  toReleaseIdentifier: string | null;
  fromReleaseDate: string | null;
  toReleaseDate: string | null;
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
  temporalPairDetails: TemporalPairProgressDetails | null;
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
    temporalPairDetails: null,
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
    temporalPairDetails: null,
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
    temporalPairDetails: null,
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
    temporalPairDetails: null,
  };
}

export function formatArchiveDateDmy(value: string | null | undefined): string | null {
  if (!value) {
    return null;
  }
  const match = value.match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (!match) {
    return null;
  }
  return `${match[3]}/${match[2]}/${match[1]}`;
}

export function buildTemporalPeriodLabel(details: TemporalPairProgressDetails | null): string {
  if (!details) {
    return "Période en cours non disponible";
  }
  const fromDate = formatArchiveDateDmy(details.fromReleaseDate);
  const toDate = formatArchiveDateDmy(details.toReleaseDate);
  if (fromDate && toDate) {
    return `Période en cours : ${fromDate} → ${toDate}`;
  }
  if (fromDate || toDate) {
    return `Période en cours : ${fromDate ?? "Date non disponible"} → ${toDate ?? "Date non disponible"}`;
  }
  if (details.fromReleaseIdentifier && details.toReleaseIdentifier) {
    return `Période en cours : ${details.fromReleaseIdentifier} → ${details.toReleaseIdentifier}`;
  }
  return "Période en cours non disponible";
}

export function temporalPairProgressPercent(details: TemporalPairProgressDetails | null): number | null {
  if (!details || details.pairFraction === null) {
    return null;
  }
  return clampPercent(details.pairFraction * 100);
}

export function temporalGlobalProgressPercent(details: TemporalPairProgressDetails | null): number | null {
  if (
    !details ||
    details.currentPairIndex === null ||
    details.totalPairCount === null ||
    details.totalPairCount <= 0 ||
    details.pairFraction === null
  ) {
    return null;
  }
  return clampPercent(((details.currentPairIndex - 1 + details.pairFraction) / details.totalPairCount) * 100);
}

export function friendlyTemporalStageLabel(stage: string | null | undefined): string {
  const normalized = (stage ?? "").toLowerCase();
  if (/saving|persist|publication|final|complete|completed/.test(normalized)) {
    return "Finalisation";
  }
  if (/preflight|validat|metadata|availability|resolving|checking|starting|prépar/.test(normalized)) {
    return "Préparation des images";
  }
  if (/download|imagery|mosaic|wayback|reference|fetch/.test(normalized)) {
    return "Téléchargement des images";
  }
  if (/inference|bandon|detection|tiled|change detection|analyse/.test(normalized)) {
    return "Analyse des changements";
  }
  if (/vector|buffer|post-process|postprocess|generation|génération|result|exporting artifacts/.test(normalized)) {
    return "Génération des résultats";
  }
  return "Traitement en cours";
}

function isCompletedStatus(value: string | null | undefined): boolean {
  if (!value) {
    return false;
  }
  return ["complete", "completed", "success", "succeeded", "done", "process_completed"].includes(value.toLowerCase());
}

function isFailureStatus(value: string | null | undefined): boolean {
  if (!value) {
    return false;
  }
  return ["error", "failed", "failure", "cancelled", "canceled", "cancel_requested"].includes(value.toLowerCase());
}

export function shouldShowExecutionProgressPanel(progress: RunProgressState): boolean {
  const detail = progress.detail.trim();
  const stageLabel = progress.stageLabel.trim();
  const hasErrorMessage =
    detail.length > 0 &&
    detail !== DEFAULT_IDLE_STATUS &&
    detail !== "Artifacts are ready." &&
    /error|fail|failed|cancel|cancelled|canceled/i.test(detail);
  const hasFailedStage = /error|fail|failed|cancel|cancelled|canceled/i.test(stageLabel);

  if (progress.phase === "error" || isFailureStatus(progress.rawEvent) || hasErrorMessage || hasFailedStage) {
    return true;
  }

  if (progress.phase === "queued" || progress.phase === "running") {
    return true;
  }

  if (progress.phase === "complete" || isCompletedStatus(progress.rawEvent) || isCompletedStatus(stageLabel)) {
    return false;
  }

  if (progress.percent < 100 && progress.phase !== "idle") {
    return true;
  }

  return false;
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
