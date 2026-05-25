import { z } from "zod";

import {
  backendAvailabilitySchema,
  jobResponseSchema,
  jobStartResponseSchema,
  releasesResponseSchema,
  referenceLayerPreflightSchema,
  referenceLayerSchema,
  runResponseSchema,
  temporalProjectRunResponseSchema,
  temporalProjectExportBundleSchema,
  temporalProjectSaveResponseSchema,
  temporalProjectSchema,
  temporalProjectSummarySchema,
  temporalProjectValidationResponseSchema,
  validationResponseSchema,
  type JobResponse,
  type JobStartResponse,
  type BackendAvailability,
  type ReferenceLayer,
  type ReferenceLayerPreflight,
  type ReferenceLayerScope,
  type ReferenceLayerStrategy,
  type RunResponse,
  type TemporalProject,
  type TemporalProjectRunResponse,
  type TemporalProjectExportBundle,
  type TemporalProjectSaveResponse,
  type TemporalProjectSummary,
  type TemporalProjectValidationResponse,
  type ValidationRequest,
  type ValidationResponse,
} from "@/api/contracts";
import { apiFetch, ApiClientError } from "@/api/http";
import {
  createActiveRunProgress,
  createCompletedRunProgress,
  createErrorRunProgress,
  formatRunStatus,
  type WaybackTileProgressDetails,
  type RunProgressState,
} from "@/lib/run-progress";
import { relayClientLog } from "@/lib/client-log-relay";

const activeCachedRunRequests = new Map<string, Promise<RunResponse>>();

function createPendingRunProgress(): RunProgressState {
  return {
    phase: "running",
    percent: 15,
    stageLabel: "Processing",
    detail: "Processing request on the backend.",
    queuePosition: null,
    etaSeconds: null,
    eventId: null,
    rawEvent: null,
    updatedAt: Date.now(),
    tileDetails: null,
  };
}

