import { z } from "zod";

import {
  backendAvailabilitySchema,
  jobResponseSchema,
  jobStartResponseSchema,
  releasesResponseSchema,
  runResponseSchema,
  temporalProjectRunResponseSchema,
  temporalProjectSaveResponseSchema,
  temporalProjectSchema,
  temporalProjectSummarySchema,
  temporalProjectValidationResponseSchema,
  validationResponseSchema,
  type JobResponse,
  type JobStartResponse,
  type BackendAvailability,
  type RunResponse,
  type TemporalProject,
  type TemporalProjectRunResponse,
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
  type RunProgressState,
} from "@/lib/run-progress";

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

function isTerminalJobStatus(status: JobResponse["status"] | "completed"): boolean {
  return status === "complete" || status === "completed" || status === "failed" || status === "cancelled";
}

function jobToProgress(job: JobResponse): RunProgressState {
  const phase =
    job.status === "complete"
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
        : job.status === "complete"
          ? "Artifacts are ready."
          : job.error_message ?? "The backend reported a failure.");

  return {
    phase,
    percent: Math.max(0, Math.min(100, job.progress ?? (job.status === "complete" ? 100 : phase === "queued" ? 0 : 5))),
    stageLabel: job.stage ?? (phase === "complete" ? "Completed" : phase === "error" ? "Run failed" : phase === "queued" ? "Queued" : "Processing"),
    detail,
    queuePosition: null,
    etaSeconds: null,
    eventId: job.job_id,
    rawEvent: job.status,
    updatedAt: Date.now(),
  };
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
    const response = runResponseSchema.parse(job.raw_result ?? null);
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

export async function getCachedRunResponse(requestHash: string): Promise<RunResponse> {
  const result = await apiFetch<unknown>(`/api/cache/runs/${encodeURIComponent(requestHash)}`);
  return runResponseSchema.parse(result);
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
  const result = await apiFetch<unknown>("/api/temporal-projects", {
    method: "POST",
    body: JSON.stringify({ project }),
  });
  return temporalProjectSaveResponseSchema.parse(result);
}

export async function validateTemporalProject(project: TemporalProject): Promise<TemporalProjectValidationResponse> {
  const result = await apiFetch<unknown>("/api/temporal-projects/validate", {
    method: "POST",
    body: JSON.stringify({ project }),
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
    const response = temporalProjectRunResponseSchema.parse(job.raw_result ?? null);
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
