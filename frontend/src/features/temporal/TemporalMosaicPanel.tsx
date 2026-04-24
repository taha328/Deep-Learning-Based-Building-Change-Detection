import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { FeatureCollection, Polygon } from "geojson";
import {
  AlertTriangle,
  Check,
  ChevronDown,
  Clock3,
  Download,
  FolderOpen,
  Layers3,
  Loader2,
  PenSquare,
  Pentagon,
  Plus,
  Save,
  Sparkles,
  Trash2,
  Upload,
} from "lucide-react";

import type {
  BackendAvailability,
  ReleaseMetadata,
  TemporalMilestone,
  TemporalProject,
  TemporalProjectValidationResponse,
} from "@/api/contracts";
import {
  getCachedRunResponse,
  getTemporalProject,
  importTemporalOverride,
  listTemporalProjects,
  runTemporalProject,
  saveTemporalProject,
  validateTemporalProject,
} from "@/api/gradio";
import { useAppStore } from "@/app/store";
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
import { Separator } from "@/components/ui/separator";
import type { FrontendRuntimeConfig } from "@/lib/env";
import { buildGradioFileUrl } from "@/lib/gradio-files";
import { cn, formatNumber } from "@/lib/utils";
import { downloadFileFromUrl } from "@/lib/download";
import { useI18n } from "@/lib/i18n";
import { getProjectDisplayName } from "@/lib/project-summary";
import { createActiveRunProgress, createCompletedRunProgress, createErrorRunProgress } from "@/lib/run-progress";
import { AOIImportModal } from "@/features/aoi/AOIImportModal";
import { RunProgressPanel } from "@/features/results/RunProgressPanel";
import { GeometryImportModal } from "@/features/temporal/GeometryImportModal";
import type { TemporalMapPresentation } from "@/features/temporal/types";
import { SharedAoiSection } from "@/features/workspace/SharedAoiSection";
import { WorkflowParametersPanel } from "@/features/workspace/WorkflowParametersPanel";
import { WorkflowSectionCard } from "@/features/workspace/WorkflowSectionCard";
import { WorkspaceShell } from "@/features/workspace/WorkspaceShell";
import type { WorkflowSectionId } from "@/features/workspace/workflowSections";

const DEFAULT_PROJECT_DIRECTORY = "/Users/tahaelouali/Desktop/Building_change_app/backend/runtime_cache";

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
  "BANDON MTGCDNet applied an MPS slide-window compatibility patch to the configured crop/stride.";

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

function emptyProject(aoi: Polygon | null, projectName: string): TemporalProject {
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
    warnings: [],
    validation_blocking_errors: [],
    download_bundle_path: null,
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

function ensureFeatureCollection(value: Record<string, unknown> | null | undefined): FeatureCollection {
  if (value && value.type === "FeatureCollection" && Array.isArray(value.features)) {
    return value as unknown as FeatureCollection;
  }
  return EMPTY_FEATURE_COLLECTION;
}

function hasFeatureCollectionFeatures(value: Record<string, unknown> | null | undefined): boolean {
  return ensureFeatureCollection(value).features.length > 0;
}

function hasMilestoneBufferFeatures(milestone: TemporalMilestone): boolean {
  return (
    hasFeatureCollectionFeatures(milestone.buffer_layers_geojson?.["10m"]) ||
    hasFeatureCollectionFeatures(milestone.buffer_layers_geojson?.["15m"]) ||
    hasFeatureCollectionFeatures(milestone.buffer_layers_geojson?.["20m"])
  );
}

function hasValidRasterBounds(bounds: number[] | null | undefined): bounds is [number, number, number, number] {
  return Array.isArray(bounds) && bounds.length >= 4 && bounds.every((value) => Number.isFinite(value));
}

function milestoneHasMapPresentation(milestone: TemporalMilestone): boolean {
  const hasReferenceImagery =
    Boolean(milestone.reference_imagery?.image_png_data_url || milestone.reference_imagery?.image_path) &&
    hasValidRasterBounds(milestone.reference_imagery?.raster_bounds_wgs84);

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

function MilestonePrimaryMetricCard({
  label,
  value,
  toneClassName,
}: {
  label: string;
  value: string;
  toneClassName?: string;
}) {
  return (
    <div
      className={cn(
        "rounded-lg border border-sidebar-border/80 bg-surface/70 px-3.5 py-3",
        toneClassName,
      )}
    >
      <p className="text-label-muted">{label}</p>
      <p className="mt-2 text-base font-semibold leading-6 tabular-nums text-foreground">{value}</p>
    </div>
  );
}

function MilestoneMetaRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-start justify-between gap-4">
      <dt className="text-label-muted">{label}</dt>
      <dd className="max-w-[13rem] text-right text-label leading-5">{value}</dd>
    </div>
  );
}

