import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { FeatureCollection, Polygon } from "geojson";
import {
  AlertTriangle,
  Check,
  ChevronDown,
  Download,
  FolderOpen,
  Layers3,
  Loader2,
  Pentagon,
  Plus,
  Save,
  Trash2,
  Upload,
} from "lucide-react";

import type {
  BackendAvailability,
  ReferenceLayer,
  ReferenceLayerScope,
  ReferenceLayerStrategy,
  ReleaseMetadata,
  PipelineExecutionConfig,
  TemporalMilestone,
  TemporalProject,
} from "@/api/contracts";
import {
  createTemporalProjectExportBundle,
  deleteReferenceLayer,
  getCachedRunResponse,
  getTemporalMilestoneArtifact,
  getTemporalProject,
  importReferenceLayer,
  importTemporalOverride,
  listReferenceLayers,
  listTemporalProjects,
  preflightReferenceLayer,
  runTemporalProject,
  saveTemporalProject,
  updateReferenceLayer,
} from "@/api/client";
import { useAppStore } from "@/app/store";
import { ApiClientError } from "@/api/http";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import type { FrontendRuntimeConfig } from "@/lib/env";
import { buildBackendFileUrl, resolveBackendUrl } from "@/lib/backend-files";
import { cn, formatNumber } from "@/lib/utils";
import { downloadFileFromRequest, downloadFileFromUrl } from "@/lib/download";
import { useI18n } from "@/lib/i18n";
import { getProjectDisplayName } from "@/lib/project-summary";
import { createActiveRunProgress, createCompletedRunProgress, createErrorRunProgress, shouldShowExecutionProgressPanel } from "@/lib/run-progress";
import { relayClientLog } from "@/lib/client-log-relay";
import { AOIImportModal } from "@/features/aoi/AOIImportModal";
import { RunProgressPanel } from "@/features/results/RunProgressPanel";
import { GeometryImportModal } from "@/features/temporal/GeometryImportModal";
import {
  buildResultsExportPerimeter,
  canDownloadExport,
  shouldRestoreExportModal,
  type ExportPerimeterMode,
} from "@/features/temporal/export-workflow";
import {
  buildTemporalRunRequest,
  DEFAULT_CHANGE_THRESHOLD,
  parseChangeThresholdInput,
} from "@/features/temporal/run-detection-threshold";
import {
  buildArtifactFetchKey,
  fetchArtifactOnce,
  isFeatureCollection,
} from "@/features/temporal/artifact-fetch-state";
import { MilestoneMetricCards } from "@/features/temporal/MilestoneMetricCards";
import { ReferenceLayerImportModal } from "@/features/temporal/ReferenceLayerImportModal";
import type {
  TemporalLayerControlEntryPresentation,
  TemporalLayerControlsPresentation,
  TemporalMapPresentation,
  TemporalOutputArtifactPresentation,
} from "@/features/temporal/types";
import { SharedAoiSection } from "@/features/workspace/SharedAoiSection";
import { WorkflowParametersPanel } from "@/features/workspace/WorkflowParametersPanel";
import { WorkflowSectionCard } from "@/features/workspace/WorkflowSectionCard";
import { WorkspaceShell } from "@/features/workspace/WorkspaceShell";
import type { WorkflowSectionId } from "@/features/workspace/workflowSections";

const DEFAULT_PROJECT_DIRECTORY = "backend/runtime_cache";

function resolveProjectDirectory(projectId: string, directory: string): string {
  const trimmedDirectory = directory.trim();
  if (trimmedDirectory === DEFAULT_PROJECT_DIRECTORY) {
    return `${DEFAULT_PROJECT_DIRECTORY}/temporal_projects/${projectId}`;
  }
  return trimmedDirectory;
}


const EMPTY_FEATURE_COLLECTION: FeatureCollection = {
  type: "FeatureCollection",
  features: [],
};

type ResultsExportFormat = "xlsx" | "kml" | "geojson" | "topojson" | "shapefile" | "tsv" | "json";
type ResultsExportPerimeterMode = ExportPerimeterMode;

const RESULTS_EXPORT_OPTIONS: Array<{ format: ResultsExportFormat; label: string; pathSuffix: string; filenameSuffix: string }> = [
  { format: "xlsx", label: "Excel (.xlsx)", pathSuffix: "results.xlsx", filenameSuffix: "xlsx" },
  { format: "kml", label: "KML (.kml)", pathSuffix: "results.kml", filenameSuffix: "kml" },
  { format: "geojson", label: "GeoJSON (.geojson)", pathSuffix: "results.geojson", filenameSuffix: "geojson" },
  { format: "topojson", label: "TopoJSON (.topojson)", pathSuffix: "results.topojson", filenameSuffix: "topojson" },
  { format: "shapefile", label: "ESRI Shapefile (.zip)", pathSuffix: "results_shapefile.zip", filenameSuffix: "zip" },
  { format: "tsv", label: "Power BI (.tsv)", pathSuffix: "results.tsv", filenameSuffix: "tsv" },
  { format: "json", label: "JSON (.json)", pathSuffix: "results.json", filenameSuffix: "json" },
];

function formatReleaseDate(value: string | undefined | null, locale: string, fallback: string): string {
  if (!value) {
    return fallback;
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat(locale, {
    day: "2-digit",
    month: "short",
    year: "numeric",
  }).format(date);
}

function temporalMilestoneChronologicalValue(milestone: TemporalMilestone): number {
  const parsedDate = Date.parse(milestone.release_date ?? "");
  if (Number.isFinite(parsedDate)) {
    return parsedDate;
  }
  const releaseYear = milestone.release_identifier.match(/(?:WB_|^)(\d{4})/)?.[1];
  return releaseYear ? Date.UTC(Number(releaseYear), 0, 1) : Number.MAX_SAFE_INTEGER;
}

function formatArea(areaM2: number | undefined, fallback: string): string {
  if (!areaM2 || areaM2 <= 0) {
    return fallback;
  }
  if (areaM2 >= 1_000_000) {
    return `${formatNumber(areaM2 / 1_000_000, 2)} km²`;
  }
  return `${formatNumber(areaM2, 0)} m²`;
}

function getArchiveCode(release: ReleaseMetadata): string {
  const identifierMatch = release.identifier.match(/R\d+$/i);
  if (identifierMatch) {
    return identifierMatch[0].toUpperCase();
  }
  const labelMatch = release.label.match(/R\d+$/i);
  return labelMatch?.[0].toUpperCase() ?? "Archive";
}

function formatReleaseMetadataDate(release: ReleaseMetadata, locale: string): string {
  const date = new Date(release.release_date);
  if (Number.isNaN(date.getTime())) {
    return release.label;
  }

  return new Intl.DateTimeFormat(locale, {
    day: "2-digit",
    month: "numeric",
    year: "numeric",
  }).format(date);
}

function groupReleasesByYear(releases: ReleaseMetadata[]): Map<number, ReleaseMetadata[]> {
  const grouped = new Map<number, ReleaseMetadata[]>();

  releases.forEach((release) => {
    const date = new Date(release.release_date);
    const year = Number.isNaN(date.getTime()) ? new Date().getFullYear() : date.getFullYear();
    const yearReleases = grouped.get(year) ?? [];
    yearReleases.push(release);
    grouped.set(year, yearReleases);
  });

  return new Map([...grouped.entries()].sort(([leftYear], [rightYear]) => rightYear - leftYear));
}

function YearGroupRow({
  year,
  isExpanded,
  onToggle,
  releaseCount,
  releaseLabel,
}: {
  year: number;
  isExpanded: boolean;
  onToggle: () => void;
  releaseCount: number;
  releaseLabel: string;
}) {
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-expanded={isExpanded}
      className="flex w-full items-center justify-between rounded border border-sidebar-border bg-sidebar px-4 py-3 text-left transition-all hover:border-primary hover:bg-surface focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2"
    >
      <div className="flex items-center gap-2">
        <ChevronDown
          className={cn("h-4 w-4 text-muted-foreground transition-transform duration-200", !isExpanded && "-rotate-90")}
          aria-hidden="true"
        />
        <span className="text-label font-semibold text-foreground">{year}</span>
        <span className="rounded bg-surface px-2 py-1 text-caption text-muted-foreground">
          {releaseCount} {releaseLabel}
        </span>
      </div>
    </button>
  );
}

function ReleaseItem({
  release,
  selected,
  disabled,
  onSelect,
  locale,
}: {
  release: ReleaseMetadata;
  selected: boolean;
  disabled: boolean;
  onSelect: () => void;
  locale: string;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      disabled={disabled}
      className={cn(
        "flex w-full items-center justify-between rounded border px-3 py-2.5 text-left transition-all focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50",
        selected
          ? "border-primary bg-primary/10 text-foreground hover:bg-primary/15 active:bg-primary/20"
          : "border-sidebar-border bg-sidebar text-foreground hover:border-primary hover:bg-surface focus-visible:border-primary",
      )}
    >
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <span className="text-label font-semibold tracking-tight text-foreground">
            {formatReleaseMetadataDate(release, locale)}
          </span>
          <span className="rounded-full border border-sidebar-border bg-sidebar px-2 py-0.5 text-caption uppercase tracking-wider text-muted-foreground">
            {getArchiveCode(release)}
          </span>
        </div>
      </div>
      {selected ? <Check className="h-4 w-4 shrink-0 text-primary" aria-hidden="true" /> : null}
    </button>
  );
}

function TimelineSectionHeader({ number, label }: { number: number; label: string }) {
  return (
    <div className="flex items-center gap-2">
      <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-primary text-xs font-semibold text-primary-foreground">
        {number}
      </span>
      <span className="text-xs font-semibold uppercase tracking-[0.08em] text-foreground">{label}</span>
    </div>
  );
}

function TemporalLayerControlRow({ entry }: { entry: TemporalLayerControlEntryPresentation }) {
  return (
    <label
      className={cn(
        "flex items-start justify-between gap-3 rounded px-2 py-1.5 text-sm",
        entry.enabled ? "text-foreground" : "text-muted-foreground",
      )}
    >
      <span className="min-w-0">
        <span className="flex min-w-0 items-center gap-2">
          {entry.swatch ? (
            <span
              aria-hidden="true"
              className="h-3 w-3 shrink-0 rounded-[2px] border border-sidebar-border"
              style={{ backgroundColor: entry.swatch.color, opacity: entry.swatch.opacity ?? 1 }}
            />
          ) : null}
          <span>{entry.label}</span>
        </span>
        {entry.description ? <span className="mt-0.5 block text-caption text-muted-foreground">{entry.description}</span> : null}
      </span>
      <input
        type="checkbox"
        checked={entry.checked}
        onChange={(event) => entry.onCheckedChange(event.target.checked)}
        disabled={!entry.enabled}
        className="mt-0.5 h-4 w-4 rounded border-sidebar-border bg-sidebar accent-primary disabled:opacity-40"
      />
    </label>
  );
}

