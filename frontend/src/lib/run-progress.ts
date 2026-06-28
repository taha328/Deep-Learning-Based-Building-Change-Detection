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

export type TemporalTimelineStageId =
  | "queued"
  | "metadata"
  | "tile_availability"
  | "download"
  | "alignment"
  | "inference"
  | "postprocessing"
  | "vectorization"
  | "publication"
  | "exports"
  | "metadata_write"
  | "cleanup"
  | "done";

export type TemporalTimelineStageState = "complete" | "current" | "pending" | "failed";

export interface TemporalTimelineStageDefinition {
  id: TemporalTimelineStageId;
  label: string;
  description?: string;
}

export interface TemporalTimelineStage extends TemporalTimelineStageDefinition {
  state: TemporalTimelineStageState;
}

export interface TemporalProgressTimeline {
  stages: TemporalTimelineStage[];
  activeStageId: TemporalTimelineStageId;
  summaryLabel: string;
  summaryDetail: string;
  readinessLabel: string;
  readinessDetail: string;
  currentStageNote: string;
  pairLabel: string;
  pairStepLabel: string;
  globalPercent: number | null;
  pairPercent: number | null;
  analysisComplete: boolean;
  finalizing: boolean;
  ready: boolean;
  failed: boolean;
  cancelled: boolean;
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

export const TEMPORAL_VERTICAL_TIMELINE_STAGES: TemporalTimelineStageDefinition[] = [
  {
    id: "queued",
    label: "En attente",
  },
  {
    id: "metadata",
    label: "Préparation du projet",
  },
  {
    id: "tile_availability",
    label: "Vérification des tuiles",
  },
  {
    id: "download",
    label: "Téléchargement des images",
  },
  {
    id: "alignment",
    label: "Alignement",
  },
  {
    id: "inference",
    label: "Inférence",
    description: "Détection des changements bâtimentaires.",
  },
  {
    id: "postprocessing",
    label: "Post-traitement",
  },
  {
    id: "vectorization",
    label: "Vectorisation",
  },
  {
    id: "publication",
    label: "Publication des couches",
  },
  {
    id: "exports",
    label: "Génération des exports",
  },
  {
    id: "metadata_write",
    label: "Écriture des métadonnées",
  },
  {
    id: "cleanup",
    label: "Nettoyage",
  },
  {
    id: "done",
    label: "Terminé",
  },
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

function timelineStageIndex(stageId: TemporalTimelineStageId): number {
  return TEMPORAL_VERTICAL_TIMELINE_STAGES.findIndex((stage) => stage.id === stageId);
}

function normalizedProgressText(progress: RunProgressState): string {
  return [
    progress.stageLabel,
    progress.detail,
    progress.rawEvent,
    progress.temporalPairDetails?.pairStage,
  ]
    .filter((value): value is string => Boolean(value))
    .join(" ")
    .toLowerCase();
}

function isCompletedProgress(progress: RunProgressState): boolean {
  return progress.phase === "complete" || isCompletedStatus(progress.rawEvent) || isCompletedStatus(progress.stageLabel);
}

function isCancelledProgress(progress: RunProgressState): boolean {
  const text = normalizedProgressText(progress);
  return progress.rawEvent === "cancel_requested" || /cancel|cancelled|canceled|annul/.test(text);
}

function activeTemporalStageFromProgress(progress: RunProgressState): TemporalTimelineStageId {
  const text = normalizedProgressText(progress);

  if (isCompletedProgress(progress)) {
    return "done";
  }
  if (progress.phase === "queued") {
    return "queued";
  }
  if (/cleanup|nettoyage/.test(text)) {
    return "cleanup";
  }
  if (/persist|metadata_write|metadata write|compact job metadata|manifest|summary|database|métadonnée/.test(text)) {
    return "metadata_write";
  }
  if (/export|bundle|geopackage|qgis|report/.test(text)) {
    return "exports";
  }
  if (/saving_artifacts|saving temporal outputs|artifact|publication|publish|layer|couche|building_buffers|buffer/.test(text)) {
    return "publication";
  }
  if (/vector|vectorizing|vectorization/.test(text)) {
    return "vectorization";
  }
  if (/postprocess|post-process|post_traitement|filter|clean|consolid/.test(text)) {
    return "postprocessing";
  }
  if (/alignment|align/.test(text)) {
    return "alignment";
  }
  if (/download|imagery|mosaic|wayback|reference|fetch/.test(text)) {
    return "download";
  }
  if (/tile_availability|tile availability|checking tile|vérification des tuiles/.test(text)) {
    return "tile_availability";
  }
  if (/starting|metadata|preflight|validat|prepar|prépar|project state/.test(text)) {
    return "metadata";
  }
  if (/inference|bandon|detection|tiled|change detection|analyse/.test(text)) {
    return "inference";
  }

  const pairPercent = temporalPairProgressPercent(progress.temporalPairDetails);
  if (pairPercent !== null && pairPercent >= 99.5 && progress.phase !== "complete") {
    return "publication";
  }
  if (progress.temporalPairDetails) {
    return "inference";
  }
  return progress.phase === "idle" ? "queued" : "metadata";
}

function temporalStageNote(stageId: TemporalTimelineStageId, progress: RunProgressState, analysisComplete: boolean): string {
  if (isCompletedProgress(progress)) {
    return "";
  }
  if (progress.phase === "error") {
    return "Une erreur est survenue pendant le traitement.";
  }
  if (isCancelledProgress(progress)) {
    return "";
  }
  if (analysisComplete && stageId !== "done") {
    return "";
  }
  switch (stageId) {
    case "inference":
      return "Analyse de la période";
    default:
      return "";
  }
}

export function buildTemporalProgressTimeline(progress: RunProgressState): TemporalProgressTimeline {
  const pairPercent = temporalPairProgressPercent(progress.temporalPairDetails);
  const globalPercent = temporalGlobalProgressPercent(progress.temporalPairDetails);
  const analysisComplete = pairPercent !== null && pairPercent >= 99.5;
  const failed = progress.phase === "error" || isFailureStatus(progress.rawEvent) || isFailureStatus(progress.stageLabel);
  const cancelled = isCancelledProgress(progress);
  const ready = isCompletedProgress(progress);
  const activeStageId = activeTemporalStageFromProgress(progress);
  const activeIndex = timelineStageIndex(activeStageId);
  const finalizing = !ready && !failed && activeIndex >= timelineStageIndex("publication");
  const pairStepLabel =
    progress.temporalPairDetails?.currentPairIndex !== null &&
    progress.temporalPairDetails?.currentPairIndex !== undefined &&
    progress.temporalPairDetails?.totalPairCount !== null &&
    progress.temporalPairDetails?.totalPairCount !== undefined
      ? `Période ${progress.temporalPairDetails.currentPairIndex}/${progress.temporalPairDetails.totalPairCount}`
      : "Période en cours";
  const currentStageNote = temporalStageNote(activeStageId, progress, analysisComplete);
  const stages = TEMPORAL_VERTICAL_TIMELINE_STAGES.map((stage, index): TemporalTimelineStage => {
    let state: TemporalTimelineStageState = "pending";
    if (ready || index < activeIndex) {
      state = "complete";
    } else if (index === activeIndex) {
      state = failed || cancelled ? "failed" : "current";
    }
    return { ...stage, state };
  });

  const summaryLabel = ready
    ? "Résultats prêts"
    : failed
      ? "Traitement interrompu"
      : finalizing
        ? "Finalisation du projet"
        : analysisComplete
          ? "Analyse terminée"
          : "Analyse en cours";
  const summaryDetail = ready
    ? "Les couches et résultats du projet sont disponibles."
    : failed
      ? progress.detail || "Une erreur est survenue pendant le traitement."
      : finalizing || analysisComplete
        ? "Analyse terminée — finalisation en cours."
        : progress.phase === "queued"
          ? "Le projet attend un worker disponible."
          : "Le backend analyse les périodes sélectionnées.";
  const readinessLabel = ready ? "Résultats prêts." : failed ? "Résultats non disponibles." : "Résultats pas encore prêts.";
  const readinessDetail = ready
    ? "Les couches publiées peuvent être consultées sur la carte."
    : failed
      ? progress.detail || "Le traitement doit être relancé après correction."
      : "Les couches seront disponibles après la publication finale.";

  return {
    stages,
    activeStageId,
    summaryLabel,
    summaryDetail,
    readinessLabel,
    readinessDetail,
    currentStageNote,
    pairLabel: buildTemporalPeriodLabel(progress.temporalPairDetails),
    pairStepLabel,
    globalPercent,
    pairPercent,
    analysisComplete,
    finalizing,
    ready,
    failed,
    cancelled,
  };
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
