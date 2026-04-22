import { Client } from "@gradio/client";
import { z } from "zod";

import {
  backendAvailabilitySchema,
  releasesResponseSchema,
  runResponseSchema,
  type BackendAvailability,
  temporalProjectRunResponseSchema,
  temporalProjectSaveResponseSchema,
  temporalProjectSchema,
  temporalProjectSummarySchema,
  temporalProjectValidationResponseSchema,
  type RunResponse,
  type TemporalProject,
  type TemporalProjectRunResponse,
  type TemporalProjectSaveResponse,
  type TemporalProjectSummary,
  type TemporalProjectValidationResponse,
  validationResponseSchema,
  type ValidationRequest,
  type ValidationResponse,
} from "@/api/contracts";
import { getBackendSpaceUrl } from "@/lib/env";
import {
  createActiveRunProgress,
  createCompletedRunProgress,
  createErrorRunProgress,
  formatRunStatus,
  updateRunProgressFromEvent,
  type RunProgressState,
} from "@/lib/run-progress";

function parsePythonRootString(raw: string): unknown {
  const trimmed = raw.trim();
  if (!trimmed.startsWith("root=")) {
    return raw;
  }

  const decodePythonStringLiteral = (content: string): string => {
    let decoded = "";
    for (let index = 0; index < content.length; index += 1) {
      const character = content[index];
      if (character !== "\\") {
        decoded += character;
        continue;
      }

      const nextCharacter = content[index + 1];
      if (nextCharacter === undefined) {
        decoded += "\\";
        continue;
      }

      index += 1;
      switch (nextCharacter) {
        case "\\":
          decoded += "\\";
          break;
        case "'":
          decoded += "'";
          break;
        case '"':
          decoded += '"';
          break;
        case "n":
          decoded += "\n";
          break;
        case "r":
          decoded += "\r";
          break;
        case "t":
          decoded += "\t";
          break;
        case "b":
          decoded += "\b";
          break;
        case "f":
          decoded += "\f";
          break;
        case "u": {
          const hexDigits = content.slice(index + 1, index + 5);
          if (/^[0-9a-fA-F]{4}$/.test(hexDigits)) {
            decoded += String.fromCharCode(Number.parseInt(hexDigits, 16));
            index += 4;
          } else {
            decoded += "u";
          }
          break;
        }
        default:
          decoded += nextCharacter;
          break;
      }
    }

    return decoded;
  };

  const normalized = trimmed
    .slice(5)
    .replace(/\bNone\b/g, "null")
    .replace(/\bTrue\b/g, "true")
    .replace(/\bFalse\b/g, "false")
    .replace(/'([^'\\]*(?:\\.[^'\\]*)*)'/g, (_, group: string) => JSON.stringify(decodePythonStringLiteral(group)));

  return JSON.parse(normalized);
}

function normalizeValue<T>(value: unknown): T {
  if (typeof value === "string") {
    return parsePythonRootString(value) as T;
  }
  return value as T;
}

function unwrapResult<T>(value: unknown): T {
  if (typeof value === "object" && value !== null && "data" in value) {
    const data = (value as { data: unknown }).data;
    if (Array.isArray(data) && data.length === 1) {
      return normalizeValue<T>(data[0]);
    }
    return normalizeValue<T>(data);
  }
  if (Array.isArray(value) && value.length === 1) {
    return normalizeValue<T>(value[0]);
  }
  return normalizeValue<T>(value);
}

async function connectClient(): Promise<Client> {
  const backendUrl = getBackendSpaceUrl();
  return Client.connect(backendUrl, {
    events: ["data", "status"],
  });
}

export async function listReleases() {
  const client = await connectClient();
  const result = await client.predict("/list_releases", {});
  return releasesResponseSchema.parse(unwrapResult(result)).releases;
}

export async function probeBackends(): Promise<BackendAvailability[]> {
  const client = await connectClient();
  const result = await client.predict("/probe_backends", {});
  return z.array(backendAvailabilitySchema).parse(unwrapResult(result));
}

export async function validateRequest(request: ValidationRequest): Promise<ValidationResponse> {
  const client = await connectClient();
  const result = await client.predict("/validate_request", { request });
  return validationResponseSchema.parse(unwrapResult(result));
}

function formatStatus(event: Record<string, unknown>): string {
  const stage = typeof event.stage === "string" ? event.stage : "processing";
  const queuePosition =
    typeof event.queue_position === "number"
      ? event.queue_position
      : typeof event.position === "number"
        ? event.position
        : null;
  const etaSeconds =
    typeof event.rank_eta === "number"
      ? event.rank_eta
      : typeof event.eta === "number"
        ? event.eta
        : null;
  const eta = etaSeconds !== null ? `${Math.round(etaSeconds)}s` : null;

  if (queuePosition !== null && eta) {
    return `${stage} | queue position ${queuePosition} | eta ${eta}`;
  }
  if (queuePosition !== null) {
    return `${stage} | queue position ${queuePosition}`;
  }
  return stage;
}

export async function runDetection(
  request: ValidationRequest,
  onStatus: (message: string) => void,
  onProgress: (progress: RunProgressState) => void,
): Promise<RunResponse> {
  const client = await connectClient();
  const submission = client.submit("/run_detection", { request });
  let progress = createActiveRunProgress();
  onProgress(progress);
  onStatus(formatRunStatus(progress));

  for await (const event of submission as AsyncIterable<Record<string, unknown>>) {
    if (event.type === "status") {
      progress = updateRunProgressFromEvent(progress, event);
      onProgress(progress);
      onStatus(formatRunStatus(progress));
      if (event.stage === "error") {
        const message =
          typeof event.message === "string" && event.message.length > 0
            ? event.message
            : "The backend reported an error while processing the request.";
        onProgress(createErrorRunProgress(message));
        throw new Error(message);
      }
      continue;
    }

    if (event.type === "data") {
      const completed = createCompletedRunProgress();
      onProgress(completed);
      onStatus(formatRunStatus(completed));
      const response = runResponseSchema.parse(unwrapResult(event));
      if (response.success === false) {
        onProgress(createErrorRunProgress(response.error_message ?? "The backend reported an unsuccessful run."));
        throw new Error(response.error_message ?? "The backend reported an unsuccessful run.");
      }
      return response;
    }
  }
  throw new Error("The backend did not return a run response.");
}

export async function listTemporalProjects(options?: { includeCachedRuns?: boolean }): Promise<TemporalProjectSummary[]> {
  const client = await connectClient();
  const result = await client.predict("/list_temporal_projects", {
    request: {
      include_cached_runs: options?.includeCachedRuns ?? false,
    },
  });
  return z.array(temporalProjectSummarySchema).parse(unwrapResult(result));
}

export async function getTemporalProject(projectId: string): Promise<TemporalProject> {
  const client = await connectClient();
  const result = await client.predict("/get_temporal_project", { request: { project_id: projectId } });
  return temporalProjectSchema.parse(unwrapResult(result));
}

export async function getCachedRunResponse(requestHash: string): Promise<RunResponse> {
  const client = await connectClient();
  const result = await client.predict("/get_cached_run_response", { request: { request_hash: requestHash } });
  return runResponseSchema.parse(unwrapResult(result));
}

export async function saveTemporalProject(project: TemporalProject): Promise<TemporalProjectSaveResponse> {
  const client = await connectClient();
  const result = await client.predict("/save_temporal_project", { request: { project } });
  return temporalProjectSaveResponseSchema.parse(unwrapResult(result));
}

export async function validateTemporalProject(project: TemporalProject): Promise<TemporalProjectValidationResponse> {
  const client = await connectClient();
  const result = await client.predict("/validate_temporal_project", { request: { project } });
  return temporalProjectValidationResponseSchema.parse(unwrapResult(result));
}

export async function runTemporalProject(projectId: string): Promise<TemporalProjectRunResponse> {
  const client = await connectClient();
  const result = await client.predict("/run_temporal_project", { request: { project_id: projectId } });
  return temporalProjectRunResponseSchema.parse(unwrapResult(result));
}

export async function importTemporalOverride(
  projectId: string,
  releaseIdentifier: string,
  overrideGeojson: Record<string, unknown>,
): Promise<TemporalProjectRunResponse> {
  const client = await connectClient();
  const result = await client.predict("/import_temporal_override", {
    request: {
      project_id: projectId,
      release_identifier: releaseIdentifier,
      override_geojson: overrideGeojson,
    },
  });
  return temporalProjectRunResponseSchema.parse(unwrapResult(result));
}