function TemporalLayerControlsBlock({
  controls,
  t,
}: {
  controls: TemporalLayerControlsPresentation | null;
  t: (key: string, fallback?: string) => string;
}) {
  return (
    <div className="space-y-3">
      <TimelineSectionHeader number={2} label={t("map.layers")} />
      <div className="rounded-lg border border-sidebar-border bg-sidebar p-3">
        {controls?.referenceWarning ? (
          <p className="mb-2 rounded border border-warning/30 bg-warning/10 px-2 py-1.5 text-caption text-warning-foreground">
            {controls.referenceWarning}
          </p>
        ) : null}
        <div className="space-y-3">
          <div>
            <p className="px-2 pb-1 text-xs font-semibold text-foreground">{t("map.satellite_view_section")}</p>
            {(controls?.satellite ?? []).length > 0 ? (
              controls?.satellite.map((entry) => <TemporalLayerControlRow key={entry.key} entry={entry} />)
            ) : (
              <p className="px-2 py-1.5 text-sm text-muted-foreground">{t("map.reference_imagery_unavailable")}</p>
            )}
          </div>
          <div>
            <p className="px-2 pb-1 text-xs font-semibold text-foreground">{t("map.building_evolution_section")}</p>
            {(controls?.buildingEvolution ?? []).map((entry) => (
              <TemporalLayerControlRow key={entry.key} entry={entry} />
            ))}
          </div>
          {controls?.manualReferenceLayers.length ? (
            <div>
              <p className="px-2 pb-1 text-xs font-semibold text-foreground">{t("reference_layer.map_section")}</p>
              {controls.manualReferenceLayers.map((layer) => (
                <div key={layer.id} className="rounded px-2 py-1.5 text-sm text-foreground">
                  <div className="flex items-center justify-between gap-3">
                    <span className="min-w-0 truncate">{layer.name}</span>
                    <span className="text-caption text-muted-foreground">{Math.round(layer.opacity * 100)}%</span>
                  </div>
                  <p className="mt-0.5 text-caption text-muted-foreground">
                    {layer.geometryType ?? "—"} / {layer.storageStrategy ?? "—"}
                  </p>
                </div>
              ))}
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function formatErrorMessage(error: unknown, fallback: string): string {
  if (typeof error === "string") {
    return error;
  }
  if (error instanceof Error) {
    return error.message;
  }
  if (error && typeof error === "object") {
    const maybeMessage = "message" in error ? (error as { message?: unknown }).message : null;
    if (typeof maybeMessage === "string" && maybeMessage.length > 0) {
      return maybeMessage;
    }
    try {
      return JSON.stringify(error);
    } catch {
      return fallback;
    }
  }
  return fallback;
}

const UNRELATED_BANDON_WARNING =
  "BANDON applied an MPS slide-window compatibility patch to the configured crop/stride.";

function filterProgressWarnings(messages: string[]) {
  return messages.filter((message) => message !== UNRELATED_BANDON_WARNING);
}

function buildProjectId() {
  return `temporal-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

function nowIso() {
  return new Date().toISOString();
}

function buildProjectIdFromName(name: string) {
  const slug = name
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 48);
  const suffix = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
  return `temporal-${slug || "project"}-${suffix}`;
}

function normalizeProjectPath(value: string | null | undefined): string {
  return value ? value.replace(/\/+$/g, "") : "";
}

function assertLoadedProjectMatchesSelection(
  loadedProject: TemporalProject,
  requestedProjectId: string,
  expectedProjectDir?: string | null,
) {
  if (loadedProject.project_id !== requestedProjectId) {
    throw new Error(`Loaded project ${loadedProject.project_id} does not match requested project ${requestedProjectId}.`);
  }
  const loadedProjectDir = normalizeProjectPath(loadedProject.project_dir);
  const expectedDir = normalizeProjectPath(expectedProjectDir);
  if (expectedDir && loadedProjectDir && loadedProjectDir !== expectedDir) {
    throw new Error(`Loaded project directory ${loadedProjectDir} does not match saved project directory ${expectedDir}.`);
  }
}

function preferredLoadedProjectPanel(project: TemporalProject): WorkflowSectionId {
  return project.milestones.length > 0 ? "progress" : "aoi";
}

function buildExecutionConfig(): PipelineExecutionConfig {
  return {
    inference_backend: "bandon_mps",
  };
}

function emptyProject(aoi: Polygon | null, projectName: string, executionConfig: PipelineExecutionConfig): TemporalProject {
  const timestamp = nowIso();
  return {
    project_id: buildProjectId(),
    name: projectName,
    project_dir: null,
    semantics: "expansion_only",
    aoi_geojson: aoi,
    milestones: [],
    created_at: timestamp,
    updated_at: timestamp,
    execution_config: executionConfig,
    warnings: [],
    validation_blocking_errors: [],
    download_bundle_path: null,
    has_reference_layers: false,
    reference_layer_count: 0,
  };
}

function createMilestone(release: ReleaseMetadata): TemporalMilestone {
  return {
    release_identifier: release.identifier,
    release_date: release.release_date,
    status: "pending",
    source_mode: "automated",
    warnings: [],
    error_message: null,
    pair_request_hash: null,
    automated_additions_geojson: null,
    automated_candidate_footprint_geojson: null,
    automated_building_blocks_geojson: null,
    manual_override_geojson: null,
    additions_geojson: null,
    effective_building_blocks_geojson: null,
    effective_footprint_geojson: null,
    buffer_layers_geojson: {},
    cumulative_union_geojson: null,
    cumulative_convex_hull_geojson: null,
    cumulative_growth_blocks_geojson: null,
    cumulative_growth_envelope_geojson: null,
    metrics: null,
    artifacts: [],
  };
}

function formatMilestoneIdentifier(milestone: TemporalMilestone, t: (key: string) => string): string {
  return milestone.release_identifier;
}

function ensureFeatureCollection(value: Record<string, unknown> | null | undefined): FeatureCollection {
  if (value && value.type === "FeatureCollection" && Array.isArray(value.features)) {
    return value as unknown as FeatureCollection;
  }
  return EMPTY_FEATURE_COLLECTION;
}

function hasFeatureCollectionFeatures(value: Record<string, unknown> | null | undefined): boolean {
  return ensureFeatureCollection(value).features.length > 0;
}

function mergeFeatureCollections(values: FeatureCollection[]): FeatureCollection {
  return {
    type: "FeatureCollection",
    features: values.flatMap((value) => value.features),
  };
}

function hasMilestoneBufferFeatures(milestone: TemporalMilestone): boolean {
  return (
    hasFeatureCollectionFeatures(milestone.buffer_layers_geojson?.["10m"]) ||
    hasFeatureCollectionFeatures(milestone.buffer_layers_geojson?.["15m"]) ||
    hasFeatureCollectionFeatures(milestone.buffer_layers_geojson?.["20m"])
  );
}

const TEMPORAL_LAZY_ARTIFACT_FIELDS = [
  ["automated_building_blocks", "automated_building_blocks_geojson"],
  ["additions", "additions_geojson"],
] as const;

const TEMPORAL_LAZY_BUFFER_ARTIFACT_FIELDS = [
  ["building_change_buffer_10m", "10m"],
  ["building_change_buffer_15m", "15m"],
  ["building_change_buffer_20m", "20m"],
] as const;

const TEMPORAL_ALLOWED_ARTIFACT_KEYS = new Set([
  "automated_building_blocks",
  "additions",
  "building_change_buffer_10m",
  "building_change_buffer_15m",
  "building_change_buffer_20m",
  "cumulative_building_change_buffer_10m",
  "cumulative_building_change_buffer_15m",
  "cumulative_building_change_buffer_20m",
]);

function milestoneHasArtifact(milestone: TemporalMilestone, key: string): boolean {
  return milestone.artifacts.some(
    (artifact) =>
      artifact.key === key &&
      ((artifact.feature_count ?? 0) > 0 ||
        (artifact.size_bytes ?? 0) > 0 ||
        Boolean(artifact.tilejson_url) ||
        Boolean(artifact.tiles_url_template)),
  );
}

function milestoneArtifactByKey(milestone: TemporalMilestone, key: string) {
  return milestone.artifacts.find((artifact) => artifact.key === key) ?? null;
}

function isHugeTemporalArtifact(milestone: TemporalMilestone, key: string): boolean {
  const artifact = milestoneArtifactByKey(milestone, key);
  const artifactRecord = artifact as
    | (typeof artifact & {
        tilejsonUrl?: string | null;
        tilesUrlTemplate?: string | null;
        sizeBytes?: number | null;
        featureCount?: number | null;
      })
    | null;
  return Boolean(
    artifact?.tilejson_url ||
      artifact?.tiles_url_template ||
      artifactRecord?.tilejsonUrl ||
      artifactRecord?.tilesUrlTemplate ||
      (artifact?.feature_count ?? artifactRecord?.featureCount ?? 0) >= 20_000 ||
      (artifact?.size_bytes ?? artifactRecord?.sizeBytes ?? 0) >= 10_000_000,
  );
}

function temporalArtifactPresentation(
  backendUrl: string,
  milestone: TemporalMilestone,
): Record<string, TemporalOutputArtifactPresentation> {
  return Object.fromEntries(
    milestone.artifacts
      .filter((artifact) => artifact.key && TEMPORAL_ALLOWED_ARTIFACT_KEYS.has(artifact.key))
      .map((artifact) => [
        artifact.key as string,
        {
          key: artifact.key as string,
          featureCount: artifact.feature_count ?? null,
          sizeBytes: artifact.size_bytes ?? null,
          bbox:
            Array.isArray(artifact.bbox) &&
            artifact.bbox.length >= 4 &&
            artifact.bbox.every((value) => Number.isFinite(value))
              ? ([artifact.bbox[0], artifact.bbox[1], artifact.bbox[2], artifact.bbox[3]] as [number, number, number, number])
              : null,
          artifactUrl: resolveBackendUrl(backendUrl, artifact.artifact_url),
          tilejsonUrl: resolveBackendUrl(backendUrl, artifact.tilejson_url),
          tilesUrlTemplate: resolveBackendUrl(backendUrl, artifact.tiles_url_template),
          vectorSourceLayer: artifact.vector_source_layer ?? null,
        },
      ]),
  );
}

function hasValidRasterBounds(bounds: number[] | null | undefined): bounds is [number, number, number, number] {
  return Array.isArray(bounds) && bounds.length >= 4 && bounds.every((value) => Number.isFinite(value));
}

function milestoneHasReferenceImageryPresentation(milestone: TemporalMilestone): boolean {
  const imagery = milestone.reference_imagery;
  if (!imagery) {
    return false;
  }

  if (imagery.tilejson_url || imagery.tiles_url_template || imagery.cog_url || imagery.cog_path) {
    return true;
  }

  return Boolean(imagery.image_png_data_url || imagery.image_path) && hasValidRasterBounds(imagery.raster_bounds_wgs84);
}

function milestoneHasMapPresentation(milestone: TemporalMilestone): boolean {
  const hasReferenceImagery = milestoneHasReferenceImageryPresentation(milestone);

  return (
    hasReferenceImagery ||
    hasFeatureCollectionFeatures(milestone.automated_candidate_footprint_geojson) ||
    hasFeatureCollectionFeatures(milestone.automated_building_blocks_geojson) ||
    hasFeatureCollectionFeatures(milestone.additions_geojson) ||
    hasFeatureCollectionFeatures(milestone.effective_building_blocks_geojson) ||
    hasFeatureCollectionFeatures(milestone.effective_footprint_geojson) ||
    hasFeatureCollectionFeatures(milestone.cumulative_union_geojson) ||
    hasFeatureCollectionFeatures(milestone.cumulative_growth_blocks_geojson) ||
    hasFeatureCollectionFeatures(milestone.cumulative_growth_envelope_geojson) ||
    hasFeatureCollectionFeatures(milestone.manual_override_geojson)
  );
}

function preferredMilestoneId(project: TemporalProject | null | undefined): string | null {
  if (!project?.milestones.length) {
    return null;
  }

  const latestPresentableMilestone = [...project.milestones].reverse().find(milestoneHasMapPresentation);
  return latestPresentableMilestone?.release_identifier ?? project.milestones.at(-1)?.release_identifier ?? null;
}

function milestoneBadgeTone(status: TemporalMilestone["status"]): string {
  if (status === "complete") return "border-primary/30 bg-primary/10 text-foreground";
  if (status === "validated") return "border-accent/30 bg-accent/10 text-foreground";
  if (status === "error") return "border-destructive/30 bg-destructive/10 text-destructive-foreground";
  return "bg-surface text-foreground border-sidebar-border";
}

interface TemporalMosaicPanelProps {
  workflowMode: "pairwise" | "temporal";
  onWorkflowModeChange: (mode: "pairwise" | "temporal") => void;
  backendUrl: string;
  runtimeConfig: FrontendRuntimeConfig;
  releases: ReleaseMetadata[];
  releasesLoading: boolean;
  releasesError: string | null;
  backendAvailability: BackendAvailability[];
  backendAvailabilityLoading: boolean;
  backendAvailabilityError: string | null;
  isCollapsed: boolean;
  onToggleCollapse: () => void;
  onMapPresentationChange: (presentation: TemporalMapPresentation | null) => void;
  temporalLayerControls: TemporalLayerControlsPresentation | null;
}

export function TemporalMosaicPanel({
  workflowMode,
  onWorkflowModeChange,
  backendUrl,
  runtimeConfig,
  releases,
  releasesLoading,
  releasesError,
  backendAvailability,
  backendAvailabilityLoading,
  backendAvailabilityError,
  isCollapsed,
  onToggleCollapse,
  onMapPresentationChange,
  temporalLayerControls,
}: TemporalMosaicPanelProps) {
  const queryClient = useQueryClient();
  const hydratingAoiRef = useRef(false);
  const [selectedMilestoneId, setSelectedMilestoneId] = useState<string | null>(null);
  const [releaseFilter, setReleaseFilter] = useState("");
  const [expandedYears, setExpandedYears] = useState<Set<number>>(new Set());
  const [activePanel, setActivePanel] = useState<WorkflowSectionId>("overview");
  const [aoiImportModalOpen, setAoiImportModalOpen] = useState(false);
  const [overrideModalOpen, setOverrideModalOpen] = useState(false);
  const [referenceLayerModalOpen, setReferenceLayerModalOpen] = useState(false);
  const [createProjectOpen, setCreateProjectOpen] = useState(false);
  const [createProjectName, setCreateProjectName] = useState("");
  const [createProjectDirectory, setCreateProjectDirectory] = useState(DEFAULT_PROJECT_DIRECTORY);
  const [createProjectError, setCreateProjectError] = useState<string | null>(null);
  const [createProjectBusy, setCreateProjectBusy] = useState(false);
  const [selectedProjectId, setSelectedProjectId] = useState("");
  const [progressMetricsVisible, setProgressMetricsVisible] = useState(false);
  const [resultsExportModalOpen, setResultsExportModalOpen] = useState(false);
  const [resultsExportFormat, setResultsExportFormat] = useState<ResultsExportFormat>("shapefile");
  const [resultsExportPerimeterMode, setResultsExportPerimeterMode] = useState<ResultsExportPerimeterMode>("project_aoi");
  const [resultsExportImportOpen, setResultsExportImportOpen] = useState(false);
  const [resultsExportBusy, setResultsExportBusy] = useState<ResultsExportFormat | null>(null);
  const [resultsExportError, setResultsExportError] = useState<string | null>(null);
  const [runChangeThreshold, setRunChangeThreshold] = useState(String(DEFAULT_CHANGE_THRESHOLD));
  const [runChangeThresholdError, setRunChangeThresholdError] = useState<string | null>(null);
  const [staleReferenceLayerProjectIds, setStaleReferenceLayerProjectIds] = useState<Set<string>>(new Set());

  const aoi = useAppStore((state) => state.aoi);
  const draftVertices = useAppStore((state) => state.draftVertices);
  const drawingMode = useAppStore((state) => state.drawingMode);
  const drawingSubMode = useAppStore((state) => state.drawingSubMode);
  const startDrawing = useAppStore((state) => state.startDrawing);
  const startRectangleDrawing = useAppStore((state) => state.startRectangleDrawing);
  const startEditing = useAppStore((state) => state.startEditing);
  const setDrawingSubMode = useAppStore((state) => state.setDrawingSubMode);
  const setAoiFromImport = useAppStore((state) => state.setAoiFromImport);
  const clearAoi = useAppStore((state) => state.clearAoi);
  const requestMapFocusToAoi = useAppStore((state) => state.requestMapFocusToAoi);
  const requestMapFocusToReferenceLayer = useAppStore((state) => state.requestMapFocusToReferenceLayer);
  const exportGeometry = useAppStore((state) => state.exportGeometry);
  const exportDrawnGeometry = useAppStore((state) => state.exportDrawnGeometry);
  const exportImportedGeometry = useAppStore((state) => state.exportImportedGeometry);
  const exportDrawingPhase = useAppStore((state) => state.exportDrawingPhase);
  const startExportDrawing = useAppStore((state) => state.startExportDrawing);
  const setExportImportedGeometry = useAppStore((state) => state.setExportImportedGeometry);
  const selectExportGeometry = useAppStore((state) => state.selectExportGeometry);
  const acknowledgeExportDrawing = useAppStore((state) => state.acknowledgeExportDrawing);
  const clearExportGeometry = useAppStore((state) => state.clearExportGeometry);
  const stopDrawing = useAppStore((state) => state.stopDrawing);
  const project = useAppStore((state) => state.temporalProject);
  const setProject = useAppStore((state) => state.setTemporalProject);
  const settings = useAppStore((state) => state.settings);
  const temporalProjectBootstrap = useAppStore((state) => state.temporalProjectBootstrap);
  const setTemporalProjectBootstrap = useAppStore((state) => state.setTemporalProjectBootstrap);
  const setSelectedReleaseIds = useAppStore((state) => state.setSelectedReleaseIds);
  const runProgress = useAppStore((state) => state.runProgress);
  const setRunProgress = useAppStore((state) => state.setRunProgress);
  const setIsRunning = useAppStore((state) => state.setIsRunning);
  const previousAoiRef = useRef<Polygon | null>(aoi);
  const latestProjectLoadRef = useRef<string | null>(null);
  const cumulativeAdditionsFetchesRef = useRef<Set<string>>(new Set());
  const exportDownloadEnabled = canDownloadExport(
    resultsExportPerimeterMode,
    Boolean(exportDrawnGeometry),
    Boolean(exportImportedGeometry),
  );

  useEffect(() => {
    if (!shouldRestoreExportModal(exportDrawingPhase)) {
      return;
    }
    setResultsExportModalOpen(true);
    acknowledgeExportDrawing();
  }, [acknowledgeExportDrawing, exportDrawingPhase]);

  const { t, language } = useI18n();
  const locale = language === "fr" ? "fr-FR" : "en-GB";

  const sortedReleases = useMemo(
    () => [...releases].sort((left, right) => Date.parse(right.release_date) - Date.parse(left.release_date)),
    [releases],
  );
  const projectsQuery = useQuery({
    queryKey: ["temporal-projects", "saved-only"],
    queryFn: () => listTemporalProjects(),
  });

  const referenceLayersQuery = useQuery({
    queryKey: ["reference-layers", project?.project_id],
    queryFn: async ({ signal }) => {
      const projectId = project?.project_id ?? "";
      const startedAt = performance.now();
      relayClientLog("REFERENCE_LAYER_MANUAL_FETCH_START", {
        projectId,
        reason: "manual_reference_layer_panel",
      });
      try {
        const layers = await listReferenceLayers(projectId, { signal });
        relayClientLog("REFERENCE_LAYER_MANUAL_FETCH_DONE", {
          projectId,
          count: layers.length,
          durationMs: Math.round(performance.now() - startedAt),
        });
        return layers;
      } catch (error) {
        if (error instanceof ApiClientError && error.status === 404) {
          setStaleReferenceLayerProjectIds((current) => {
            if (current.has(projectId)) {
              return current;
            }
            const next = new Set(current);
            next.add(projectId);
            return next;
          });
          relayClientLog("TEMPORAL_STALE_PROJECT_REFERENCE_CLEARED", {
            projectId,
            reason: "reference_layers_404",
            currentProjectId: project?.project_id ?? null,
            affectedCurrentProject: project?.project_id === projectId,
          });
          return [];
        }
        throw error;
      }
    },
    enabled: Boolean(project?.project_id && !staleReferenceLayerProjectIds.has(project.project_id)),
    retry: (failureCount, error) => !(error instanceof ApiClientError && error.status === 404) && failureCount < 1,
  });

  type LoadProjectRequest = {
    projectId: string;
    expectedProjectDir?: string | null;
    focusPanel?: boolean;
  };

  const loadProjectMutation = useMutation({
    mutationFn: async ({ projectId, expectedProjectDir }: LoadProjectRequest) => {
      const loadedProject = await getTemporalProject(projectId);
      assertLoadedProjectMatchesSelection(loadedProject, projectId, expectedProjectDir);
      return loadedProject;
    },
    onSuccess: (loadedProject, variables) => {
      if (loadedProject.project_id !== variables.projectId) {
        return;
      }
      if (latestProjectLoadRef.current && latestProjectLoadRef.current !== variables.projectId) {
        return;
      }
      const hydratedProject = {
        ...loadedProject,
        milestones: [...loadedProject.milestones].sort(
          (left, right) => Date.parse(left.release_date ?? "") - Date.parse(right.release_date ?? ""),
        ),
      };
      hydratingAoiRef.current = true;
      setProject(hydratedProject);
      setSelectedProjectId(hydratedProject.project_id);
      setSelectedMilestoneId(preferredMilestoneId(hydratedProject));
      setSelectedReleaseIds(hydratedProject.milestones.map((item) => item.release_identifier));
      setProgressMetricsVisible(true);
      onWorkflowModeChange("temporal");
      if (hydratedProject.aoi_geojson && hydratedProject.aoi_geojson.type === "Polygon") {
        setAoiFromImport(hydratedProject.aoi_geojson as Polygon);
        requestMapFocusToAoi();
      } else if (!hydratedProject.aoi_geojson) {
        clearAoi();
      }
      if (variables.focusPanel) {
        setActivePanel(preferredLoadedProjectPanel(hydratedProject));
      }
      queueMicrotask(() => {
        hydratingAoiRef.current = false;
      });
    },
  });

  const loadSavedProject = (projectId: string, expectedProjectDir?: string | null) => {
    if (!projectId) {
      return;
    }
    setSelectedProjectId(projectId);
    latestProjectLoadRef.current = projectId;
    loadProjectMutation.mutate({
      projectId,
      expectedProjectDir,
      focusPanel: true,
    });
  };

  const saveProjectMutation = useMutation({
    mutationFn: saveTemporalProject,
    onSuccess: (savedProject) => {
      setProject((current) =>
        current
          ? {
              ...current,
              updated_at: savedProject.updated_at,
              download_bundle_path: savedProject.download_bundle_path ?? current.download_bundle_path,
            }
          : current,
      );
      void queryClient.invalidateQueries({ queryKey: ["temporal-projects"] });
    },
  });

  const runProjectMutation = useMutation({
    mutationFn: async ({ projectId, changeThreshold }: { projectId: string; changeThreshold: number }) => {
      setIsRunning(true);
      setRunProgress({
        ...createActiveRunProgress(),
        phase: "running",
        percent: 5,
        stageLabel: "Metadata",
        detail: "Temporal mosaic timeline run started.",
      });
      return runTemporalProject(
        projectId,
        buildTemporalRunRequest(changeThreshold),
        undefined,
        setRunProgress,
        () => setActivePanel("progress"),
      );
    },
    onSuccess: (response) => {
      setIsRunning(false);
      setRunProgress(createCompletedRunProgress());
      setProgressMetricsVisible(true);
      setProject(response.project);
      void queryClient.invalidateQueries({ queryKey: ["reference-layers", response.project.project_id] });
      void queryClient.invalidateQueries({ queryKey: ["temporal-projects"] });
    },
    onError: (error) => {
      setIsRunning(false);
      setRunProgress(createErrorRunProgress(error instanceof Error ? error.message : t("status.run_failed")));
    },
  });

  const importOverrideMutation = useMutation({
    mutationFn: ({
      projectId,
      releaseIdentifier,
      overrideGeojson,
    }: {
      projectId: string;
      releaseIdentifier: string;
      overrideGeojson: Record<string, unknown>;
    }) => importTemporalOverride(projectId, releaseIdentifier, overrideGeojson),
    onSuccess: (response) => {
      setProject(response.project);
      void queryClient.invalidateQueries({ queryKey: ["temporal-projects"] });
    },
  });

  const importReferenceLayerMutation = useMutation({
    mutationFn: ({
      file,
      name,
      scope,
      strategy,
    }: {
      file: File;
      name: string;
      scope: ReferenceLayerScope;
      strategy: ReferenceLayerStrategy;
    }) => {
      if (!project?.project_id) {
        throw new Error(t("reference_layer.no_project"));
      }
      return importReferenceLayer(project.project_id, file, name, scope, strategy);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["reference-layers", project?.project_id] });
    },
  });

  const updateReferenceLayerMutation = useMutation({
    mutationFn: ({ layer, patch }: { layer: ReferenceLayer; patch: Partial<Pick<ReferenceLayer, "visible" | "opacity">> }) => {
      if (!project?.project_id) {
        throw new Error(t("reference_layer.no_project"));
      }
      return updateReferenceLayer(project.project_id, layer.layer_id, patch);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["reference-layers", project?.project_id] });
    },
  });

  const deleteReferenceLayerMutation = useMutation({
    mutationFn: (layer: ReferenceLayer) => {
      if (!project?.project_id) {
        throw new Error(t("reference_layer.no_project"));
      }
      return deleteReferenceLayer(project.project_id, layer.layer_id);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["reference-layers", project?.project_id] });
    },
  });

  useEffect(() => {
    if (workflowMode !== "temporal") {
      return;
    }
    if (project || temporalProjectBootstrap || loadProjectMutation.isPending) {
      if (loadProjectMutation.isPending) {
        relayClientLog("TEMPORAL_PROJECT_ROUTE_GUARD", {
          projectId: selectedProjectId,
          state: "loading_project",
        });
      }
      return;
    }
    setProgressMetricsVisible(false);
    setProject(emptyProject(aoi, t("temporal.untitled_project"), buildExecutionConfig()));
  }, [aoi, project, setProject, temporalProjectBootstrap, loadProjectMutation.isPending, workflowMode, t]);

  useEffect(() => {
    if (!project || hydratingAoiRef.current) {
      return;
    }
    if (previousAoiRef.current === aoi) {
      return;
    }
    previousAoiRef.current = aoi;
    setProject((current) =>
      current
        ? {
            ...current,
            aoi_geojson: aoi,
            updated_at: nowIso(),
          }
        : current,
    );
  }, [aoi, project]);

  useEffect(() => {
    if (!temporalProjectBootstrap || workflowMode !== "temporal") {
      return;
    }

    hydratingAoiRef.current = true;
    setProject(temporalProjectBootstrap);
    setSelectedProjectId(temporalProjectBootstrap.project_id);
    setSelectedMilestoneId(preferredMilestoneId(temporalProjectBootstrap));
    setSelectedReleaseIds(temporalProjectBootstrap.milestones.map((item) => item.release_identifier));
    setProgressMetricsVisible(true);

    if (temporalProjectBootstrap.aoi_geojson && temporalProjectBootstrap.aoi_geojson.type === "Polygon") {
      setAoiFromImport(temporalProjectBootstrap.aoi_geojson as Polygon);
      requestMapFocusToAoi();
    } else if (!temporalProjectBootstrap.aoi_geojson) {
      clearAoi();
    }

    setActivePanel(preferredLoadedProjectPanel(temporalProjectBootstrap));
    setTemporalProjectBootstrap(null);
    queueMicrotask(() => {
      hydratingAoiRef.current = false;
    });
  }, [
    clearAoi,
    requestMapFocusToAoi,
    setAoiFromImport,
    setTemporalProjectBootstrap,
    temporalProjectBootstrap,
    workflowMode,
  ]);

  useEffect(() => {
    if (!project) {
      return;
    }
    if (temporalProjectBootstrap || loadProjectMutation.isPending) {
      return;
    }
    if (project.milestones.length > 0 && workflowMode !== "temporal") {
      onWorkflowModeChange("temporal");
    }
  }, [loadProjectMutation.isPending, onWorkflowModeChange, project?.milestones.length, temporalProjectBootstrap, workflowMode]);

  const selectedMilestone = useMemo(
    () => project?.milestones.find((item) => item.release_identifier === selectedMilestoneId) ?? null,
    [project, selectedMilestoneId],
  );
  const milestoneCount = project?.milestones.length ?? 0;
  const referenceLayers = referenceLayersQuery.data ?? [];
  const manualReferenceLayers = referenceLayers;

  useEffect(() => {
    if (!project?.project_id || !selectedMilestone) {
      return;
    }
    const releaseIdentifier = selectedMilestone.release_identifier;
    const projectVersion = project.updated_at ?? "unknown";
    const artifactFetches: Promise<[string, Record<string, unknown>] | null>[] = [];
    let cancelled = false;

    for (const [artifactKey, fieldName] of TEMPORAL_LAZY_ARTIFACT_FIELDS) {
      const currentValue = selectedMilestone[fieldName];
      if (!isFeatureCollection(currentValue) && milestoneHasArtifact(selectedMilestone, artifactKey)) {
        if (isHugeTemporalArtifact(selectedMilestone, artifactKey)) {
          relayClientLog("TEMPORAL_GEOJSON_FETCH_SKIPPED_HUGE_ARTIFACT", {
            projectId: project.project_id,
            releaseIdentifier,
            artifactKey,
            reason: "vector_tile_available",
          });
          relayClientLog("TEMPORAL_GEOJSON_FETCH_BLOCKED_VECTOR_TILE_ARTIFACT", {
            projectId: project.project_id,
            releaseIdentifier,
            artifactKey,
            reason: "vector_tile_available",
          });
          continue;
        }
        const cacheKey = buildArtifactFetchKey(project.project_id, releaseIdentifier, artifactKey, projectVersion);
        artifactFetches.push(
          fetchArtifactOnce(cacheKey, () =>
            getTemporalMilestoneArtifact(project.project_id, releaseIdentifier, artifactKey),
          ).then((payload) => (payload ? [fieldName, payload] : null)),
        );
      }
    }
    for (const [artifactKey, bufferKey] of TEMPORAL_LAZY_BUFFER_ARTIFACT_FIELDS) {
      const currentValue = selectedMilestone.buffer_layers_geojson?.[bufferKey];
      if (!isFeatureCollection(currentValue) && milestoneHasArtifact(selectedMilestone, artifactKey)) {
        if (isHugeTemporalArtifact(selectedMilestone, artifactKey)) {
          relayClientLog("TEMPORAL_GEOJSON_FETCH_SKIPPED_HUGE_ARTIFACT", {
            projectId: project.project_id,
            releaseIdentifier,
            artifactKey,
            reason: "vector_tile_available",
          });
          relayClientLog("TEMPORAL_GEOJSON_FETCH_BLOCKED_VECTOR_TILE_ARTIFACT", {
            projectId: project.project_id,
            releaseIdentifier,
            artifactKey,
            reason: "vector_tile_available",
          });
          continue;
        }
        const cacheKey = buildArtifactFetchKey(project.project_id, releaseIdentifier, artifactKey, projectVersion);
        artifactFetches.push(
          fetchArtifactOnce(cacheKey, () =>
            getTemporalMilestoneArtifact(project.project_id, releaseIdentifier, artifactKey),
          ).then((payload) => (payload ? [`buffer_layers_geojson.${bufferKey}`, payload] : null)),
        );
      }
    }
    if (!artifactFetches.length) {
      return;
    }

    relayClientLog("PROJECT_LAYER_ARTIFACT_LAZY_FETCH", {
      projectId: project.project_id,
      releaseIdentifier,
      artifactCount: artifactFetches.length,
    });
    Promise.all(artifactFetches)
      .then((entries) => {
        if (cancelled) {
          return;
        }
        const loadedEntries = entries.filter((entry): entry is [string, Record<string, unknown>] => entry !== null);
        if (!loadedEntries.length) {
          return;
        }
        setProject((current) => {
          if (!current || current.project_id !== project.project_id) {
            return current;
          }
          return {
            ...current,
            milestones: current.milestones.map((milestone) => {
              if (milestone.release_identifier !== releaseIdentifier) {
                return milestone;
              }
              const nextMilestone: TemporalMilestone = {
                ...milestone,
                buffer_layers_geojson: { ...milestone.buffer_layers_geojson },
              };
              for (const [fieldName, payload] of loadedEntries) {
                if (fieldName.startsWith("buffer_layers_geojson.")) {
                  const bufferKey = fieldName.split(".", 2)[1];
                  nextMilestone.buffer_layers_geojson = {
                    ...nextMilestone.buffer_layers_geojson,
                    [bufferKey]: payload,
                  };
                } else {
                  (nextMilestone as unknown as Record<string, unknown>)[fieldName] = payload;
                }
              }
              return nextMilestone;
            }),
          };
        });
      })
      .catch((error) => {
        if (!cancelled) {
          relayClientLog("PROJECT_LAYER_ARTIFACT_LAZY_FETCH", {
            projectId: project.project_id,
            releaseIdentifier,
            error: error instanceof Error ? error.message : String(error),
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [project?.project_id, project?.updated_at, selectedMilestone?.release_identifier, setProject]);

  useEffect(() => {
    if (!project?.project_id || !selectedMilestone) {
      return;
    }

    const chronologicalMilestones = [...project.milestones].sort(
      (left, right) => temporalMilestoneChronologicalValue(left) - temporalMilestoneChronologicalValue(right),
    );
    const selectedIndex = chronologicalMilestones.findIndex(
      (milestone) => milestone.release_identifier === selectedMilestone.release_identifier,
    );
    relayClientLog("TEMPORAL_CUMULATIVE_ADDITIONS_FETCH_EVALUATED", {
      projectId: project.project_id,
      selectedReleaseIdentifier: selectedMilestone.release_identifier,
      selectedIndex,
      milestoneOrder: chronologicalMilestones.map((milestone) => milestone.release_identifier),
    });
    if (selectedIndex <= 0) {
      return;
    }

    const controller = new AbortController();
    const releasesToFetch = chronologicalMilestones
      .slice(1, selectedIndex + 1)
      .filter((milestone) => milestone.release_identifier !== selectedMilestone.release_identifier)
      .filter((milestone) => !hasFeatureCollectionFeatures(milestone.additions_geojson))
      .filter((milestone) => milestoneHasArtifact(milestone, "additions"))
      .filter((milestone) => {
        if (isHugeTemporalArtifact(milestone, "additions")) {
          relayClientLog("TEMPORAL_CUMULATIVE_ADDITIONS_FETCH_SKIPPED", {
            projectId: project.project_id,
            releaseIdentifier: milestone.release_identifier,
            selectedReleaseIdentifier: selectedMilestone.release_identifier,
            artifactKey: "additions",
            reason: "vector_tile_available",
          });
          return false;
        }
        const fetchKey = `${project.project_id}:${milestone.release_identifier}:additions`;
        if (cumulativeAdditionsFetchesRef.current.has(fetchKey)) {
          return false;
        }
        cumulativeAdditionsFetchesRef.current.add(fetchKey);
        return true;
      });

    if (!releasesToFetch.length) {
      relayClientLog("TEMPORAL_CUMULATIVE_ADDITIONS_FETCH_NOOP", {
        projectId: project.project_id,
        selectedReleaseIdentifier: selectedMilestone.release_identifier,
        selectedIndex,
        reason: "no_missing_included_additions",
      });
      return () => {
        controller.abort();
      };
    }

    relayClientLog("TEMPORAL_CUMULATIVE_ADDITIONS_FETCH_START", {
      projectId: project.project_id,
      selectedReleaseIdentifier: selectedMilestone.release_identifier,
      releaseIdentifiers: releasesToFetch.map((milestone) => milestone.release_identifier),
      artifactKey: "additions",
      reason: "hydrate_all_new_buildings_initial_runtime",
    });

    Promise.all(
      releasesToFetch.map((milestone) =>
        getTemporalMilestoneArtifact(project.project_id, milestone.release_identifier, "additions", {
          signal: controller.signal,
        }).then((payload: Record<string, unknown>) => [milestone.release_identifier, payload] as const),
      ),
    )
      .then((entries) => {
        if (controller.signal.aborted) {
          return;
        }
        setProject((current) => {
          if (!current || current.project_id !== project.project_id) {
            return current;
          }
          const additionsByRelease = new Map(entries);
          return {
            ...current,
            milestones: current.milestones.map((milestone) => {
              const additionsGeojson = additionsByRelease.get(milestone.release_identifier);
              return additionsGeojson ? { ...milestone, additions_geojson: additionsGeojson } : milestone;
            }),
          };
        });
        relayClientLog("TEMPORAL_CUMULATIVE_ADDITIONS_FETCH_DONE", {
          projectId: project.project_id,
          selectedReleaseIdentifier: selectedMilestone.release_identifier,
          releaseIdentifiers: entries.map(([releaseIdentifier]) => releaseIdentifier),
          artifactKey: "additions",
          featureCounts: entries.map(([releaseIdentifier, payload]) => ({
            releaseIdentifier,
            featureCount: ensureFeatureCollection(payload).features.length,
          })),
        });
      })
      .catch((error) => {
        if (controller.signal.aborted) {
          return;
        }
        relayClientLog("TEMPORAL_CUMULATIVE_ADDITIONS_FETCH_FAILED", {
          projectId: project.project_id,
          selectedReleaseIdentifier: selectedMilestone.release_identifier,
          releaseIdentifiers: releasesToFetch.map((milestone) => milestone.release_identifier),
          artifactKey: "additions",
          error: error instanceof Error ? error.message : String(error),
        });
      })
      .finally(() => {
        for (const milestone of releasesToFetch) {
          cumulativeAdditionsFetchesRef.current.delete(`${project.project_id}:${milestone.release_identifier}:additions`);
        }
      });

    return () => {
      controller.abort();
      for (const milestone of releasesToFetch) {
        cumulativeAdditionsFetchesRef.current.delete(`${project.project_id}:${milestone.release_identifier}:additions`);
      }
    };
  }, [project?.project_id, project?.milestones, selectedMilestone, setProject]);

  useEffect(() => {
    if (!project?.project_id || !referenceLayersQuery.isSuccess) {
      return;
    }
    relayClientLog("REFERENCE_LAYER_PANEL_FILTERED_TEMPORAL_REFERENCES", {
      projectId: project.project_id,
      totalReferenceLayers: referenceLayers.length,
      userImportedCount: manualReferenceLayers.length,
      temporalReferenceFilteredCount: 0,
    });
    if (manualReferenceLayers.length === 0) {
      relayClientLog("TEMPORAL_PROJECT_ROUTE_GUARD", {
        projectId: project.project_id,
        state: "manual_reference_layers_absent",
      });
    }
    relayClientLog("TEMPORAL_PROJECT_ROUTE_GUARD", {
      projectId: project.project_id,
      state: "valid_project_loaded",
    });
  }, [
    manualReferenceLayers.length,
    project?.project_id,
    referenceLayers.length,
    referenceLayersQuery.isSuccess,
  ]);

  const referenceLayerPresentation = useMemo(
    () =>
      manualReferenceLayers.map((layer) => ({
        ...layer,
        resolvedDisplayUrl: layer.display_url ? new URL(layer.display_url, backendUrl).toString() : null,
        resolvedPmtilesUrl: layer.pmtiles_url ? new URL(layer.pmtiles_url, backendUrl).toString() : null,
      })),
    [backendUrl, manualReferenceLayers],
  );

  const addedOverlayTimeline = useMemo(
    () =>
      project?.milestones.map((milestone, index) => ({
        releaseIdentifier: milestone.release_identifier,
        status: milestone.status,
        artifacts: temporalArtifactPresentation(backendUrl, milestone),
        additions: ensureFeatureCollection(milestone.additions_geojson),
        buffer10m: ensureFeatureCollection(milestone.buffer_layers_geojson?.["10m"]),
        buffer15m: ensureFeatureCollection(milestone.buffer_layers_geojson?.["15m"]),
        buffer20m: ensureFeatureCollection(milestone.buffer_layers_geojson?.["20m"]),
        cumulativeBuffer10m: mergeFeatureCollections(
          project.milestones
            .slice(0, index + 1)
            .map((item) => ensureFeatureCollection(item.buffer_layers_geojson?.["10m"])),
        ),
        cumulativeBuffer15m: mergeFeatureCollections(
          project.milestones
            .slice(0, index + 1)
            .map((item) => ensureFeatureCollection(item.buffer_layers_geojson?.["15m"])),
        ),
        cumulativeBuffer20m: mergeFeatureCollections(
          project.milestones
            .slice(0, index + 1)
            .map((item) => ensureFeatureCollection(item.buffer_layers_geojson?.["20m"])),
        ),
        automatedCandidate: ensureFeatureCollection(milestone.automated_candidate_footprint_geojson),
        automatedBuildingBlocks: ensureFeatureCollection(milestone.automated_building_blocks_geojson),
        effectiveBuildingBlocks: ensureFeatureCollection(milestone.effective_building_blocks_geojson),
        cumulativeConvexHull: ensureFeatureCollection(milestone.cumulative_convex_hull_geojson),
        cumulativeUnion: ensureFeatureCollection(milestone.cumulative_union_geojson),
        cumulativeGrowthBlocks: ensureFeatureCollection(milestone.cumulative_growth_blocks_geojson),
        cumulativeGrowthEnvelope: ensureFeatureCollection(milestone.cumulative_growth_envelope_geojson),
        manualOverride: ensureFeatureCollection(milestone.manual_override_geojson),
      })) ?? [],
    [backendUrl, project?.milestones],
  );

  useEffect(() => {
    if (!selectedMilestone?.pair_request_hash || hasMilestoneBufferFeatures(selectedMilestone)) {
      return;
    }
    const hasArtifactBackedBuffers =
      milestoneHasArtifact(selectedMilestone, "building_change_buffer_10m") ||
      milestoneHasArtifact(selectedMilestone, "building_change_buffer_15m") ||
      milestoneHasArtifact(selectedMilestone, "building_change_buffer_20m");
    if (hasArtifactBackedBuffers) {
      relayClientLog("RUN_CACHE_POLL_STOPPED_STALE", {
        projectId: project?.project_id ?? null,
        releaseIdentifier: selectedMilestone.release_identifier,
        requestHash: selectedMilestone.pair_request_hash,
        reason: "artifact_backed_temporal_buffers",
      });
      return;
    }

    let cancelled = false;
    getCachedRunResponse(selectedMilestone.pair_request_hash)
      .then((response) => {
        if (cancelled || !response.success || !Object.keys(response.buffer_layers_geojson ?? {}).length) {
          return;
        }
        setProject((current) => {
          if (!current) {
            return current;
          }
          return {
            ...current,
            milestones: current.milestones.map((milestone) =>
              milestone.release_identifier === selectedMilestone.release_identifier
                ? { ...milestone, buffer_layers_geojson: response.buffer_layers_geojson }
                : milestone,
            ),
          };
        });
      })
      .catch(() => {
        // The layer remains unavailable if the underlying pairwise cache is missing.
      });

    return () => {
      cancelled = true;
      relayClientLog("RUN_CACHE_POLL_CANCELLED", {
        projectId: project?.project_id ?? null,
        releaseIdentifier: selectedMilestone.release_identifier,
        requestHash: selectedMilestone.pair_request_hash,
      });
    };
  }, [project?.project_id, selectedMilestone, setProject]);

  useEffect(() => {
    if (!project?.milestones.length) {
      setSelectedMilestoneId(null);
      return;
    }
    if (!selectedMilestoneId || !project.milestones.some((item) => item.release_identifier === selectedMilestoneId)) {
      setSelectedMilestoneId(preferredMilestoneId(project));
    }
  }, [project?.milestones, selectedMilestoneId]);

  useEffect(() => {
    if (!project || !selectedMilestone) {
      onMapPresentationChange(null);
      return;
    }

    const referenceImagery = selectedMilestone.reference_imagery;
    const toReferenceImageryPresentation = (milestone: TemporalMilestone) => {
      const imagery = milestone.reference_imagery;
      const storageStrategy =
        imagery?.storage_strategy ??
        (imagery?.tilejson_url || imagery?.tiles_url_template
          ? "raster_tiles"
          : imagery?.cog_url || imagery?.cog_path
            ? "cog"
            : "image_overlay");
      return {
        releaseIdentifier: milestone.release_identifier,
        storageStrategy,
        tilejsonUrl: resolveBackendUrl(backendUrl, imagery?.tilejson_url),
        tilesUrlTemplate: resolveBackendUrl(backendUrl, imagery?.tiles_url_template),
        cogUrl: imagery?.cog_url
          ? resolveBackendUrl(backendUrl, imagery.cog_url)
          : imagery?.cog_path
            ? buildBackendFileUrl(backendUrl, imagery.cog_path)
            : null,
        imageUrl: imagery?.image_path
          ? buildBackendFileUrl(backendUrl, imagery.image_path)
          : imagery?.image_png_data_url ?? null,
        bounds: hasValidRasterBounds(imagery?.raster_bounds_wgs84) ? imagery?.raster_bounds_wgs84 ?? null : null,
        minzoom: imagery?.minzoom ?? null,
        maxzoom: imagery?.maxzoom ?? null,
        tileSize: imagery?.tile_size ?? 256,
      };
    };
    const referenceImageryUrl = referenceImagery?.image_path
      ? buildBackendFileUrl(backendUrl, referenceImagery.image_path)
      : referenceImagery?.image_png_data_url
        ? referenceImagery.image_png_data_url
        : null;
    const referenceImageryBounds = hasValidRasterBounds(referenceImagery?.raster_bounds_wgs84)
      ? referenceImagery.raster_bounds_wgs84
      : null;
    const selectedMilestoneIndex = project.milestones.findIndex(
      (milestone) => milestone.release_identifier === selectedMilestone.release_identifier,
    );
    const selectedAddedOverlay =
      addedOverlayTimeline.find((item) => item.releaseIdentifier === selectedMilestone.release_identifier) ?? null;
    const referenceImageryTimeline = project.milestones
      .filter((milestone) => milestone.reference_imagery)
      .map((milestone) => toReferenceImageryPresentation(milestone));

    onMapPresentationChange({
      projectId: project.project_id,
      projectUpdatedAt: project.updated_at,
      isHydratingProject: Boolean(temporalProjectBootstrap || loadProjectMutation.isPending || hydratingAoiRef.current),
      projectAoiOverlayVisible:
        activePanel === "aoi" ||
        runProjectMutation.isPending ||
        !project.milestones.some((milestone) => milestone.status === "complete" && milestone.metrics),
      availableMilestoneIds: project.milestones.map((milestone) => milestone.release_identifier),
      availableMilestones: project.milestones.map((milestone) => ({
        releaseIdentifier: milestone.release_identifier,
        date: milestone.release_date ?? null,
      })),
      selectedMilestoneIndex,
      selectedReleaseIdentifier: selectedMilestone.release_identifier,
      selectedMilestoneStatus: selectedMilestone.status,
      selectedMilestone,
      milestones: project.milestones,
      milestoneCount: project.milestones.length,
      referenceImagery: referenceImagery ? toReferenceImageryPresentation(selectedMilestone) : null,
      referenceImageryTimeline,
      addedOverlayTimeline,
      referenceImageryUrl,
      referenceImageryBounds,
      automatedCandidate: ensureFeatureCollection(selectedMilestone.automated_candidate_footprint_geojson),
      automatedBuildingBlocks: ensureFeatureCollection(selectedMilestone.automated_building_blocks_geojson),
      additions: ensureFeatureCollection(selectedMilestone.additions_geojson),
      effectiveBuildingBlocks: ensureFeatureCollection(selectedMilestone.effective_building_blocks_geojson),
      bufferLayers: {
        "10m": ensureFeatureCollection(selectedMilestone.buffer_layers_geojson?.["10m"]),
        "15m": ensureFeatureCollection(selectedMilestone.buffer_layers_geojson?.["15m"]),
        "20m": ensureFeatureCollection(selectedMilestone.buffer_layers_geojson?.["20m"]),
      },
      cumulativeBuffer10m: selectedAddedOverlay?.cumulativeBuffer10m ?? EMPTY_FEATURE_COLLECTION,
      cumulativeBuffer15m: selectedAddedOverlay?.cumulativeBuffer15m ?? EMPTY_FEATURE_COLLECTION,
      cumulativeBuffer20m: selectedAddedOverlay?.cumulativeBuffer20m ?? EMPTY_FEATURE_COLLECTION,
      cumulativeUnion: ensureFeatureCollection(selectedMilestone.cumulative_union_geojson),
      cumulativeConvexHull: ensureFeatureCollection(selectedMilestone.cumulative_convex_hull_geojson),
      cumulativeGrowthBlocks: ensureFeatureCollection(selectedMilestone.cumulative_growth_blocks_geojson),
      cumulativeGrowthEnvelope: ensureFeatureCollection(selectedMilestone.cumulative_growth_envelope_geojson),
      manualOverride: ensureFeatureCollection(selectedMilestone.manual_override_geojson),
      referenceLayers: referenceLayerPresentation,
    });
  }, [activePanel, addedOverlayTimeline, backendUrl, project, selectedMilestone, onMapPresentationChange, referenceLayerPresentation, runProjectMutation.isPending]);

  const releasesById = useMemo(
    () => new Map(releases.map((release) => [release.identifier, release])),
    [releases],
  );

  const selectedReleaseIds = useMemo(
    () =>
      new Set(
        project?.milestones.map((item) => item.release_identifier) ?? [],
      ),
    [project?.milestones],
  );

  const groupedAvailableReleases = useMemo(() => groupReleasesByYear(sortedReleases), [sortedReleases]);

  const syncProjectWithCurrentAoi = (current: TemporalProject): TemporalProject => ({
    ...current,
    aoi_geojson: aoi,
    execution_config: buildExecutionConfig(),
    updated_at: nowIso(),
  });

  const persistProject = async (currentProject: TemporalProject) => {
    const normalizedProject = syncProjectWithCurrentAoi(currentProject);
    setProject(normalizedProject);
    await saveProjectMutation.mutateAsync(normalizedProject);
    return normalizedProject;
  };

  const handleCreateProject = () => {
    setCreateProjectError(null);
    setCreateProjectOpen(true);
  };

  const confirmCreateProject = async () => {
    const name = createProjectName.trim();
    const directory = createProjectDirectory.trim();
    if (!name) {
      setCreateProjectError(t("temporal.project_name_required"));
      return;
    }
    if (!directory) {
      setCreateProjectError(t("temporal.project_directory_required"));
      return;
    }

    setCreateProjectBusy(true);
    try {
      const projectId = buildProjectIdFromName(name);
      const nextProject: TemporalProject = {
        ...emptyProject(aoi, t("temporal.untitled_project"), buildExecutionConfig()),
        project_id: projectId,
        name,
        project_dir: resolveProjectDirectory(projectId, directory),
        created_at: nowIso(),
        updated_at: nowIso(),
      };
      const savedProject = await saveTemporalProject(nextProject);
      setProject({
        ...nextProject,
        updated_at: savedProject.updated_at,
        download_bundle_path: savedProject.download_bundle_path ?? nextProject.download_bundle_path,
      });
      setSelectedProjectId(savedProject.project_id);
      setSelectedMilestoneId(null);
      setReleaseFilter("");
      setProgressMetricsVisible(false);
      onMapPresentationChange(null);
      setTemporalProjectBootstrap(null);
      onWorkflowModeChange("temporal");
      setActivePanel("aoi");
      setCreateProjectOpen(false);
      setCreateProjectError(null);
      setCreateProjectName("");
      setCreateProjectDirectory(DEFAULT_PROJECT_DIRECTORY);
      void queryClient.invalidateQueries({ queryKey: ["temporal-projects"] });
    } catch (error) {
      setCreateProjectError(error instanceof Error ? error.message : t("temporal.create_project_failed"));
    } finally {
      setCreateProjectBusy(false);
    }
  };

  const handleAddMilestone = (release: ReleaseMetadata) => {
    if (!project || selectedReleaseIds.has(release.identifier)) {
      return;
    }
    const nextMilestones = [...project.milestones, createMilestone(release)]
      .sort((left, right) => {
        const leftDate = releasesById.get(left.release_identifier)?.release_date ?? left.release_date ?? "";
        const rightDate = releasesById.get(right.release_identifier)?.release_date ?? right.release_date ?? "";
        return Date.parse(leftDate) - Date.parse(rightDate);
      });
    setProject({
      ...project,
      milestones: nextMilestones,
      updated_at: nowIso(),
    });
    setSelectedReleaseIds(nextMilestones.map((item) => item.release_identifier));
    setSelectedMilestoneId(release.identifier);
  };

  const handleRemoveMilestone = (releaseIdentifier: string) => {
    if (!project) {
      return;
    }
    const nextMilestones = project.milestones.filter((item) => item.release_identifier !== releaseIdentifier);
    setProject({
      ...project,
      milestones: nextMilestones,
      updated_at: nowIso(),
    });
    setSelectedReleaseIds(nextMilestones.map((item) => item.release_identifier));
    if (selectedMilestoneId === releaseIdentifier) {
      setSelectedMilestoneId(nextMilestones.at(-1)?.release_identifier ?? null);
    }
  };

  const handleToggleRelease = (release: ReleaseMetadata) => {
    if (selectedReleaseIds.has(release.identifier)) {
      handleRemoveMilestone(release.identifier);
      return;
    }
    handleAddMilestone(release);
  };

  const handleSave = async () => {
    if (!project) {
      return;
    }
    await persistProject(project);
  };

  const handleRun = async () => {
    if (!project) {
      return;
    }
    const changeThreshold = parseChangeThresholdInput(runChangeThreshold);
    if (changeThreshold === null) {
      setRunChangeThresholdError(t("temporal.change_threshold_error"));
      return;
    }
    setRunChangeThresholdError(null);
    setRunChangeThreshold(String(changeThreshold));
    const savedProject = await persistProject(project);
    const response = await runProjectMutation.mutateAsync({
      projectId: savedProject.project_id,
      changeThreshold,
    });
    if (response.project.milestones.length > 0) {
      setSelectedMilestoneId(response.project.milestones.at(-1)?.release_identifier ?? null);
    }
  };

  const handleDownloadBundle = async () => {
    if (!project?.project_id) {
      return;
    }
    const bundleResult = project.download_bundle_path ?? (await createTemporalProjectExportBundle(project.project_id));
    const bundlePath = typeof bundleResult === "string" ? bundleResult : bundleResult.path;
    const fileName =
      typeof bundleResult === "string" ? bundleResult.split("/").pop() ?? `${project.project_id}.zip` : bundleResult.filename;
    await downloadFileFromUrl(buildBackendFileUrl(backendUrl, bundlePath), fileName);
  };

  const handleDownloadResults = async () => {
    if (!project?.project_id) {
      return;
    }
    const option = RESULTS_EXPORT_OPTIONS.find((item) => item.format === resultsExportFormat);
    if (!option) {
      return;
    }
    setResultsExportError(null);
    setResultsExportBusy(resultsExportFormat);
    try {
      const path = `/api/temporal-projects/${encodeURIComponent(project.project_id)}/exports/results`;
      const filename = `resultats_${project.project_id}.${option.filenameSuffix}`;
      const perimeter = buildResultsExportPerimeter(
        resultsExportPerimeterMode,
        exportDrawnGeometry,
        exportImportedGeometry,
      );
      await downloadFileFromRequest(resolveBackendUrl(backendUrl, path) ?? path, filename, {
        format: resultsExportFormat,
        perimeter,
      });
      clearExportGeometry();
      setResultsExportModalOpen(false);
    } catch (error) {
      selectExportGeometry(null);
      setResultsExportError(error instanceof Error ? error.message : "Export impossible pour ce projet.");
    } finally {
      setResultsExportBusy(null);
    }
  };

  const handleImportOverride = async (geometry: Polygon) => {
    if (!project || !selectedMilestone) {
      return;
    }
    const savedProject = await persistProject(project);
    const response = await importOverrideMutation.mutateAsync({
      projectId: savedProject.project_id,
      releaseIdentifier: selectedMilestone.release_identifier,
      overrideGeojson: geometry as unknown as Record<string, unknown>,
    });
    setSelectedMilestoneId(response.project.milestones.find((item) => item.release_identifier === selectedMilestone.release_identifier)?.release_identifier ?? selectedMilestone.release_identifier);
  };

  const runBusy =
    saveProjectMutation.isPending ||
    runProjectMutation.isPending ||
    importOverrideMutation.isPending ||
    loadProjectMutation.isPending;

  const navItems = [
    { id: "overview", icon: FolderOpen, label: t("temporal.overview") },
    { id: "aoi", icon: Pentagon, label: t("aoi.title") },
    { id: "releases", icon: Layers3, label: t("temporal.releases") },
    { id: "progress", icon: Upload, label: t("temporal.progress") },
    { id: "downloads", icon: FolderOpen, label: t("temporal.downloads") },
  ] as const;

  const activeTitle = navItems.find((item) => item.id === activePanel)?.label ?? t("temporal.progress");
  const visibleMilestoneWarnings = selectedMilestone ? filterProgressWarnings(selectedMilestone.warnings) : [];
  const aoiVertices = draftVertices.length || (aoi ? aoi.coordinates[0].length - 1 : 0);
  const showRunProgress = runProjectMutation.isPending || shouldShowExecutionProgressPanel(runProgress);
  const showMilestoneRunData = progressMetricsVisible && Boolean(selectedMilestone);
  const hasCompletedTemporalResult = Boolean(project?.milestones.some((milestone) => milestone.status === "complete" && milestone.metrics));

  return (
    <>
      <AOIImportModal
        open={aoiImportModalOpen}
        onOpenChange={setAoiImportModalOpen}
        onImport={(geometry) => {
          setAoiFromImport(geometry);
        }}
      />

      <Dialog open={createProjectOpen} onOpenChange={setCreateProjectOpen}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>{t("temporal.create_project_title")}</DialogTitle>
            <DialogDescription>{t("temporal.create_project_description")}</DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-2">
            <div className="space-y-2">
              <label className="label-xs" htmlFor="create-project-name">
                {t("temporal.project_name_label")}
              </label>
              <Input
                id="create-project-name"
                value={createProjectName}
                onChange={(event) => setCreateProjectName(event.target.value)}
                placeholder={t("temporal.project_name_placeholder")}
                className="border-sidebar-border bg-card text-card-foreground"
              />
            </div>
            <div className="space-y-2">
              <label className="label-xs" htmlFor="create-project-directory">
                {t("temporal.save_directory")}
              </label>
              <Input
                id="create-project-directory"
                value={createProjectDirectory}
                onChange={(event) => setCreateProjectDirectory(event.target.value)}
                placeholder="backend/runtime_cache/custom-projects/zone-industrial"
                className="border-sidebar-border bg-card text-card-foreground"
              />
              <p className="text-caption text-muted-foreground">
                {t("temporal.project_saved_as_json")}
              </p>
            </div>
            {createProjectError ? (
              <div className="rounded border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive-foreground">
                {createProjectError}
              </div>
            ) : null}
          </div>
          <DialogFooter>
            <Button variant="outline" className="border-sidebar-border" onClick={() => setCreateProjectOpen(false)} disabled={createProjectBusy}>
              {t("aoi.cancel_button")}
            </Button>
            <Button onClick={() => void confirmCreateProject()} disabled={createProjectBusy}>
              {createProjectBusy ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Plus className="mr-2 h-4 w-4" />}
              {t("temporal.create_project_button")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <WorkspaceShell
        brandLabel={t("header.title")}
        activeTitle={activeTitle}
        navItems={navItems}
        activePanel={activePanel}
        onActivePanelChange={setActivePanel}
        isCollapsed={isCollapsed}
        onToggleCollapse={onToggleCollapse}
        footerContent={
          <div hidden aria-hidden="true">
            <WorkflowParametersPanel
              runtimeConfig={runtimeConfig}
              backendAvailability={backendAvailability}
              backendAvailabilityLoading={backendAvailabilityLoading}
              backendAvailabilityError={backendAvailabilityError}
            />
          </div>
        }
      >
        {activePanel === "overview" ? (
          <div className="space-y-4 p-5">
            <WorkflowSectionCard
              title={t("temporal.overview")}
              description={t("temporal.overview.description")}
              actions={
                <Button variant="ghost" className="text-foreground" onClick={handleCreateProject}>
                  <Plus className="mr-2 h-4 w-4" />
                  {t("temporal.new_button")}
                </Button>
              }
              contentClassName="space-y-3"
            >
                <div className="space-y-2">
                  <label className="label-xs">{t("temporal.saved_projects_heading")}</label>
                  <div className="flex gap-2">
                    <Select
                      value=""
                      onChange={(event) => {
                        const projectId = event.target.value;
                        if (projectId) {
                          const projectSummary = projectsQuery.data?.find((item) => item.project_id === projectId);
                          loadSavedProject(projectId, projectSummary?.project_dir);
                        }
                      }}
                      disabled={projectsQuery.isLoading || loadProjectMutation.isPending}
                      className="border-sidebar-border bg-card text-card-foreground"
                    >
                      <option value="" disabled>
                        {projectsQuery.isLoading ? t("temporal.loading_projects") : t("temporal.select_project")}
                      </option>
                      {(projectsQuery.data ?? []).map((item) => (
                        <option key={item.project_id} value={item.project_id}>
                          {getProjectDisplayName(item, t)}
                        </option>
                      ))}
                    </Select>
                    <Button
                      variant="outline"
                      className="border-sidebar-border bg-card"
                      onClick={() => {
                        const projectId = selectedProjectId || project?.project_id;
                        if (projectId) {
                          const projectSummary = projectsQuery.data?.find((item) => item.project_id === projectId);
                          loadSavedProject(projectId, projectSummary?.project_dir);
                        }
                      }}
                      aria-label={t("temporal.load_project")}
                    >
                      <FolderOpen className="h-4 w-4" />
                    </Button>
                  </div>
                </div>

                <div className="space-y-2">
                  <label className="label-xs">{t("temporal.project_name_label")}</label>
                  <Input
                    value={project?.name ?? ""}
                    onChange={(event) =>
                      setProject((current) =>
                        current
                          ? {
                              ...current,
                              name: event.target.value,
                              updated_at: nowIso(),
                          }
                          : current,
                      )
                    }
                    className="border-sidebar-border bg-card text-card-foreground placeholder:text-muted-foreground"
                  />
                </div>

                <div className="grid grid-cols-2 gap-3">
                  <Button variant="outline" className="border-sidebar-border bg-sidebar" onClick={() => void handleSave()} disabled={!project || runBusy}>
                    {saveProjectMutation.isPending ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Save className="mr-2 h-4 w-4" />}
                    {t("temporal.save_button")}
                  </Button>
                  <Button variant="outline" className="border-sidebar-border bg-sidebar" onClick={() => void handleDownloadBundle()} disabled={!project?.project_id}>
                    <Download className="mr-2 h-4 w-4" />
                    {t("temporal.export_button")}
                  </Button>
                </div>
            </WorkflowSectionCard>
          </div>
        ) : null}

        {activePanel === "releases" ? (
          <div className="flex min-h-full flex-col p-5">
            <WorkflowSectionCard
              title={t("temporal.milestones.title")}
              description={t("temporal.milestones.description")}
              className="flex min-h-0 flex-1 flex-col"
              contentClassName="flex min-h-0 flex-1 flex-col gap-4"
            >
                <div className="space-y-2">
                  <p className="label-xs">{t("temporal.selected_timeline")}</p>
                  {project?.milestones.length ? (
                    <div className="space-y-2">
                      {project.milestones.map((milestone) => {
                        const isSelected = selectedMilestoneId === milestone.release_identifier;
                        return (
                          <div
                            key={milestone.release_identifier}
                            className={cn(
                              "flex items-stretch justify-between rounded-lg border transition",
                              isSelected ? "border-primary bg-primary/10" : "border-sidebar-border bg-sidebar hover:border-sidebar-border",
                            )}
                          >
                            <button
                              type="button"
                              onClick={() => {
                                setSelectedMilestoneId(milestone.release_identifier);
                              }}
                              className="flex min-w-0 flex-1 items-center justify-between gap-3 px-3 py-3 text-left"
                              aria-pressed={isSelected}
                            >
                              <div className="min-w-0">
                                <p className="truncate text-label font-medium text-foreground">{formatReleaseDate(milestone.release_date, locale, t("temporal.unknown_date"))}</p>
                                <p className="truncate text-caption text-foreground">{formatMilestoneIdentifier(milestone, t)}</p>
                              </div>
                              <span className={cn("rounded-full border px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.12em]", milestoneBadgeTone(milestone.status))}>
                                {t(`temporal.milestone_status.${milestone.status}`)}
                              </span>
                            </button>
                            <button
                              type="button"
                              onClick={() => handleRemoveMilestone(milestone.release_identifier)}
                              className="border-l border-sidebar-border px-3 text-foreground transition hover:bg-surface hover:text-foreground"
                              aria-label={`${t("button.remove")} ${milestone.release_identifier}`}
                            >
                              <Trash2 className="h-4 w-4" />
                            </button>
                          </div>
                        );
                      })}
                    </div>
                  ) : (
                    <div className="rounded-lg border border-dashed border-sidebar-border px-4 py-6 text-sm text-foreground">
                      {t("temporal.no_milestones")}
                    </div>
                  )}
                </div>

                <div className="flex min-h-[18rem] flex-1 flex-col gap-2">
                  <p className="label-xs">{t("temporal.available_releases")}</p>
                  <div className="min-h-0 flex-1 space-y-2 overflow-y-auto pr-1">
                    {releasesLoading ? (
                      <div className="flex items-center gap-2 rounded-lg border border-sidebar-border bg-sidebar px-4 py-3 text-sm text-foreground">
                        <Loader2 className="h-4 w-4 animate-spin" />
                        {t("temporal.loading_projects")}
                      </div>
                    ) : releasesError ? (
                      <div className="rounded-lg border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive-foreground">
                        {releasesError}
                      </div>
                    ) : groupedAvailableReleases.size ? (
                      Array.from(groupedAvailableReleases).map(([year, yearReleases]) => (
                        <div key={year} className="space-y-2">
                          <YearGroupRow
                            year={year}
                            isExpanded={expandedYears.has(year)}
                            onToggle={() => {
                              const nextExpanded = new Set(expandedYears);
                              if (nextExpanded.has(year)) {
                                nextExpanded.delete(year);
                              } else {
                                nextExpanded.add(year);
                              }
                              setExpandedYears(nextExpanded);
                            }}
                            releaseCount={yearReleases.length}
                            releaseLabel={t(yearReleases.length === 1 ? "release.single" : "release.plural")}
                          />
                          {expandedYears.has(year) ? (
                            <div className="space-y-2 pl-4">
                              {yearReleases.map((release) => (
                                <ReleaseItem
                                  key={release.identifier}
                                  release={release}
                                  selected={selectedReleaseIds.has(release.identifier)}
                                  disabled={!project}
                                  onSelect={() => handleToggleRelease(release)}
                                  locale={locale}
                                />
                              ))}
                            </div>
                          ) : null}
                        </div>
                      ))
                    ) : (
                      <div className="rounded-lg border border-dashed border-sidebar-border px-4 py-6 text-sm text-foreground">
                        {t("release.not_available")}
                      </div>
                    )}
                  </div>
                </div>

                <div className="space-y-3 rounded-lg border border-sidebar-border bg-sidebar px-4 py-4">
                  <div className="space-y-1">
                    <p className="text-sm font-medium text-foreground">{t("temporal.run_detection_title")}</p>
                    <p className="text-sm text-muted-foreground">{t("temporal.run_detection_description")}</p>
                  </div>
                  <div className="space-y-2">
                    <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                      <label className="text-sm font-medium text-foreground" htmlFor="temporal-change-threshold">
                        {t("temporal.change_threshold")}
                      </label>
                      <Input
                        id="temporal-change-threshold"
                        type="number"
                        min="0.01"
                        max="0.99"
                        step="0.01"
                        value={runChangeThreshold}
                        onChange={(event) => {
                          setRunChangeThreshold(event.target.value);
                          setRunChangeThresholdError(null);
                        }}
                        onBlur={() => {
                          const normalized = parseChangeThresholdInput(runChangeThreshold);
                          if (normalized !== null) {
                            setRunChangeThreshold(String(normalized));
                          }
                        }}
                        aria-invalid={Boolean(runChangeThresholdError)}
                        aria-describedby="temporal-change-threshold-help"
                        className="h-9 w-full sm:w-28"
                      />
                    </div>
                    <p id="temporal-change-threshold-help" className="text-xs text-muted-foreground">
                      {t("temporal.change_threshold_help")}
                    </p>
                    {runChangeThresholdError ? <p className="text-xs text-destructive">{runChangeThresholdError}</p> : null}
                  </div>
                  <div>
                    <Button className="w-full sm:w-auto" onClick={() => void handleRun()} disabled={!project || runBusy || project.milestones.length === 0}>
                      {t("temporal.run_detection_button")}
                    </Button>
                  </div>
                </div>
            </WorkflowSectionCard>
          </div>
        ) : null}

        {activePanel === "aoi" ? (
          <SharedAoiSection
            sectionTitle={t("temporal.draw_aoi")}
            readyText={t("temporal.aoi_ready")}
            emptyText={t("temporal.no_aoi_yet")}
            helpText={t("temporal.aoi_help")}
            drawingSubMode={drawingSubMode}
            drawingMode={drawingMode}
            aoiReady={Boolean(aoi)}
            vertexCount={aoiVertices}
            onSelectMode={setDrawingSubMode}
            onStartDrawing={() => {
              if (drawingSubMode === "rectangle") {
                startRectangleDrawing();
              } else {
                startDrawing();
              }
            }}
            onStartEditing={startEditing}
            onClear={clearAoi}
            onImport={() => setAoiImportModalOpen(true)}
            importLabel={t("aoi.import_aoi")}
          />
        ) : null}

        {activePanel === "progress" ? (
          <div className="space-y-4 p-5">
            <WorkflowSectionCard
              title={t("temporal.progress_title")}
              contentClassName="space-y-3"
            >
                {showRunProgress ? <RunProgressPanel progress={runProgress} /> : null}

                {progressMetricsVisible && !selectedMilestone ? (
                  <div className="rounded-lg border border-dashed border-sidebar-border px-4 py-6 text-sm text-foreground">
                    {t("temporal.select_milestone_prompt")}
                  </div>
                ) : null}

                {showMilestoneRunData && selectedMilestone ? (
                  <>
                    <Card className="border-sidebar-border bg-sidebar shadow-panel">
                      <CardContent className="space-y-6 p-5 lg:p-6">
                        <div className="space-y-3">
                          <TimelineSectionHeader number={1} label={t("temporal.timeline_short")} />
                          <MilestoneMetricCards
                            milestone={selectedMilestone}
                            milestones={project?.milestones ?? []}
                            selectedMilestoneId={selectedMilestoneId}
                            onSelectMilestone={setSelectedMilestoneId}
                            t={t}
                            variant="timeline"
                          />
                        </div>

                        <TemporalLayerControlsBlock controls={temporalLayerControls} t={t} />

                        <div className="space-y-3">
                          <TimelineSectionHeader number={3} label={t("temporal.download_results")} />
                          <div className="relative">
                            <Button
                              type="button"
                              variant="outline"
                              className="h-10 w-full justify-between border-sidebar-border bg-card px-3"
                              onClick={() => {
                                setResultsExportError(null);
                                setResultsExportModalOpen(true);
                              }}
                              disabled={!hasCompletedTemporalResult || Boolean(resultsExportBusy)}
                            >
                              <span className="flex items-center">
                                {resultsExportBusy ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Download className="mr-2 h-4 w-4" />}
                                {t("temporal.download_results")}
                              </span>
                              <ChevronDown className="h-4 w-4" />
                            </Button>
                          </div>
                        </div>
                        {resultsExportError ? (
                          <div className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive-foreground">
                            {resultsExportError}
                          </div>
                        ) : null}

                        {selectedMilestone.error_message ? (
                          <div className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-3 text-sm text-destructive-foreground">
                            {selectedMilestone.error_message}
                          </div>
                        ) : null}

                        {visibleMilestoneWarnings.length ? (
                          <div className="rounded-lg border border-warning/30 bg-warning/10 px-3 py-3 text-sm text-warning-foreground">
                            {visibleMilestoneWarnings.map((message) => (
                              <p key={message}>{message}</p>
                            ))}
                          </div>
                        ) : null}
                      </CardContent>
                    </Card>

                    <div className="grid gap-3 sm:grid-cols-2">
                      <Button
                        variant="outline"
                        className="h-11 w-full border-sidebar-border bg-sidebar justify-start px-4 sm:justify-center"
                        onClick={() => setOverrideModalOpen(true)}
                        disabled={!project || importOverrideMutation.isPending}
                      >
                        {importOverrideMutation.isPending ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Upload className="mr-2 h-4 w-4" />}
                        {t("temporal.import_override")}
                      </Button>
                      <Button
                        variant="outline"
                        className="h-11 w-full border-sidebar-border bg-sidebar justify-start px-4 sm:justify-center"
                        disabled={!project}
                        onClick={() => setReferenceLayerModalOpen(true)}
                      >
                        <Layers3 className="mr-2 h-4 w-4" />
                        {t("reference_layer.add")}
                      </Button>
                    </div>

                    {manualReferenceLayers.length ? (
                      <Card className="border-sidebar-border bg-sidebar shadow-panel">
                        <CardContent className="space-y-3 p-4">
                          <div>
                            <p className="text-sm font-medium text-foreground">{t("reference_layer.layers")}</p>
                            <p className="text-caption text-muted-foreground">{t("reference_layer.layers_help")}</p>
                          </div>
                          <div className="space-y-3">
                            {manualReferenceLayers.map((layer) => (
                              <div key={layer.layer_id} className="rounded-lg border border-sidebar-border bg-card px-3 py-3">
                                <div className="flex items-start justify-between gap-3">
                                  <div className="min-w-0">
                                    <p className="truncate text-sm font-medium text-foreground">{layer.name}</p>
                                    <p className="text-caption text-muted-foreground">{layer.geometry_type} / {layer.storage_strategy}</p>
                                  </div>
                                  <button
                                    type="button"
                                    className="text-muted-foreground transition hover:text-destructive"
                                    onClick={() => deleteReferenceLayerMutation.mutate(layer)}
                                    aria-label={t("common.delete")}
                                  >
                                    <Trash2 className="h-4 w-4" />
                                  </button>
                                </div>
                                <div className="mt-3 flex items-center gap-3">
                                  <label className="flex items-center gap-2 text-caption text-foreground">
                                    <input
                                      type="checkbox"
                                      checked={layer.visible}
                                      onChange={(event) =>
                                        updateReferenceLayerMutation.mutate({ layer, patch: { visible: event.target.checked } })
                                      }
                                      className="h-4 w-4 rounded border-border accent-primary"
                                    />
                                    {t("reference_layer.visible")}
                                  </label>
                                  <label className="flex min-w-0 flex-1 items-center gap-2 text-caption text-muted-foreground">
                                    {t("reference_layer.opacity")}
                                    <span className="w-10 text-right text-foreground">{Math.round(layer.opacity * 100)}%</span>
                                    <input
                                      type="range"
                                      min={0}
                                      max={1}
                                      step={0.05}
                                      value={layer.opacity}
                                      onChange={(event) =>
                                        updateReferenceLayerMutation.mutate({ layer, patch: { opacity: Number(event.target.value) } })
                                      }
                                      className="min-w-0 flex-1"
                                    />
                                  </label>
                                  {Array.isArray(layer.bounds_wgs84) && layer.bounds_wgs84.length >= 4 ? (
                                    <Button
                                      type="button"
                                      variant="outline"
                                      className="h-8 border-sidebar-border bg-sidebar"
                                      onClick={() => requestMapFocusToReferenceLayer(layer.bounds_wgs84 as [number, number, number, number])}
                                    >
                                      {t("reference_layer.zoom")}
                                    </Button>
                                  ) : null}
                                </div>
                                {layer.warnings.length ? (
                                  <div className="mt-3 space-y-1 text-caption text-amber-700 dark:text-amber-300">
                                    {layer.warnings.map((warning) => (
                                      <p key={warning}>{warning}</p>
                                    ))}
                                  </div>
                                ) : null}
                              </div>
                            ))}
                          </div>
                        </CardContent>
                      </Card>
                    ) : null}
                  </>
                ) : null}
            </WorkflowSectionCard>
          </div>
        ) : null}

        {activePanel === "downloads" ? (
          <div className="space-y-4 p-5">
            <WorkflowSectionCard
              title={t("temporal.downloads")}
              description={t("temporal.downloads.description")}
              contentClassName="space-y-3"
            >
                {!project?.project_id ? (
                  <div className="rounded-lg border border-dashed border-sidebar-border px-4 py-6 text-sm text-foreground">
                    {t("temporal.downloads.empty")}
                  </div>
                ) : (
                  <Button variant="outline" className="border-sidebar-border bg-sidebar" onClick={() => void handleDownloadBundle()}>
                    <Download className="mr-2 h-4 w-4" />
                    {t("temporal.download_bundle")}
                  </Button>
                )}
            </WorkflowSectionCard>
          </div>
        ) : null}

        {(projectsQuery.error || loadProjectMutation.error || saveProjectMutation.error || runProjectMutation.error || importOverrideMutation.error || importReferenceLayerMutation.error || updateReferenceLayerMutation.error || deleteReferenceLayerMutation.error) ? (
          <div className="p-5 pt-0">
            <div className="rounded-lg border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive-foreground">
              <div className="flex items-start gap-2">
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                <div>
                  {formatErrorMessage(
                      projectsQuery.error ??
                      loadProjectMutation.error ??
                      saveProjectMutation.error ??
                      runProjectMutation.error ??
                      importOverrideMutation.error ??
                      importReferenceLayerMutation.error ??
                      updateReferenceLayerMutation.error ??
                      deleteReferenceLayerMutation.error,
                    t("common.unexpected_error"),
                  )}
                </div>
              </div>
            </div>
          </div>
        ) : null}
      </WorkspaceShell>

      <GeometryImportModal
        open={overrideModalOpen}
        onOpenChange={setOverrideModalOpen}
        title={selectedMilestone ? `${t("temporal.manual_override_for")} ${formatReleaseDate(selectedMilestone.release_date, locale, t("temporal.unknown_date"))}` : t("temporal.import_manual_override")}
        description={t("temporal.import_manual_override_description")}
        onImport={handleImportOverride}
      />
      <GeometryImportModal
        open={resultsExportImportOpen}
        onOpenChange={setResultsExportImportOpen}
        title="Importer une zone"
        description="Importer une zone GeoJSON, WKT, KML, KMZ, GPX ou Shapefile ZIP."
        onImport={(geometry) => {
          setExportImportedGeometry(geometry);
          setResultsExportPerimeterMode("imported");
        }}
      />
      <Dialog
        open={resultsExportModalOpen}
        onOpenChange={(open) => {
          setResultsExportModalOpen(open);
          if (!open) {
            clearExportGeometry();
          }
        }}
      >
        <DialogContent className="max-w-xl">
          <DialogHeader>
            <DialogTitle>Télécharger les résultats</DialogTitle>
            <DialogDescription>Sélectionnez le périmètre temporaire et le format d’export.</DialogDescription>
          </DialogHeader>
          <div className="space-y-5">
            <fieldset className="space-y-2">
              <legend className="mb-2 text-sm font-semibold text-foreground">Périmètre d’export</legend>
              {([
                ["project_aoi", "Tout le projet", "Exporter tous les résultats dans l’AOI complète du projet."],
                ["drawn", "Zone spécifique dans le projet", "Dessiner une zone à l’intérieur de l’AOI du projet."],
                ["imported", "Importer une zone", "Importer une zone GeoJSON, WKT, KML, KMZ, GPX ou Shapefile ZIP."],
              ] as const).map(([mode, label, description]) => (
                <label key={mode} className="flex cursor-pointer gap-3 rounded border border-sidebar-border p-3">
                  <input
                    type="radio"
                    name="export-perimeter"
                    checked={resultsExportPerimeterMode === mode}
                    onChange={() => {
                      setResultsExportPerimeterMode(mode);
                      selectExportGeometry(mode === "project_aoi" ? null : mode);
                    }}
                  />
                  <span><span className="block text-sm font-medium">{label}</span><span className="block text-xs text-muted-foreground">{description}</span></span>
                </label>
              ))}
            </fieldset>
            <label className="block space-y-2 text-sm font-medium">Format
              <Select value={resultsExportFormat} onChange={(event) => setResultsExportFormat(event.target.value as ResultsExportFormat)}>
                {RESULTS_EXPORT_OPTIONS.map((option) => <option key={option.format} value={option.format}>{option.label}</option>)}
              </Select>
            </label>
            {resultsExportPerimeterMode === "drawn" ? (
              <div className="space-y-2">
                <p className="text-sm font-medium text-foreground">Zone spécifique</p>
                <div className="flex gap-2">
                  <Button type="button" variant="outline" onClick={() => { startExportDrawing("polygon"); setResultsExportModalOpen(false); }}>Dessiner un polygone</Button>
                  <Button type="button" variant="outline" onClick={() => { startExportDrawing("rectangle"); setResultsExportModalOpen(false); }}>Dessiner un rectangle</Button>
                </div>
              </div>
            ) : null}
            {resultsExportPerimeterMode === "imported" ? (
              <Button type="button" variant="outline" onClick={() => setResultsExportImportOpen(true)}><Upload className="mr-2 h-4 w-4" />Importer une zone</Button>
            ) : null}
            {resultsExportPerimeterMode === "drawn" && !exportDrawnGeometry ? <p className="text-sm text-muted-foreground">Aucune zone dessinée.</p> : null}
            {resultsExportPerimeterMode === "imported" && !exportImportedGeometry ? <p className="text-sm text-muted-foreground">Aucune zone importée.</p> : null}
            {resultsExportPerimeterMode !== "project_aoi" && exportGeometry ? <p className="text-sm text-green-600">Zone d’export valide.</p> : null}
            {resultsExportError ? <p className="text-sm text-destructive">{resultsExportError}</p> : null}
          </div>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => {
                clearExportGeometry();
                setResultsExportModalOpen(false);
              }}
            >
              Annuler
            </Button>
            <Button type="button" onClick={() => void handleDownloadResults()} disabled={Boolean(resultsExportBusy) || !exportDownloadEnabled}>
              {resultsExportBusy ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Download className="mr-2 h-4 w-4" />}Télécharger
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      {exportDrawingPhase === "drawing_polygon" || exportDrawingPhase === "drawing_rectangle" ? (
        <div className="fixed bottom-6 left-1/2 z-50 w-[min(92vw,560px)] -translate-x-1/2 rounded border border-primary/40 bg-card px-4 py-3 shadow-panel">
          <div className="flex items-center justify-between gap-4">
            <p className="text-sm text-foreground">
              Dessinez la zone d’export sur la carte. Fermez le polygone sur son premier point ou cliquez sur le coin opposé.
            </p>
            <Button type="button" variant="outline" onClick={stopDrawing} aria-label="Annuler le dessin de la zone d’export">
              Annuler
            </Button>
          </div>
        </div>
      ) : null}
      <ReferenceLayerImportModal
        open={referenceLayerModalOpen}
        projectId={project?.project_id ?? null}
        onOpenChange={setReferenceLayerModalOpen}
        onPreflight={(file, scope) => {
          if (!project?.project_id) {
            throw new Error(t("reference_layer.no_project"));
          }
          return preflightReferenceLayer(project.project_id, file, scope);
        }}
        onImport={(file, name, scope, strategy) =>
          importReferenceLayerMutation.mutateAsync({
            file,
            name,
            scope,
            strategy,
          })
        }
      />
    </>
  );
}
