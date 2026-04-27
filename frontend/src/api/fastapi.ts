import { z } from "zod";

import {
  backendAvailabilitySchema,
  releasesResponseSchema,
  runResponseSchema,
  temporalProjectRunResponseSchema,
  temporalProjectSaveResponseSchema,
  temporalProjectSchema,
  temporalProjectSummarySchema,
  temporalProjectValidationResponseSchema,
  validationResponseSchema,
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

  progress = createPendingRunProgress();
  onProgress(progress);
  onStatus(formatRunStatus(progress));

  try {
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

export async function runTemporalProject(projectId: string): Promise<TemporalProjectRunResponse> {
  const result = await apiFetch<unknown>(`/api/temporal-projects/${encodeURIComponent(projectId)}/run`, {
    method: "POST",
  });
  const response = temporalProjectRunResponseSchema.parse(result);
  if (response.success === false) {
    throw new Error(response.error_message ?? "The backend reported an unsuccessful temporal run.");
  }
  return response;
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
