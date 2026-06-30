import type { Polygon } from "geojson";

export type ExportPerimeterMode = "project_aoi" | "drawn" | "imported";
export type ExportDrawingPhase = "idle" | "drawing_polygon" | "drawing_rectangle" | "completed" | "cancelled";
export type ResultsExportFormat = "xlsx" | "kml" | "geojson" | "topojson" | "shapefile" | "tsv" | "json";
export type ResultsExportPerimeter =
  | { mode: "project_aoi" }
  | { mode: "custom_geometry"; source: "drawn" | "imported"; geometry: Polygon | null };
export type ResultsExportJobStatus = "queued" | "running" | "succeeded" | "failed";
export type ResultsExportJob = {
  job_id: string;
  project_id: string;
  status: ResultsExportJobStatus;
  format: string;
  progress?: number | null;
  file_size_bytes?: number | null;
  filename?: string | null;
  download_url?: string | null;
  error_message?: string | null;
};
export type ResultsExportJobRequest = {
  format: ResultsExportFormat;
  perimeter: ResultsExportPerimeter;
  includeRasters?: boolean;
  includeOfflinePackage?: boolean;
};

export function canDownloadExport(
  perimeterMode: ExportPerimeterMode,
  hasDrawnGeometry: boolean,
  hasImportedGeometry: boolean,
): boolean {
  if (perimeterMode === "project_aoi") {
    return true;
  }
  return perimeterMode === "drawn" ? hasDrawnGeometry : hasImportedGeometry;
}

export function shouldRestoreExportModal(phase: ExportDrawingPhase): boolean {
  return phase === "completed" || phase === "cancelled";
}

export function selectedExportGeometry(
  perimeterMode: ExportPerimeterMode,
  drawnGeometry: Polygon | null,
  importedGeometry: Polygon | null,
): Polygon | null {
  return perimeterMode === "drawn"
    ? drawnGeometry
    : perimeterMode === "imported"
      ? importedGeometry
      : null;
}

export function buildResultsExportPerimeter(
  perimeterMode: ExportPerimeterMode,
  drawnGeometry: Polygon | null,
  importedGeometry: Polygon | null,
): ResultsExportPerimeter {
  if (perimeterMode === "project_aoi") {
    return { mode: "project_aoi" };
  }
  return {
    mode: "custom_geometry",
    source: perimeterMode,
    geometry: selectedExportGeometry(perimeterMode, drawnGeometry, importedGeometry),
  };
}

export function buildResultsExportJobRequest(
  format: ResultsExportFormat,
  perimeter: ResultsExportPerimeter,
  includeOfflinePackage: boolean,
): ResultsExportJobRequest {
  return {
    format,
    perimeter,
    includeRasters: includeOfflinePackage,
    includeOfflinePackage,
  };
}

export function formatExportFileSize(sizeBytes: number | null | undefined): string | null {
  if (!Number.isFinite(sizeBytes ?? NaN) || !sizeBytes || sizeBytes <= 0) {
    return null;
  }
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = sizeBytes;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  const precision = unitIndex >= 3 ? 2 : unitIndex === 0 ? 0 : 1;
  return `${value.toFixed(precision)} ${units[unitIndex]}`;
}

export function formatExportJobStatus(job: ResultsExportJob | null): string | null {
  if (!job) {
    return null;
  }
  if (job.status === "queued") {
    return "Export en file d’attente";
  }
  if (job.status === "running") {
    return "Export en préparation";
  }
  if (job.status === "succeeded") {
    const size = formatExportFileSize(job.file_size_bytes);
    return size ? `Export prêt (${size})` : "Export prêt";
  }
  return job.error_message ?? "Export impossible.";
}

function sleep(milliseconds: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

function resolveDirectDownloadUrl(backendUrl: string, value: string): string {
  if (/^https?:\/\//i.test(value)) {
    return value;
  }
  if (!backendUrl) {
    return value;
  }
  return new URL(value, backendUrl).toString();
}

export async function runResultsExportJobDownload(args: {
  projectId: string;
  backendUrl: string;
  request: ResultsExportJobRequest;
  fallbackFilename: string;
  createJob: (projectId: string, request: ResultsExportJobRequest) => Promise<ResultsExportJob>;
  getJob: (projectId: string, jobId: string) => Promise<ResultsExportJob>;
  triggerDownload: (url: string, filename: string) => void;
  onJob?: (job: ResultsExportJob) => void;
  pollIntervalMs?: number;
  maxPolls?: number;
  sleepForTest?: (milliseconds: number) => Promise<void>;
}): Promise<ResultsExportJob> {
  const pollIntervalMs = args.pollIntervalMs ?? 1_500;
  const maxPolls = args.maxPolls ?? 240;
  const wait = args.sleepForTest ?? sleep;
  let job = await args.createJob(args.projectId, args.request);
  args.onJob?.(job);

  for (let attempt = 0; attempt <= maxPolls; attempt += 1) {
    if (job.status === "succeeded") {
      if (!job.download_url) {
        throw new Error("Le backend n’a pas fourni de lien de téléchargement.");
      }
      args.triggerDownload(
        resolveDirectDownloadUrl(args.backendUrl, job.download_url),
        job.filename ?? args.fallbackFilename,
      );
      return job;
    }
    if (job.status === "failed") {
      throw new Error(job.error_message ?? "Export impossible pour ce projet.");
    }
    await wait(pollIntervalMs);
    job = await args.getJob(args.projectId, job.job_id);
    args.onJob?.(job);
  }
  throw new Error("Export toujours en préparation. Réessayez dans quelques instants.");
}