function numberDetail(record: Record<string, unknown>, key: string): number | null {
  const value = record[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function parseWaybackTileProgress(details: unknown): WaybackTileProgressDetails | null {
  if (!details || typeof details !== "object") {
    return null;
  }
  const record = details as Record<string, unknown>;
  if (!("selected_tile_count" in record) && !("total_tile_count" in record)) {
    return null;
  }
  return {
    releaseIdentifier: typeof record.release_identifier === "string" ? record.release_identifier : null,
    preferredZoom: numberDetail(record, "preferred_zoom"),
    effectiveZoom: numberDetail(record, "effective_zoom"),
    fallbackApplied: record.fallback_applied === true,
    processedTileCount: numberDetail(record, "processed_tile_count") ?? 0,
    totalTileCount: numberDetail(record, "total_tile_count") ?? numberDetail(record, "selected_tile_count") ?? 0,
    cacheHitCount: numberDetail(record, "cache_hit_count") ?? 0,
    downloadedTileCount: numberDetail(record, "downloaded_tile_count") ?? 0,
    missingTileCount: numberDetail(record, "missing_tile_count") ?? 0,
    failedTileCount: numberDetail(record, "failed_tile_count") ?? 0,
    retryCount: numberDetail(record, "retry_count") ?? 0,
    throttleCount: numberDetail(record, "throttle_count") ?? 0,
    timeoutCount: numberDetail(record, "timeout_count") ?? 0,
    tileRatePerSec: numberDetail(record, "tile_rate_per_sec"),
    etaSeconds: numberDetail(record, "eta_seconds"),
  };
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => {
    globalThis.setTimeout(resolve, ms);
  });
}

const JOB_POLL_INTERVAL_MS = 1500;
const QUEUED_WORKER_WARNING_MS = 30_000;
const activeJobPolls = new Map<string, Promise<JobResponse>>();

function isJobUnavailableError(error: unknown): boolean {
  return (
    error instanceof ApiClientError &&
    (error.status === 503 || error.code === "redis_unavailable" || error.code === "jobs_disabled" || error.code === "celery_unavailable")
  );
}

function isTerminalJobStatus(status: JobResponse["status"]): boolean {
  return status === "completed" || status === "failed" || status === "cancelled";
}

function jobToProgress(job: JobResponse): RunProgressState {
  const phase =
    job.status === "completed"
      ? "complete"
      : job.status === "failed" || job.status === "cancelled"
        ? "error"
        : job.status === "queued" || job.status === "cancel_requested"
          ? "queued"
          : "running";
  const detail =
    job.message ??
    (job.status === "queued"
      ? "Queued for execution."
      : job.status === "running"
        ? "Backend worker started processing your request."
        : job.status === "completed"
          ? "Artifacts are ready."
          : job.error_message ?? "The backend reported a failure.");

  return {
    phase,
    percent: Math.max(0, Math.min(100, job.progress ?? (job.status === "completed" ? 100 : phase === "queued" ? 0 : 5))),
    stageLabel: job.stage ?? (phase === "complete" ? "Completed" : phase === "error" ? "Run failed" : phase === "queued" ? "Queued" : "Processing"),
    detail,
    queuePosition: null,
    etaSeconds: null,
    eventId: job.job_id,
    rawEvent: job.status,
    updatedAt: Date.now(),
    tileDetails: parseWaybackTileProgress(job.progress_details),
  };
}

function compactRawResult(job: JobResponse): Record<string, unknown> {
  return job.raw_result ?? {};
}

function compactString(job: JobResponse, key: string): string | null {
  const value = compactRawResult(job)[key];
  return typeof value === "string" && value.length > 0 ? value : null;
}

async function resolveDetectionJobResponse(job: JobResponse): Promise<RunResponse> {
  const requestHash = compactString(job, "request_hash") ?? job.request_hash ?? job.result_run_id;
  if (requestHash) {
    return getCachedRunResponse(requestHash);
  }
  return runResponseSchema.parse(job.raw_result ?? null);
}

async function resolveTemporalJobResponse(job: JobResponse): Promise<TemporalProjectRunResponse> {
  const projectId = compactString(job, "project_id") ?? job.project_id;
  if (projectId) {
    const project = await getTemporalProject(projectId);
    return temporalProjectRunResponseSchema.parse({
      success: job.status === "completed",
      error_message: job.error_message ?? compactString(job, "error_message"),
      project,
    });
  }
  return temporalProjectRunResponseSchema.parse(job.raw_result ?? null);
}

async function getJob(jobId: string): Promise<JobResponse> {
  const result = await apiFetch<unknown>(`/api/jobs/${encodeURIComponent(jobId)}`);
  return jobResponseSchema.parse(result);
}

async function startDetectionJob(request: ValidationRequest): Promise<JobStartResponse> {
  const result = await apiFetch<unknown>("/api/jobs/detection", {
    method: "POST",
    body: JSON.stringify(request),
  });
  return jobStartResponseSchema.parse(result);
}

async function startTemporalProjectJob(projectId: string): Promise<JobStartResponse> {
  const result = await apiFetch<unknown>(`/api/jobs/temporal-projects/${encodeURIComponent(projectId)}`, {
    method: "POST",
  });
  return jobStartResponseSchema.parse(result);
}

async function pollJobUntilComplete(
  jobId: string,
  onStatus?: (message: string) => void,
  onProgress?: (progress: RunProgressState) => void,
): Promise<JobResponse> {
  const existingPoll = activeJobPolls.get(jobId);
  if (existingPoll) {
    return existingPoll;
  }

  const poll = pollJobUntilCompleteOnce(jobId, onStatus, onProgress);
  activeJobPolls.set(jobId, poll);
  try {
    return await poll;
  } finally {
    activeJobPolls.delete(jobId);
  }
}

async function pollJobUntilCompleteOnce(
  jobId: string,
  onStatus?: (message: string) => void,
  onProgress?: (progress: RunProgressState) => void,
): Promise<JobResponse> {
  const initialProgress = createActiveRunProgress();
  onProgress?.(initialProgress);
  onStatus?.(formatRunStatus(initialProgress));
  const queuedStartedAt = Date.now();
  let showedQueuedWarning = false;

  for (;;) {
    const job = await getJob(jobId);
    const nextProgress = jobToProgress(job);
    if (job.status === "queued" && !showedQueuedWarning && Date.now() - queuedStartedAt >= QUEUED_WORKER_WARNING_MS) {
      nextProgress.detail = "Job is still queued. Check that the Celery worker is running.";
      nextProgress.stageLabel = "Waiting for worker";
      showedQueuedWarning = true;
    }
    onProgress?.(nextProgress);
    onStatus?.(formatRunStatus(nextProgress));

    if (isTerminalJobStatus(job.status)) {
      return job;
    }

    await sleep(JOB_POLL_INTERVAL_MS);
  }
}

export async function listReleases() {
  const result = await apiFetch<unknown>("/api/releases");
  return releasesResponseSchema.parse(result).releases;
}

export async function probeBackends(): Promise<BackendAvailability[]> {
  const result = await apiFetch<unknown>("/api/backends");
  return z.array(backendAvailabilitySchema).parse(result);
}

export async function validateRequest(request: ValidationRequest): Promise<ValidationResponse> {
  const result = await apiFetch<unknown>("/api/detection/validate", {
    method: "POST",
    body: JSON.stringify(request),
  });
  return validationResponseSchema.parse(result);
}

export async function runDetection(
  request: ValidationRequest,
  onStatus: (message: string) => void,
  onProgress: (progress: RunProgressState) => void,
): Promise<RunResponse> {
  let progress = createActiveRunProgress();
  onProgress(progress);
  onStatus(formatRunStatus(progress));

  let startResponse: JobStartResponse | null = null;
  try {
    startResponse = await startDetectionJob(request);
  } catch (error) {
    if (isJobUnavailableError(error)) {
      try {
        progress = createPendingRunProgress();
        onProgress(progress);
        onStatus(formatRunStatus(progress));
        const result = await apiFetch<unknown>("/api/detection/run", {
          method: "POST",
          body: JSON.stringify(request),
        });
        const response = runResponseSchema.parse(result);
        if (response.success === false) {
          const message = response.error_message ?? "The backend reported an unsuccessful run.";
          const errorProgress = createErrorRunProgress(message);
          onProgress(errorProgress);
          onStatus(formatRunStatus(errorProgress));
          throw new Error(message);
        }

        const completed = createCompletedRunProgress();
        onProgress(completed);
        onStatus(formatRunStatus(completed));
        return response;
      } catch (fallbackError) {
        const message =
          fallbackError instanceof ApiClientError
            ? fallbackError.message
            : fallbackError instanceof Error
              ? fallbackError.message
              : "The backend failed to complete the run.";
        const errorProgress = createErrorRunProgress(message);
        onProgress(errorProgress);
        onStatus(formatRunStatus(errorProgress));
        throw fallbackError instanceof Error ? fallbackError : new Error(message);
      }
    }

    const message =
      error instanceof ApiClientError
        ? error.message
        : error instanceof Error
          ? error.message
          : "The backend failed to queue the run.";
    const errorProgress = createErrorRunProgress(message);
    onProgress(errorProgress);
    onStatus(formatRunStatus(errorProgress));
    throw error instanceof Error ? error : new Error(message);
  }

  if (!startResponse) {
    throw new Error("The backend failed to queue the run.");
  }

  try {
    progress = createPendingRunProgress();
    onProgress(progress);
    onStatus(formatRunStatus(progress));

    const job = await pollJobUntilComplete(startResponse.job_id, onStatus, onProgress);
    const response = await resolveDetectionJobResponse(job);
    if (response.success === false) {
      const message = response.error_message ?? job.error_message ?? "The backend reported an unsuccessful run.";
      const errorProgress = createErrorRunProgress(message);
      onProgress(errorProgress);
      onStatus(formatRunStatus(errorProgress));
      throw new Error(message);
    }

    const completed = createCompletedRunProgress();
    onProgress(completed);
    onStatus(formatRunStatus(completed));
    return response;
  } catch (error) {
    const message =
      error instanceof ApiClientError
        ? error.message
        : error instanceof Error
          ? error.message
          : "The backend failed to complete the run.";
    const errorProgress = createErrorRunProgress(message);
    onProgress(errorProgress);
    onStatus(formatRunStatus(errorProgress));
    throw error instanceof Error ? error : new Error(message);
  }
}

export async function listTemporalProjects(options?: { includeCachedRuns?: boolean }): Promise<TemporalProjectSummary[]> {
  const search = new URLSearchParams();
  if (options?.includeCachedRuns) {
    search.set("include_cached_runs", "true");
  }
  const suffix = search.toString() ? `?${search.toString()}` : "";
  const result = await apiFetch<unknown>(`/api/temporal-projects${suffix}`);
  return z.array(temporalProjectSummarySchema).parse(result);
}

export async function getTemporalProject(projectId: string): Promise<TemporalProject> {
  const result = await apiFetch<unknown>(`/api/temporal-projects/${encodeURIComponent(projectId)}`);
  return temporalProjectSchema.parse(result);
}

export async function getTemporalMilestoneArtifact(
  projectId: string,
  releaseIdentifier: string,
  artifactKey: string,
  options?: { signal?: AbortSignal },
): Promise<Record<string, unknown>> {
  const result = await apiFetch<unknown>(
    `/api/temporal-projects/${encodeURIComponent(projectId)}/milestones/${encodeURIComponent(releaseIdentifier)}/artifacts/${encodeURIComponent(artifactKey)}`,
    { signal: options?.signal },
  );
  return z.record(z.any()).parse(result);
}

export async function getCachedRunResponse(requestHash: string): Promise<RunResponse> {
  const inFlight = activeCachedRunRequests.get(requestHash);
  if (inFlight) {
    relayClientLog("RUN_CACHE_POLL_DEDUPED", { requestHash });
    return inFlight;
  }
  relayClientLog("RUN_CACHE_POLL_START", { requestHash });
  const request = apiFetch<unknown>(`/api/cache/runs/${encodeURIComponent(requestHash)}`)
    .then((result) => {
      const parsed = runResponseSchema.parse(result);
      relayClientLog("RUN_CACHE_POLL_STOPPED_COMPLETED", { requestHash, success: parsed.success });
      return parsed;
    })
    .finally(() => {
      activeCachedRunRequests.delete(requestHash);
    });
  activeCachedRunRequests.set(requestHash, request);
  return request;
}

export async function createRunExportBundle(runId: string): Promise<string> {
  const result = await apiFetch<{ path: string }>(`/api/files/runs/${encodeURIComponent(runId)}/export-bundle`, {
    method: "POST",
  });
  return result.path;
}

export async function listJobs(options?: { limit?: number; status?: string; jobKind?: string }): Promise<JobResponse[]> {
  const search = new URLSearchParams();
  if (options?.limit !== undefined) {
    search.set("limit", String(options.limit));
  }
  if (options?.status) {
    search.set("status", options.status);
  }
  if (options?.jobKind) {
    search.set("job_kind", options.jobKind);
  }
  const suffix = search.toString() ? `?${search.toString()}` : "";
  const result = await apiFetch<unknown>(`/api/jobs${suffix}`);
  return z.array(jobResponseSchema).parse(result);
}

export async function cancelJob(jobId: string): Promise<JobResponse> {
  const result = await apiFetch<unknown>(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, {
    method: "POST",
  });
  return jobResponseSchema.parse(result);
}

export async function saveTemporalProject(project: TemporalProject): Promise<TemporalProjectSaveResponse> {
  const { has_reference_layers: _hasReferenceLayers, reference_layer_count: _referenceLayerCount, ...persistedProject } = project;
  const result = await apiFetch<unknown>("/api/temporal-projects", {
    method: "POST",
    body: JSON.stringify({ project: persistedProject }),
  });
  return temporalProjectSaveResponseSchema.parse(result);
}

export async function validateTemporalProject(project: TemporalProject): Promise<TemporalProjectValidationResponse> {
  const { has_reference_layers: _hasReferenceLayers, reference_layer_count: _referenceLayerCount, ...persistedProject } = project;
  const result = await apiFetch<unknown>("/api/temporal-projects/validate", {
    method: "POST",
    body: JSON.stringify({ project: persistedProject }),
  });
  return temporalProjectValidationResponseSchema.parse(result);
}

export async function runTemporalProject(
  projectId: string,
  onStatus?: (message: string) => void,
  onProgress?: (progress: RunProgressState) => void,
): Promise<TemporalProjectRunResponse> {
  let progress = createActiveRunProgress();
  onProgress?.(progress);
  onStatus?.(formatRunStatus(progress));

  let startResponse: JobStartResponse | null = null;
  try {
    startResponse = await startTemporalProjectJob(projectId);
  } catch (error) {
    if (isJobUnavailableError(error)) {
      try {
        progress = createPendingRunProgress();
        onProgress?.(progress);
        onStatus?.(formatRunStatus(progress));
        const result = await apiFetch<unknown>(`/api/temporal-projects/${encodeURIComponent(projectId)}/run`, {
          method: "POST",
        });
        const response = temporalProjectRunResponseSchema.parse(result);
        if (response.success === false) {
          const message = response.error_message ?? "The backend reported an unsuccessful temporal run.";
          const errorProgress = createErrorRunProgress(message);
          onProgress?.(errorProgress);
          onStatus?.(formatRunStatus(errorProgress));
          throw new Error(message);
        }

        const completed = createCompletedRunProgress();
        onProgress?.(completed);
        onStatus?.(formatRunStatus(completed));
        return response;
      } catch (fallbackError) {
        const message =
          fallbackError instanceof ApiClientError
            ? fallbackError.message
            : fallbackError instanceof Error
              ? fallbackError.message
              : "The backend failed to complete the temporal run.";
        const errorProgress = createErrorRunProgress(message);
        onProgress?.(errorProgress);
        onStatus?.(formatRunStatus(errorProgress));
        throw fallbackError instanceof Error ? fallbackError : new Error(message);
      }
    }

    const message =
      error instanceof ApiClientError
        ? error.message
        : error instanceof Error
          ? error.message
          : "The backend failed to queue the temporal run.";
    const errorProgress = createErrorRunProgress(message);
    onProgress?.(errorProgress);
    onStatus?.(formatRunStatus(errorProgress));
    throw error instanceof Error ? error : new Error(message);
  }

  if (!startResponse) {
    throw new Error("The backend failed to queue the temporal run.");
  }

  try {
    progress = createPendingRunProgress();
    onProgress?.(progress);
    onStatus?.(formatRunStatus(progress));

    const job = await pollJobUntilComplete(startResponse.job_id, onStatus, onProgress);
    const response = await resolveTemporalJobResponse(job);
    if (response.success === false) {
      const message = response.error_message ?? job.error_message ?? "The backend reported an unsuccessful temporal run.";
      const errorProgress = createErrorRunProgress(message);
      onProgress?.(errorProgress);
      onStatus?.(formatRunStatus(errorProgress));
      throw new Error(message);
    }

    const completed = createCompletedRunProgress();
    onProgress?.(completed);
    onStatus?.(formatRunStatus(completed));
    return response;
  } catch (error) {
    const message =
      error instanceof ApiClientError
        ? error.message
        : error instanceof Error
          ? error.message
          : "The backend failed to complete the temporal run.";
    const errorProgress = createErrorRunProgress(message);
    onProgress?.(errorProgress);
    onStatus?.(formatRunStatus(errorProgress));
    throw error instanceof Error ? error : new Error(message);
  }
}

export async function importTemporalOverride(
  projectId: string,
  releaseIdentifier: string,
  overrideGeojson: Record<string, unknown>,
): Promise<TemporalProjectRunResponse> {
  const result = await apiFetch<unknown>(
    `/api/temporal-projects/${encodeURIComponent(projectId)}/milestones/${encodeURIComponent(releaseIdentifier)}/override`,
    {
      method: "POST",
      body: JSON.stringify({ override_geojson: overrideGeojson }),
    },
  );
  const response = temporalProjectRunResponseSchema.parse(result);
  if (response.success === false) {
    throw new Error(response.error_message ?? "The backend reported an unsuccessful temporal override run.");
  }
  return response;
}

export async function listReferenceLayers(projectId: string, options?: { signal?: AbortSignal }): Promise<ReferenceLayer[]> {
  const result = await apiFetch<unknown>(`/api/temporal-projects/${encodeURIComponent(projectId)}/reference-layers`, {
    signal: options?.signal,
  });
  return z.array(referenceLayerSchema).parse(result);
}

export async function preflightReferenceLayer(
  projectId: string,
  file: File,
  scope: ReferenceLayerScope,
): Promise<ReferenceLayerPreflight> {
  const formData = new FormData();
  formData.set("file", file);
  formData.set("scope", scope);
  const result = await apiFetch<unknown>(`/api/temporal-projects/${encodeURIComponent(projectId)}/reference-layers/preflight`, {
    method: "POST",
    body: formData,
  });
  return referenceLayerPreflightSchema.parse(result);
}

export async function importReferenceLayer(
  projectId: string,
  file: File,
  name: string,
  scope: ReferenceLayerScope,
  renderingStrategy: ReferenceLayerStrategy,
): Promise<ReferenceLayer> {
  const formData = new FormData();
  formData.set("file", file);
  formData.set("name", name);
  formData.set("scope", scope);
  formData.set("rendering_strategy", renderingStrategy);
  const result = await apiFetch<unknown>(`/api/temporal-projects/${encodeURIComponent(projectId)}/reference-layers`, {
    method: "POST",
    body: formData,
  });
  return referenceLayerSchema.parse(result);
}

export async function updateReferenceLayer(
  projectId: string,
  layerId: string,
  patch: Partial<Pick<ReferenceLayer, "name" | "visible" | "opacity" | "style">>,
): Promise<ReferenceLayer> {
  const result = await apiFetch<unknown>(
    `/api/temporal-projects/${encodeURIComponent(projectId)}/reference-layers/${encodeURIComponent(layerId)}`,
    {
      method: "PATCH",
      body: JSON.stringify(patch),
    },
  );
  return referenceLayerSchema.parse(result);
}

export async function deleteReferenceLayer(projectId: string, layerId: string): Promise<void> {
  await apiFetch<unknown>(
    `/api/temporal-projects/${encodeURIComponent(projectId)}/reference-layers/${encodeURIComponent(layerId)}`,
    {
      method: "DELETE",
    },
  );
}

export async function createTemporalProjectExportBundle(projectId: string): Promise<TemporalProjectExportBundle> {
  const result = await apiFetch<unknown>(`/api/temporal-projects/${encodeURIComponent(projectId)}/export-bundle`, {
    method: "POST",
  });
  return temporalProjectExportBundleSchema.parse(result);
}