function MilestoneSecondaryMetricRow({
  label,
  value,
  emphasized = false,
}: {
  label: string;
  value: string;
  emphasized?: boolean;
}) {
  return (
    <div className="rounded-lg border border-sidebar-border/80 bg-surface/70 px-3.5 py-3">
      <dt className="text-label-muted">{label}</dt>
      <dd
        className={cn(
          "mt-2 text-base font-semibold leading-6 tabular-nums text-foreground",
          emphasized ? "inline-flex items-center rounded-full border border-border/80 bg-background px-2.5 py-1 text-label tracking-normal" : "",
        )}
      >
        {value}
      </dd>
    </div>
  );
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
}: TemporalMosaicPanelProps) {
  const queryClient = useQueryClient();
  const hydratingAoiRef = useRef(false);
  const [validation, setValidation] = useState<TemporalProjectValidationResponse | null>(null);
  const [selectedMilestoneId, setSelectedMilestoneId] = useState<string | null>(null);
  const [releaseFilter, setReleaseFilter] = useState("");
  const [expandedYears, setExpandedYears] = useState<Set<number>>(new Set());
  const [activePanel, setActivePanel] = useState<WorkflowSectionId>("overview");
  const [aoiImportModalOpen, setAoiImportModalOpen] = useState(false);
  const [overrideModalOpen, setOverrideModalOpen] = useState(false);
  const [createProjectOpen, setCreateProjectOpen] = useState(false);
  const [createProjectName, setCreateProjectName] = useState("");
  const [createProjectDirectory, setCreateProjectDirectory] = useState(DEFAULT_PROJECT_DIRECTORY);
  const [createProjectError, setCreateProjectError] = useState<string | null>(null);
  const [createProjectBusy, setCreateProjectBusy] = useState(false);
  const [selectedProjectId, setSelectedProjectId] = useState("");
  const [progressMetricsVisible, setProgressMetricsVisible] = useState(false);

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
  const project = useAppStore((state) => state.temporalProject);
  const setProject = useAppStore((state) => state.setTemporalProject);
  const temporalProjectBootstrap = useAppStore((state) => state.temporalProjectBootstrap);
  const setTemporalProjectBootstrap = useAppStore((state) => state.setTemporalProjectBootstrap);
  const setSelectedReleaseIds = useAppStore((state) => state.setSelectedReleaseIds);
  const runProgress = useAppStore((state) => state.runProgress);
  const setRunProgress = useAppStore((state) => state.setRunProgress);
  const setIsRunning = useAppStore((state) => state.setIsRunning);
  const previousAoiRef = useRef<Polygon | null>(aoi);
  const latestProjectLoadRef = useRef<string | null>(null);

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
      setValidation(null);
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

  const validateProjectMutation = useMutation({
    mutationFn: validateTemporalProject,
    onSuccess: (response) => {
      setValidation(response);
      setProject(response.project);
    },
  });

  const runProjectMutation = useMutation({
    mutationFn: async (projectId: string) => {
      setIsRunning(true);
      setRunProgress({
        ...createActiveRunProgress(),
        phase: "running",
        percent: 5,
        stageLabel: "Metadata",
        detail: "Temporal mosaic timeline run started.",
      });
      return runTemporalProject(projectId);
    },
    onSuccess: (response) => {
      setIsRunning(false);
      setRunProgress(createCompletedRunProgress());
      setProgressMetricsVisible(true);
      setProject(response.project);
      setValidation(null);
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
      setValidation(null);
      void queryClient.invalidateQueries({ queryKey: ["temporal-projects"] });
    },
  });

  useEffect(() => {
    if (workflowMode !== "temporal") {
      return;
    }
    if (project || temporalProjectBootstrap || loadProjectMutation.isPending) {
      return;
    }
    setProgressMetricsVisible(false);
    setProject(emptyProject(aoi, t("temporal.untitled_project")));
  }, [aoi, project, temporalProjectBootstrap, loadProjectMutation.isPending, workflowMode, t]);

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
    setValidation(null);
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

  useEffect(() => {
    if (!selectedMilestone?.pair_request_hash || hasMilestoneBufferFeatures(selectedMilestone)) {
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
    };
  }, [selectedMilestone?.pair_request_hash, selectedMilestone?.release_identifier, selectedMilestone?.buffer_layers_geojson, setProject]);

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
    const referenceImageryUrl = referenceImagery?.image_png_data_url
      ? referenceImagery.image_png_data_url
      : referenceImagery?.image_path
        ? buildGradioFileUrl(backendUrl, referenceImagery.image_path)
        : null;
    const referenceImageryBounds = hasValidRasterBounds(referenceImagery?.raster_bounds_wgs84)
      ? referenceImagery.raster_bounds_wgs84
      : null;

    onMapPresentationChange({
      selectedReleaseIdentifier: selectedMilestone.release_identifier,
      selectedMilestoneStatus: selectedMilestone.status,
      milestoneCount: project.milestones.length,
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
      cumulativeUnion: ensureFeatureCollection(selectedMilestone.cumulative_union_geojson),
      cumulativeConvexHull: ensureFeatureCollection(selectedMilestone.cumulative_convex_hull_geojson),
      cumulativeGrowthBlocks: ensureFeatureCollection(selectedMilestone.cumulative_growth_blocks_geojson),
      cumulativeGrowthEnvelope: ensureFeatureCollection(selectedMilestone.cumulative_growth_envelope_geojson),
      manualOverride: ensureFeatureCollection(selectedMilestone.manual_override_geojson),
    });
  }, [backendUrl, project, selectedMilestone, onMapPresentationChange]);

  const releasesById = useMemo(
    () => new Map(releases.map((release) => [release.identifier, release])),
    [releases],
  );

  const selectedReleaseIds = useMemo(
    () => new Set(project?.milestones.map((item) => item.release_identifier) ?? []),
    [project?.milestones],
  );

  const groupedAvailableReleases = useMemo(() => groupReleasesByYear(sortedReleases), [sortedReleases]);

  const syncProjectWithCurrentAoi = (current: TemporalProject): TemporalProject => ({
    ...current,
    aoi_geojson: aoi,
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
        ...emptyProject(aoi, t("temporal.untitled_project")),
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
      setValidation(null);
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
    setValidation(null);
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
    setValidation(null);
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

  const handleValidate = async () => {
    if (!project) {
      return;
    }
    const savedProject = await persistProject(project);
    const response = await validateProjectMutation.mutateAsync(savedProject);
    setProject(response.project);
  };

  const handleRun = async () => {
    if (!project) {
      return;
    }
    const savedProject = await persistProject(project);
    const response = await runProjectMutation.mutateAsync(savedProject.project_id);
    if (response.project.milestones.length > 0) {
      setSelectedMilestoneId(response.project.milestones.at(-1)?.release_identifier ?? null);
    }
  };

  const handleDownloadBundle = async () => {
    if (!project?.download_bundle_path) {
      return;
    }
    const fileName = project.download_bundle_path.split("/").pop() ?? `${project.project_id}.zip`;
    await downloadFileFromUrl(buildGradioFileUrl(backendUrl, project.download_bundle_path), fileName);
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
    validateProjectMutation.isPending ||
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
  const visibleValidationWarnings = validation ? filterProgressWarnings(validation.warnings) : [];
  const visibleMilestoneWarnings = selectedMilestone ? filterProgressWarnings(selectedMilestone.warnings) : [];
  const aoiVertices = draftVertices.length || (aoi ? aoi.coordinates[0].length - 1 : 0);
  const showRunProgress = runProjectMutation.isPending || runProgress.phase !== "idle";
  const showMilestoneRunData = progressMetricsVisible && Boolean(selectedMilestone);
  const milestonePrimaryMetrics = selectedMilestone
    ? [
        {
          label: t("temporal.metric.added_area"),
          value: formatArea(selectedMilestone.metrics?.added_area_m2, t("release.not_available")),
          toneClassName: "border-primary/20 bg-primary/[0.06]",
        },
        {
          label: t("temporal.metric.total_built_up_area"),
          value: formatArea(selectedMilestone.metrics?.total_area_m2, t("release.not_available")),
          toneClassName: "border-accent/20 bg-accent/[0.06]",
        },
        {
          label: t("temporal.metric.additions_features"),
          value: formatNumber(selectedMilestone.metrics?.additions_feature_count ?? 0),
          toneClassName: "border-secondary bg-secondary/60",
        },
      ]
    : [];
  const milestoneSecondaryMetrics = selectedMilestone
    ? [
        {
          label: t("temporal.metric.added_blocks"),
          value: formatNumber(selectedMilestone.metrics?.added_block_count ?? 0),
        },
        {
          label: t("temporal.metric.cumulative_blocks"),
          value: formatNumber(selectedMilestone.metrics?.cumulative_block_count ?? 0),
        },
        {
          label: t("temporal.metric.added_block_area"),
          value: formatArea(selectedMilestone.metrics?.added_block_area_m2, t("release.not_available")),
        },
        {
          label: t("temporal.metric.cumulative_block_area"),
          value: formatArea(selectedMilestone.metrics?.cumulative_block_area_m2, t("release.not_available")),
        },
        {
          label: t("temporal.metric.growth_envelope_area"),
          value: formatArea(selectedMilestone.metrics?.growth_envelope_area_m2, t("release.not_available")),
        },
        {
          label: t("temporal.metric.building_level_detail"),
          value: selectedMilestone.metrics?.building_level_available ? t("temporal.metric.available") : t("temporal.metric.footprint_only"),
          emphasized: true,
        },
      ]
    : [];

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
                placeholder="/Users/tahaelouali/Desktop/Building_change_app/backend/runtime_cache/custom-projects/zone-industrial"
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
                  <Button variant="outline" className="border-sidebar-border bg-sidebar" onClick={() => void handleDownloadBundle()} disabled={!project?.download_bundle_path}>
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
                                <p className="truncate text-caption text-foreground">{milestone.release_identifier}</p>
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
                    <p className="text-sm font-medium text-foreground">{t("temporal.extend_and_rerun")}</p>
                    <p className="text-sm text-muted-foreground">{t("temporal.extend_and_rerun_description")}</p>
                  </div>
                  <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                    <Button
                      variant="outline"
                      className="border-sidebar-border bg-sidebar"
                      onClick={() => void handleValidate()}
                      disabled={!project || runBusy}
                    >
                      {validateProjectMutation.isPending ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Clock3 className="mr-2 h-4 w-4" />}
                      {t("temporal.validate_timeline")}
                    </Button>
                    <Button onClick={() => void handleRun()} disabled={!project || runBusy || project.milestones.length === 0}>
                      {runProjectMutation.isPending ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Sparkles className="mr-2 h-4 w-4" />}
                      {t("temporal.run_timeline")}
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
              description={t("temporal.progress_description")}
              contentClassName="space-y-3"
            >
                {validation ? (
                  <div className="space-y-3 rounded-lg border border-sidebar-border bg-sidebar px-4 py-4">
                    <div className="flex items-center justify-between">
                      <span className="text-sm font-medium text-foreground">
                        {validation.valid ? t("status.validation_passed") : t("status.validation_needs_attention")}
                      </span>
                      <span className="text-xs uppercase tracking-[0.12em] text-foreground">
                        {formatNumber(validation.estimated_total_tiles)} {t("temporal.total_tiles")}
                      </span>
                    </div>
                    {validation.blocking_errors.length ? (
                      <div className="space-y-2 rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-3 text-sm text-destructive-foreground">
                        {validation.blocking_errors.map((message) => (
                          <p key={message}>{message}</p>
                        ))}
                      </div>
                    ) : null}
                    {visibleValidationWarnings.length ? (
                      <div className="space-y-2 rounded-lg border border-warning/30 bg-warning/10 px-3 py-3 text-sm text-warning-foreground">
                        {visibleValidationWarnings.map((message) => (
                          <p key={message}>{message}</p>
                        ))}
                      </div>
                    ) : null}
                  </div>
                ) : null}

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
                        <div className="flex justify-end">
                          <span className={cn("rounded-full border px-3 py-1.5 label-xs font-semibold uppercase", milestoneBadgeTone(selectedMilestone.status))}>
                            {t(`temporal.milestone_status.${selectedMilestone.status}`)}
                          </span>
                        </div>

                        <dl className="space-y-2 rounded-lg border border-sidebar-border/80 bg-surface/60 px-3.5 py-3">
                          <MilestoneMetaRow
                            label={t("temporal.metrics_date_label")}
                            value={formatReleaseDate(selectedMilestone.release_date, locale, t("temporal.unknown_date"))}
                          />
                          <MilestoneMetaRow
                            label={t("temporal.metrics_identifier_label")}
                            value={selectedMilestone.release_identifier}
                          />
                        </dl>

                        <div className="grid auto-rows-fr gap-4 sm:grid-cols-2 lg:grid-cols-3">
                          {milestonePrimaryMetrics.map((metric) => (
                            <MilestonePrimaryMetricCard
                              key={metric.label}
                              label={metric.label}
                              value={metric.value}
                              toneClassName={metric.toneClassName}
                            />
                          ))}
                        </div>

                        <Separator className="bg-sidebar-border" />

                        <div className="space-y-4">
                          <div className="flex flex-col gap-1.5 sm:flex-row sm:items-baseline sm:justify-between">
                            <p className="text-label font-semibold tracking-tight text-foreground">{t("temporal.metrics_spatial_analysis")}</p>
                            <span className="text-caption">
                              {t("temporal.metrics_secondary_summary")}
                            </span>
                          </div>
                          <dl className="grid gap-3 lg:grid-cols-2">
                            {milestoneSecondaryMetrics.map((metric) => (
                              <MilestoneSecondaryMetricRow
                                key={metric.label}
                                label={metric.label}
                                value={metric.value}
                                emphasized={metric.emphasized}
                              />
                            ))}
                          </dl>
                        </div>

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
                        disabled={!selectedMilestone.artifacts.length}
                        onClick={() => setSelectedMilestoneId(selectedMilestone.release_identifier)}
                      >
                        <Layers3 className="mr-2 h-4 w-4" />
                        {t("temporal.map_ready")}
                      </Button>
                    </div>
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
                {!project?.download_bundle_path ? (
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

        {(projectsQuery.error || loadProjectMutation.error || saveProjectMutation.error || validateProjectMutation.error || runProjectMutation.error || importOverrideMutation.error) ? (
          <div className="p-5 pt-0">
            <div className="rounded-lg border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive-foreground">
              <div className="flex items-start gap-2">
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                <div>
                  {formatErrorMessage(
                    projectsQuery.error ??
                      loadProjectMutation.error ??
                      saveProjectMutation.error ??
                      validateProjectMutation.error ??
                      runProjectMutation.error ??
                      importOverrideMutation.error,
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
    </>
  );
}
