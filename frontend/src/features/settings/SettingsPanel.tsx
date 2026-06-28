import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  ChevronDown,
  Clock3,
  Download,
  FolderOpen,
  Info,
  Layers,
  Loader2,
  Pentagon,
  Play,
  Plus,
} from "lucide-react";

import type {
  BackendAvailability,
  ReleaseMetadata,
  TemporalProjectSummary,
  TemporalMilestone,
  TemporalProject,
  ValidationRequest,
} from "@/api/contracts";
import { createRunExportBundle, getCachedRunResponse, getTemporalProject, listTemporalProjects, runDetection, validateRequest } from "@/api/client";
import { useAppStore } from "@/app/store";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Progress } from "@/components/ui/progress";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { downloadFileFromUrl } from "@/lib/download";
import { type FrontendRuntimeConfig } from "@/lib/env";
import { buildBackendFileUrl } from "@/lib/backend-files";
import { getProjectDisplayName } from "@/lib/project-summary";
import { useI18n } from "@/lib/i18n";
import { saveTemporalProject } from "@/api/client";
import {
  PIPELINE_STAGES,
  createActiveRunProgress,
  createCompletedRunProgress,
  createErrorRunProgress,
  formatRunStatus,
  getStageState,
} from "@/lib/run-progress";
import { cn, formatNumber } from "@/lib/utils";
import { AOIImportModal } from "@/features/aoi/AOIImportModal";
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

function parseBufferDistances(input: string): number[] {
  return input
    .split(",")
    .map((value) => Number(value.trim()))
    .filter((value) => Number.isFinite(value) && value > 0);
}

function buildRequest(
  state: ReturnType<typeof useAppStore.getState>,
  runtimeConfig: FrontendRuntimeConfig,
): ValidationRequest | null {
  if (!state.aoi || !state.settings.t1Release || !state.settings.t2Release) {
    return null;
  }

  const request: ValidationRequest = {
    aoi_geojson: state.aoi,
    t1_release: state.settings.t1Release,
    t2_release: state.settings.t2Release,
    mode: state.settings.mode,
    merge_close_gap_m: state.settings.mergeCloseGapM,
    building_block_gap_m: state.settings.buildingBlockGapM,
    buffer_distances_m: parseBufferDistances(state.settings.bufferDistancesText),
    keep_disjoint_buffer_parts_separate: true,
  };

  if (runtimeConfig.supportsRequestBackendSelection) {
    request.inference_backend = state.settings.modelBackend;
  }

  return request;
}

function requestKey(request: ValidationRequest | null): string | null {
  return request ? JSON.stringify(request) : null;
}

type PanelId = WorkflowSectionId;

function parseReleaseTime(release: ReleaseMetadata): number {
  const timestamp = Date.parse(release.release_date);
  return Number.isFinite(timestamp) ? timestamp : 0;
}

function getArchiveCode(release: ReleaseMetadata): string {
  const identifierMatch = release.identifier.match(/R\d+$/i);
  if (identifierMatch) {
    return identifierMatch[0].toUpperCase();
  }
  const labelMatch = release.label.match(/R\d+$/i);
  return labelMatch?.[0].toUpperCase() ?? "Archive";
}

function formatReleaseDate(release: ReleaseMetadata, locale: string, format: "long" | "short" = "long"): string {
  const date = new Date(release.release_date);
  if (Number.isNaN(date.getTime())) {
    return release.label;
  }

  return new Intl.DateTimeFormat(locale, {
    day: "2-digit",
    month: format === "long" ? "short" : "numeric",
    year: "numeric",
  }).format(date);
}

function formatReleaseMonth(release: ReleaseMetadata, locale: string, fallback: string): string {
  const date = new Date(release.release_date);
  if (Number.isNaN(date.getTime())) {
    return fallback;
  }

  return new Intl.DateTimeFormat(locale, {
    month: "long",
    year: "numeric",
  }).format(date);
}

function describeArchive(release: ReleaseMetadata, archiveLabel: string): string {
  return `${archiveLabel} ${getArchiveCode(release)}`;
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

function projectExistsInList(projects: TemporalProjectSummary[] | undefined, projectId: string): boolean {
  return Boolean(projectId && projects?.some((item) => item.project_id === projectId));
}

function formatArea(areaM2: number | undefined | null, fallback: string): string {
  if (!areaM2 || areaM2 <= 0) {
    return fallback;
  }
  if (areaM2 >= 1_000_000) {
    return `${formatNumber(areaM2 / 1_000_000, 2)} km²`;
  }
  return `${formatNumber(areaM2, 0)} m²`;
}

function SectionTitle({ title, description }: { title: string; description?: string }) {
  return (
    <div className="space-y-1.5">
      <h3 className="label-xs font-semibold block mb-1">{title}</h3>
      {description ? <p className="text-label text-muted-foreground leading-relaxed">{description}</p> : null}
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
    try {
      return JSON.stringify(error);
    } catch {
      return fallback;
    }
  }
  return fallback;
}

function localizeRunProgressDetail(detail: string, t: (key: string) => string): string {
  switch (detail) {
    case "Submitting request to the backend queue.":
      return t("progress.detail_submitting");
    case "Waiting for a worker slot.":
      return t("progress.detail_waiting_worker");
    case "Backend worker started processing your request.":
      return t("progress.detail_started");
    case "The backend is advancing through the pipeline.":
      return t("progress.detail_processing");
    case "Waiting for the next backend update.":
      return t("progress.detail_waiting_update");
    case "Artifacts are ready.":
      return t("progress.detail_artifacts_ready");
    default:
      return detail;
  }
}

// Helper to group releases by year
function groupReleasesByYear(releases: ReleaseMetadata[]): Map<number, ReleaseMetadata[]> {
  const grouped = new Map<number, ReleaseMetadata[]>();
  
  releases.forEach((release) => {
    const date = new Date(release.release_date);
    const year = Number.isNaN(date.getTime()) ? new Date().getFullYear() : date.getFullYear();
    
    if (!grouped.has(year)) {
      grouped.set(year, []);
    }
    grouped.get(year)!.push(release);
  });
  
  // Sort years in descending order (newest first)
  return new Map([...grouped.entries()].sort((a, b) => b[0] - a[0]));
}

// Year group row component
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
        <span className="font-semibold text-foreground text-label">{year}</span>
        <span className="rounded bg-surface px-2 py-1 text-caption text-muted-foreground">
          {releaseCount} {releaseLabel}
        </span>
      </div>
    </button>
  );
}

// Release item component (used in year groups)
function ReleaseItem({
  release,
  selected,
  onSelect,
  locale,
}: {
  release: ReleaseMetadata;
  selected: boolean;
  onSelect: () => void;
  locale: string;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      className={cn(
        "flex w-full items-center justify-between rounded border px-3 py-2.5 text-left transition-all focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2",
        selected
          ? "border-primary bg-primary/10 text-foreground hover:bg-primary/15 active:bg-primary/20"
          : "border-sidebar-border bg-sidebar text-foreground hover:border-primary hover:bg-surface focus-visible:border-primary",
      )}
    >
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <span className="font-semibold tracking-tight text-label text-foreground">
            {formatReleaseDate(release, locale, "short")}
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

function ReleasePairCard({
  role,
  tone,
  release,
  locale,
  archiveLabel,
}: {
  role: string;
  tone: string;
  release?: ReleaseMetadata;
  locale: string;
  archiveLabel: string;
}) {
  return (
    <div className="rounded border border-sidebar-border bg-sidebar px-4 py-3.5">
      <div className="flex items-center justify-between gap-3">
        <p className="label-xs font-semibold text-muted-foreground uppercase">{role}</p>
        {release ? (
          <span className="rounded-full border border-sidebar-border bg-surface px-2.5 py-1 text-caption font-medium text-foreground">
            {tone}
          </span>
        ) : null}
      </div>

      {release ? (
        <div className="mt-3 space-y-1.5">
          <p className="heading-sm font-semibold text-foreground">{formatReleaseDate(release, locale)}</p>
          <p className="text-label text-muted-foreground">{formatReleaseMonth(release, locale, archiveLabel)}</p>
          <p className="text-caption uppercase tracking-wider text-muted-foreground">{describeArchive(release, archiveLabel)}</p>
        </div>
      ) : (
        <div className="mt-3">
          <p className="text-label text-muted-foreground">{archiveLabel}</p>
        </div>
      )}
    </div>
  );
}

function buildTemporalMilestone(release: ReleaseMetadata): TemporalMilestone {
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
    cumulative_growth_blocks_geojson: null,
    cumulative_growth_envelope_geojson: null,
    reference_imagery: null,
    metrics: null,
    artifacts: [],
  };
}

function buildTemporalBootstrapProject(
  projectId: string,
  aoi: GeoJSON.Polygon | null,
  releases: ReleaseMetadata[],
  projectName: string,
): TemporalProject {
  const timestamp = new Date().toISOString();
  return {
    project_id: projectId,
    name: projectName,
    semantics: "expansion_only",
    aoi_geojson: aoi,
    milestones: releases.map(buildTemporalMilestone),
    created_at: timestamp,
    updated_at: timestamp,
    warnings: [],
    validation_blocking_errors: [],
    download_bundle_path: null,
    has_reference_layers: false,
    reference_layer_count: 0,
  };
}

function compareReleaseIdentifiersByDate(
  left: string,
  right: string,
  releasesById: Map<string, ReleaseMetadata>,
): number {
  const leftRelease = releasesById.get(left);
  const rightRelease = releasesById.get(right);
  const leftTime = leftRelease ? parseReleaseTime(leftRelease) : -Infinity;
  const rightTime = rightRelease ? parseReleaseTime(rightRelease) : -Infinity;
  return leftTime - rightTime;
}

export function SettingsPanel({
  workflowMode,
  onWorkflowModeChange,
  backendUrl,
  releases,
  releasesLoading,
  releasesError,
  backendAvailability,
  backendAvailabilityLoading,
  backendAvailabilityError,
  runtimeConfig,
  isCollapsed,
  onToggleCollapse,
}: {
  workflowMode: "pairwise" | "temporal";
  onWorkflowModeChange: (mode: "pairwise" | "temporal") => void;
  backendUrl: string;
  releases: ReleaseMetadata[];
  releasesLoading: boolean;
  releasesError: string | null;
  backendAvailability: BackendAvailability[];
  backendAvailabilityLoading: boolean;
  backendAvailabilityError: string | null;
  runtimeConfig: FrontendRuntimeConfig;
  isCollapsed: boolean;
  onToggleCollapse: () => void;
}) {
  const state = useAppStore();
  const queryClient = useQueryClient();
  const setSetting = useAppStore((store) => store.setSetting);
  const selectedReleaseIds = useAppStore((store) => store.selectedReleaseIds);
  const setSelectedReleaseIds = useAppStore((store) => store.setSelectedReleaseIds);
  const startDrawing = useAppStore((store) => store.startDrawing);
  const startRectangleDrawing = useAppStore((store) => store.startRectangleDrawing);
  const startEditing = useAppStore((store) => store.startEditing);
  const stopDrawing = useAppStore((store) => store.stopDrawing);
  const setDrawingSubMode = useAppStore((store) => store.setDrawingSubMode);
  const finishDrawing = useAppStore((store) => store.finishDrawing);
  const setAoiFromImport = useAppStore((store) => store.setAoiFromImport);
  const clearAoi = useAppStore((store) => store.clearAoi);
  const requestMapFocusToAoi = useAppStore((store) => store.requestMapFocusToAoi);
  const setTemporalProject = useAppStore((store) => store.setTemporalProject);
  const setTemporalProjectBootstrap = useAppStore((store) => store.setTemporalProjectBootstrap);
  const setValidation = useAppStore((store) => store.setValidation);
  const setResult = useAppStore((store) => store.setResult);
  const setRunStatus = useAppStore((store) => store.setRunStatus);
  const setRunProgress = useAppStore((store) => store.setRunProgress);
  const setIsRunning = useAppStore((store) => store.setIsRunning);

  const [activePanel, setActivePanel] = useState<PanelId>("overview");
  const [downloadsExpanded, setDownloadsExpanded] = useState(false);
  const [importModalOpen, setImportModalOpen] = useState(false);
  const [expandedYears, setExpandedYears] = useState<Set<number>>(new Set());
  const [selectedProjectId, setSelectedProjectId] = useState("");
  const [createProjectOpen, setCreateProjectOpen] = useState(false);
  const [createProjectName, setCreateProjectName] = useState("");
  const [createProjectDirectory, setCreateProjectDirectory] = useState(DEFAULT_PROJECT_DIRECTORY);
  const [createProjectError, setCreateProjectError] = useState<string | null>(null);
  const [createProjectBusy, setCreateProjectBusy] = useState(false);
  const suppressNextResultDownloadsRef = useRef(false);
  const latestProjectLoadRef = useRef<string | null>(null);

  const { t, language } = useI18n();
  const locale = language === "fr" ? "fr-FR" : "en-GB";
  const currentRequest = buildRequest(state, runtimeConfig);
  const currentRequestKey = requestKey(currentRequest);
  const validation = state.validation;
  const result = state.result;

  const projectsQuery = useQuery({
    queryKey: ["temporal-projects", "saved-only"],
    queryFn: () => listTemporalProjects(),
  });

  const selectedProjectSummary = useMemo(
    () => projectsQuery.data?.find((item) => item.project_id === selectedProjectId) ?? null,
    [projectsQuery.data, selectedProjectId],
  );

  const preferredLoadedProjectPanel = (project: TemporalProject): PanelId => {
    return project.milestones.length > 0 ? "progress" : "aoi";
  };

  const loadProjectMutation = useMutation({
    mutationFn: async ({ projectId, expectedProjectDir }: { projectId: string; expectedProjectDir?: string | null }) => {
      const loadedProject = await getTemporalProject(projectId);
      assertLoadedProjectMatchesSelection(loadedProject, projectId, expectedProjectDir);
      return loadedProject;
    },
    onSuccess: async (loadedProject, variables) => {
      if (loadedProject.project_id !== variables.projectId) {
        return;
      }
      if (latestProjectLoadRef.current && latestProjectLoadRef.current !== variables.projectId) {
        return;
      }
      const sortedMilestones = [...loadedProject.milestones].sort(
        (left, right) => Date.parse(left.release_date ?? "") - Date.parse(right.release_date ?? ""),
      );
      const isTemporalProject = sortedMilestones.length > 2;

      setSelectedProjectId(loadedProject.project_id);
      setSelectedReleaseIds(sortedMilestones.map((item) => item.release_identifier));
      setValidation(null, null);
      setResult(null);

      if (loadedProject.aoi_geojson && loadedProject.aoi_geojson.type === "Polygon") {
        setAoiFromImport(loadedProject.aoi_geojson as GeoJSON.Polygon);
        requestMapFocusToAoi();
      } else {
        clearAoi();
      }

      if (isTemporalProject) {
        suppressNextResultDownloadsRef.current = false;
        const hydratedProject = {
          ...loadedProject,
          milestones: sortedMilestones,
        };
        setTemporalProject(hydratedProject);
        setTemporalProjectBootstrap(hydratedProject);
        onWorkflowModeChange("temporal");
        return;
      }

      setTemporalProject(null);
      setTemporalProjectBootstrap(null);
      onWorkflowModeChange("pairwise");
      setSetting("t1Release", sortedMilestones[0]?.release_identifier ?? "");
      setSetting("t2Release", sortedMilestones[1]?.release_identifier ?? "");

      if (loadedProject.project_id.startsWith("run-")) {
        suppressNextResultDownloadsRef.current = false;
        try {
          const cachedRunResponse = await getCachedRunResponse(loadedProject.project_id.slice(4));
          if (latestProjectLoadRef.current && latestProjectLoadRef.current !== loadedProject.project_id) {
            return;
          }
          if (cachedRunResponse.success) {
            setResult(cachedRunResponse);
          } else {
            suppressNextResultDownloadsRef.current = false;
          }
        } catch {
          suppressNextResultDownloadsRef.current = false;
        }
      } else {
        suppressNextResultDownloadsRef.current = false;
      }
    },
  });

  const loadSavedProject = (projectId: string, expectedProjectDir?: string | null) => {
    if (!projectId) {
      return;
    }
    if (projectsQuery.data && !projectExistsInList(projectsQuery.data, projectId)) {
      setSelectedProjectId("");
      if (latestProjectLoadRef.current === projectId) {
        latestProjectLoadRef.current = null;
      }
      return;
    }
    setSelectedProjectId(projectId);
    latestProjectLoadRef.current = projectId;
    loadProjectMutation.mutate({ projectId, expectedProjectDir });
  };

  useEffect(() => {
    if (!projectsQuery.data || !selectedProjectId || selectedProjectSummary) {
      return;
    }
    const staleProjectId = selectedProjectId;
    setSelectedProjectId("");
    if (latestProjectLoadRef.current === staleProjectId) {
      latestProjectLoadRef.current = null;
    }
  }, [projectsQuery.data, selectedProjectId, selectedProjectSummary]);

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
      const timestamp = new Date().toISOString();
      const projectId = buildProjectIdFromName(name);
      const nextProject: TemporalProject = {
        project_id: projectId,
        name,
        project_dir: resolveProjectDirectory(projectId, directory),
        semantics: "expansion_only",
        aoi_geojson: state.aoi,
        milestones: [],
        created_at: timestamp,
        updated_at: timestamp,
        warnings: [],
        validation_blocking_errors: [],
        download_bundle_path: null,
        has_reference_layers: false,
        reference_layer_count: 0,
      };
      const savedProject = await saveTemporalProject(nextProject);
      setSelectedProjectId(savedProject.project_id);
      setSelectedReleaseIds([]);
      setTemporalProject(null);
      setTemporalProjectBootstrap(null);
      setValidation(null, null);
      setResult(null);
      clearAoi();
      setSetting("t1Release", "");
      setSetting("t2Release", "");
      onWorkflowModeChange("pairwise");
      setCreateProjectOpen(false);
      setCreateProjectError(null);
      setCreateProjectName("");
      setCreateProjectDirectory(DEFAULT_PROJECT_DIRECTORY);
      setActivePanel("aoi");
      void queryClient.invalidateQueries({ queryKey: ["temporal-projects"] });
    } catch (error) {
      setCreateProjectError(error instanceof Error ? error.message : t("temporal.create_project_failed"));
    } finally {
      setCreateProjectBusy(false);
    }
  };

  const availabilityByMode = useMemo(
    () => new Map(backendAvailability.map((entry) => [entry.mode, entry])),
    [backendAvailability],
  );
  const selectedBackendAvailability = availabilityByMode.get(state.settings.modelBackend);
  const probeMissingForBandon =
    state.settings.modelBackend === "bandon_mps" &&
    backendAvailability.length === 0 &&
    backendAvailabilityError !== null;
  const selectedBackendBlocked =
    probeMissingForBandon || (backendAvailability.length > 0 && selectedBackendAvailability?.available === false);
  const selectedBackendReason =
    (probeMissingForBandon
      ? t("settings.backend_probe_unavailable")
      : null) ??
    selectedBackendAvailability?.reason ??
    (selectedBackendBlocked ? t("settings.backend_unavailable") : null);
  const runEnabled =
    Boolean(currentRequest) &&
    Boolean(validation?.valid) &&
    currentRequestKey === state.validationRequestKey &&
    !state.isRunning &&
    !selectedBackendBlocked;

  const releasesById = useMemo(() => new Map(releases.map((release) => [release.identifier, release])), [releases]);
  const sortedReleases = useMemo(
    () => [...releases].sort((left, right) => parseReleaseTime(right) - parseReleaseTime(left)),
    [releases],
  );

  useEffect(() => {
    if (workflowMode !== "pairwise") {
      return;
    }
    const nextSelected = [state.settings.t1Release, state.settings.t2Release]
      .filter((value): value is string => Boolean(value))
      .sort((left, right) => compareReleaseIdentifiersByDate(left, right, releasesById));
    if (selectedReleaseIds.length !== nextSelected.length || selectedReleaseIds.some((value, index) => value !== nextSelected[index])) {
      setSelectedReleaseIds(nextSelected);
    }
  }, [releasesById, selectedReleaseIds, setSelectedReleaseIds, state.settings.t1Release, state.settings.t2Release, workflowMode]);

  useEffect(() => {
    if (state.isRunning) {
      setActivePanel("progress");
      return;
    }
    if (result) {
      if (suppressNextResultDownloadsRef.current) {
        suppressNextResultDownloadsRef.current = false;
        return;
      }
      setActivePanel("downloads");
    }
  }, [result, state.isRunning]);

  useEffect(() => {
    setDownloadsExpanded(false);
  }, [result?.summary?.request_hash]);

  useEffect(() => {
    if (!selectedBackendBlocked || !selectedBackendReason) {
      return;
    }
    setRunStatus(selectedBackendReason);
  }, [selectedBackendBlocked, selectedBackendReason, setRunStatus]);

  const validationMutation = useMutation({
    mutationFn: validateRequest,
    onSuccess: (response) => {
      setValidation(response, currentRequestKey);
      setRunStatus(response.valid ? t("status.validation_passed") : t("status.validation_needs_attention"));
    },
    onError: (error) => {
      setValidation(null, null);
      setRunStatus(error instanceof Error ? error.message : t("status.validation_needs_attention"));
    },
  });

  const runMutation = useMutation({
    mutationFn: async (request: ValidationRequest) => {
      setIsRunning(true);
      setResult(null);
      setRunProgress(createActiveRunProgress());
      return runDetection(request, setRunStatus, setRunProgress);
    },
    onSuccess: (response) => {
      setIsRunning(false);
      setResult(response);
      setRunProgress(createCompletedRunProgress());
      setRunStatus(response.success ? t("status.completed_label") : response.error_message ?? t("status.run_failed"));
    },
    onError: (error) => {
      setIsRunning(false);
      setRunProgress(createErrorRunProgress(error instanceof Error ? error.message : t("status.run_failed")));
      setRunStatus(error instanceof Error ? error.message : t("status.run_failed"));
    },
  });

  const selectedReleases = selectedReleaseIds
    .map((identifier) => releasesById.get(identifier))
    .filter((release): release is ReleaseMetadata => Boolean(release))
    .sort((left, right) => parseReleaseTime(left) - parseReleaseTime(right));

  useEffect(() => {
    if (selectedReleaseIds.length === 2 && workflowMode !== "pairwise") {
      onWorkflowModeChange("pairwise");
    }
  }, [onWorkflowModeChange, selectedReleaseIds.length, workflowMode]);

  const artifactCount = result?.artifacts.length ?? 0;

  const downloadBundle = async () => {
    const runId = result?.summary?.request_hash;
    if (!runId) {
      return;
    }
    const bundlePath = result.downloadable_zip_path ?? (await createRunExportBundle(runId));
    const name = bundlePath.split("/").pop() ?? "building-change-outputs.zip";
    await downloadFileFromUrl(buildBackendFileUrl(backendUrl, bundlePath), name);
  };

  const downloadArtifact = async (path: string, name: string) => {
    await downloadFileFromUrl(buildBackendFileUrl(backendUrl, path), name);
  };

  const handleReleaseToggle = (identifier: string) => {
    const release = releasesById.get(identifier);
    if (!release) return;
    const nextSelection = selectedReleaseIds.includes(identifier)
      ? selectedReleaseIds.filter((item) => item !== identifier)
      : [...selectedReleaseIds, identifier];
    const sortedSelection = [...nextSelection].sort((left, right) =>
      compareReleaseIdentifiersByDate(left, right, releasesById),
    );

    setSelectedReleaseIds(sortedSelection);

    if (sortedSelection.length <= 2) {
      const [t1Release, t2Release] = sortedSelection;
      setTemporalProject(null);
      setTemporalProjectBootstrap(null);
      setSetting("t1Release", t1Release ?? "");
      setSetting("t2Release", t2Release ?? "");
      onWorkflowModeChange("pairwise");
      return;
    }

    const selectedReleasesForTemporal = sortedSelection
      .map((releaseId) => releasesById.get(releaseId))
      .filter((item): item is ReleaseMetadata => Boolean(item));

    if (selectedReleasesForTemporal.length >= 3) {
      const temporalProjectId =
        selectedProjectSummary && !selectedProjectSummary.project_id.startsWith("run-")
          ? selectedProjectSummary.project_id
          : `temporal-${Date.now()}`;
      const temporalProjectName =
        selectedProjectSummary && !selectedProjectSummary.project_id.startsWith("run-")
          ? selectedProjectSummary.name
          : t("temporal.untitled_project");
      const temporalProjectDirectory =
        selectedProjectSummary && !selectedProjectSummary.project_id.startsWith("run-")
          ? selectedProjectSummary.project_dir ?? resolveProjectDirectory(temporalProjectId, DEFAULT_PROJECT_DIRECTORY)
          : null;

      setSetting("t1Release", "");
      setSetting("t2Release", "");
      const bootstrapProject = {
        ...buildTemporalBootstrapProject(temporalProjectId, state.aoi, selectedReleasesForTemporal, t("temporal.untitled_project")),
        name: temporalProjectName,
        project_dir: temporalProjectDirectory,
      };
      setTemporalProject(bootstrapProject);
      setTemporalProjectBootstrap(bootstrapProject);
      onWorkflowModeChange("temporal");
    }
  };

  const validationState =
    validationMutation.isPending ? "validating" : validation ? (validation.valid ? "valid" : "invalid") : "idle";

  const navItems: Array<{
    id: PanelId;
    icon: React.ComponentType<{ className?: string }>;
    label: string;
  }> = [
    { id: "overview", icon: Info, label: t("settings.panel.overview") },
    { id: "aoi", icon: Pentagon, label: t("settings.panel.aoi") },
    { id: "releases", icon: Layers, label: t("settings.panel.releases") },
    { id: "progress", icon: Play, label: t("settings.panel.progress") },
    { id: "downloads", icon: Download, label: t("settings.panel.downloads") },
  ];

  const activeTitle =
    navItems.find((item) => item.id === activePanel)?.label ?? t("settings.panel.progress");
  const aoiVertices = state.draftVertices.length || (state.aoi ? state.aoi.coordinates[0].length - 1 : 0);
  const visibleStages = PIPELINE_STAGES.filter((stage) => stage.key !== "queue");
  const runStatus = state.isRunning ? "running" : result ? (result.success ? "completed" : "failed") : "idle";

  return (
    <>
      <Dialog open={createProjectOpen} onOpenChange={setCreateProjectOpen}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>{t("settings.create_project_title")}</DialogTitle>
            <DialogDescription>{t("settings.create_project_description")}</DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-2">
            <div className="space-y-2">
              <label htmlFor="create-project-name" className="label-xs">
                {t("settings.project_name")}
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
              <label htmlFor="create-project-directory" className="label-xs">
                {t("settings.save_directory")}
              </label>
              <Input
                id="create-project-directory"
                value={createProjectDirectory}
                onChange={(event) => setCreateProjectDirectory(event.target.value)}
                placeholder={DEFAULT_PROJECT_DIRECTORY}
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
              {t("common.cancel")}
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
          <div className="space-y-6 px-5 py-5">
            <WorkflowSectionCard
              title={t("temporal.overview")}
              description={t("temporal.overview.description")}
              actions={
                <button
                  type="button"
                  onClick={handleCreateProject}
                  className="inline-flex items-center gap-2 rounded border border-sidebar-border bg-sidebar px-3 py-2 text-sm font-medium text-foreground transition-colors hover:bg-surface"
                >
                  <Plus className="h-4 w-4" />
                  {t("temporal.new_button")}
                </button>
              }
              contentClassName="space-y-4"
            >
              <div className="space-y-2">
                <label htmlFor="saved-projects" className="label-xs">
                  {t("temporal.saved_projects")}
                </label>
                <div className="flex gap-2">
                <Select
                  id="saved-projects"
                  value={selectedProjectSummary?.project_id ?? ""}
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
                    {(projectsQuery.data ?? []).map((item: TemporalProjectSummary) => (
                      <option key={item.project_id} value={item.project_id}>
                        {getProjectDisplayName(item, t)}
                      </option>
                    ))}
                  </Select>
                  <button
                    type="button"
                    onClick={() => {
                      if (selectedProjectSummary) {
                        loadSavedProject(selectedProjectSummary.project_id, selectedProjectSummary.project_dir);
                      }
                    }}
                    disabled={!selectedProjectSummary || loadProjectMutation.isPending}
                    aria-label={t("temporal.load_project")}
                    className="inline-flex h-11 w-11 items-center justify-center rounded border border-sidebar-border bg-card text-foreground transition-colors hover:bg-surface disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {loadProjectMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <FolderOpen className="h-4 w-4" />}
                  </button>
                </div>
              </div>

              {projectsQuery.error ? (
                <div className="rounded border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive-foreground">
                  {formatErrorMessage(projectsQuery.error, t("common.unexpected_error"))}
                </div>
              ) : null}

              {loadProjectMutation.error ? (
                <div className="rounded border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive-foreground">
                  {formatErrorMessage(loadProjectMutation.error, t("common.unexpected_error"))}
                </div>
              ) : null}
            </WorkflowSectionCard>
          </div>
            ) : null}

            {activePanel === "releases" ? (
              <div className="space-y-5 px-5 py-5">
                <div className="space-y-4 rounded border border-sidebar-border bg-sidebar px-4 py-4">
                  <SectionTitle
                    title={t("temporal.selected_releases")}
                    description={t("temporal.selected_releases_description")}
                  />
                  <div className="space-y-2">
                    {selectedReleases.length ? (
                      <div className="space-y-2">
                        {selectedReleases.map((release, index) => (
                          <div
                            key={release.identifier}
                            className="flex items-center justify-between gap-3 rounded border border-sidebar-border bg-card px-4 py-3"
                          >
                            <div className="min-w-0">
                              <p className="truncate text-label font-medium text-foreground">
                                {index === 0 ? t("temporal.selected_release_earliest") : index === 1 ? t("temporal.selected_release_latest") : t("temporal.selected_release")}
                              </p>
                              <p className="truncate text-caption text-muted-foreground">{formatReleaseDate(release, locale, "short")}</p>
                              <p className="truncate text-caption text-foreground">{release.identifier}</p>
                            </div>
                            <button
                              type="button"
                              onClick={() => handleReleaseToggle(release.identifier)}
                              className="rounded border border-sidebar-border bg-surface px-3 py-1.5 text-caption font-medium text-foreground transition hover:bg-sidebar"
                            >
                              {t("button.remove")}
                            </button>
                          </div>
                        ))}
                      </div>
                    ) : (
                      <div className="rounded border border-dashed border-sidebar-border px-4 py-6 text-sm text-muted-foreground">
                        {t("temporal.no_releases_selected")}
                      </div>
                    )}
                  </div>
                </div>

                {releasesLoading ? (
                  <div className="rounded border border-sidebar-border bg-sidebar px-4 py-4 text-sm text-muted-foreground">
                    {t("status.loading_releases")}
                  </div>
                ) : null}

                {releasesError ? (
                  <div className="rounded border border-destructive/30 bg-destructive/10 px-4 py-4 text-sm text-destructive-foreground">
                    {releasesError}
                  </div>
                ) : null}

                {!releasesLoading ? (
                  <div className="space-y-4">
                    {/* Year-grouped releases */}
                    <div className="space-y-2">
                      {Array.from(groupReleasesByYear(sortedReleases)).map(([year, yearReleases]) => (
                        <div key={year} className="space-y-2">
                          <YearGroupRow
                            year={year}
                            isExpanded={expandedYears.has(year)}
                            onToggle={() => {
                              const newExpanded = new Set(expandedYears);
                              if (newExpanded.has(year)) {
                                newExpanded.delete(year);
                              } else {
                                newExpanded.add(year);
                              }
                              setExpandedYears(newExpanded);
                            }}
                            releaseCount={yearReleases.length}
                            releaseLabel={t(yearReleases.length === 1 ? "release.single" : "release.plural")}
                          />
                          {expandedYears.has(year) && (
                            <div className="space-y-2 pl-4">
                              {yearReleases.map((release) => {
                                const alreadySelected = selectedReleaseIds.includes(release.identifier);
                                return (
                                  <ReleaseItem
                                    key={release.identifier}
                                    release={release}
                                    selected={alreadySelected}
                                    onSelect={() => handleReleaseToggle(release.identifier)}
                                    locale={locale}
                                  />
                                );
                              })}
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  </div>
                ) : null}
              </div>
            ) : null}

            {activePanel === "aoi" ? (
              <>
                <AOIImportModal
                  open={importModalOpen}
                  onOpenChange={setImportModalOpen}
                  onImport={(geometry) => {
                    setAoiFromImport(geometry);
                  }}
                />
                <SharedAoiSection
                  sectionTitle={t("temporal.draw_aoi")}
                  readyText={t("temporal.aoi_ready")}
                  emptyText={t("temporal.no_aoi_yet")}
                  drawingSubMode={state.drawingSubMode}
                  drawingMode={state.drawingMode}
                  aoiReady={Boolean(state.aoi)}
                  vertexCount={aoiVertices}
                  onSelectMode={(mode) => {
                    setDrawingSubMode(mode);
                  }}
                  onStartDrawing={() => {
                    if (state.drawingSubMode === "rectangle") {
                      startRectangleDrawing();
                    } else {
                      startDrawing();
                    }
                  }}
                  onStartEditing={startEditing}
                  onClear={clearAoi}
                  onImport={() => setImportModalOpen(true)}
                  importLabel={t("button.import_file")}
                />
                {(state.drawingMode === "drawing" || state.drawingMode === "editing") ? (
                  <div className="space-y-6 px-5 pb-5">
                    <div className="space-y-3 rounded border border-sidebar-border bg-surface px-4 py-4">
                      <SectionTitle
                        title={state.drawingMode === "drawing" ? t("section.drawing_on_map") : t("section.editing_vertices")}
                        description={
                          state.drawingMode === "drawing"
                            ? state.drawingSubMode === "rectangle"
                              ? t("instruction.rectangle_step2")
                              : t("instruction.polygon_steps")
                            : t("instruction.editing_steps")
                        }
                      />
                      <div className="grid grid-cols-2 gap-3">
                        <button
                          type="button"
                          onClick={finishDrawing}
                          disabled={state.draftVertices.length < 3 || (state.drawingSubMode === "rectangle" && state.draftVertices.length < 4)}
                          className="rounded border border-primary bg-primary/10 px-3 py-3 text-label font-medium text-foreground transition-all hover:bg-primary/15 focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2 active:bg-primary/20 disabled:cursor-not-allowed disabled:opacity-50"
                        >
                          {t("button.finish")}
                        </button>
                        <button
                          type="button"
                          onClick={stopDrawing}
                          className="rounded border border-sidebar-border bg-transparent px-3 py-3 text-label font-medium text-foreground transition-all hover:border-primary hover:bg-surface focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2"
                        >
                          {t("button.cancel")}
                        </button>
                      </div>
                    </div>
                  </div>
                ) : null}
              </>
            ) : null}

            {activePanel === "progress" ? (
              <div className="space-y-6 px-5 py-5">
              <div className="space-y-3 rounded border border-sidebar-border bg-sidebar px-4 py-4">
                <SectionTitle title={t("validation.title")} />
                <button
                  type="button"
                  disabled={!currentRequest || validationMutation.isPending}
                  onClick={() => {
                    if (selectedBackendBlocked) {
                      setRunStatus(selectedBackendReason ?? t("settings.backend_unavailable"));
                      return;
                    }
                    if (currentRequest) {
                      validationMutation.mutate(currentRequest);
                    }
                  }}
                  aria-busy={validationMutation.isPending}
                  className="flex h-11 w-full items-center justify-center rounded bg-surface text-label font-medium text-foreground transition-all hover:bg-muted focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
                >
                {validationMutation.isPending ? (
                  <>
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" />
                    {t("button.validating")}
                  </>
                ) : (
                  t("button.validate_request")
                )}
                </button>
              </div>

              <div className="space-y-3 rounded border border-sidebar-border bg-sidebar px-4 py-4">
                <button
                  type="button"
                  disabled={!runEnabled || runMutation.isPending}
                  onClick={() => {
                    if (selectedBackendBlocked) {
                      const message = selectedBackendReason ?? t("settings.backend_unavailable");
                      setRunStatus(message);
                      setRunProgress(createErrorRunProgress(message));
                      return;
                    }
                    if (currentRequest) {
                      runMutation.mutate(currentRequest);
                    }
                  }}
                  aria-busy={runMutation.isPending || state.isRunning}
                  className="flex h-12 w-full items-center justify-center rounded bg-primary text-label font-medium text-primary-foreground transition-all hover:opacity-95 hover:shadow-md focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {runMutation.isPending || state.isRunning ? (
                    <>
                      <Loader2 className="mr-3 h-5 w-5 animate-spin" aria-hidden="true" />
                      {t("status.running_label")}
                    </>
                  ) : (
                    <>
                      <Play className="mr-3 h-5 w-5" aria-hidden="true" />
                      {t("button.run_detection")}
                    </>
                  )}
                </button>

                <p className="text-center text-label text-muted-foreground">
                  {selectedBackendBlocked
                    ? selectedBackendReason
                    : runEnabled
                    ? t("status.ready_to_run")
                    : t("status.validation_required")}
                </p>
              </div>

              {validation ? (
                <div className="space-y-4 rounded border border-sidebar-border bg-sidebar px-4 py-4">
                  <SectionTitle title={t("ui.validation_result")} />
                  <div className="flex flex-wrap gap-2">
                    <span className="rounded border border-sidebar-border bg-surface px-3 py-1.5 text-caption font-medium text-foreground">
                      {formatNumber(validation.estimated_total_tiles)} {t("temporal.total_tiles")}
                    </span>
                    <span className="rounded border border-sidebar-border bg-surface px-3 py-1.5 text-caption font-medium text-foreground">
                      {formatArea(validation.estimated_area_m2, t("release.not_available"))}
                    </span>
                  </div>

                  {validation.blocking_errors.length > 0 ? (
                    <div className="space-y-2" role="alert">
                      {validation.blocking_errors.map((message) => (
                        <div key={message} className="flex gap-2 text-label text-red-900 dark:text-red-200">
                          <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 flex-shrink-0 text-red-600 dark:text-red-500" aria-hidden="true" />
                          <span>{message}</span>
                        </div>
                      ))}
                    </div>
                  ) : null}

                  {validation.warnings.length > 0 ? (
                    <div className="space-y-2" role="alert">
                      {validation.warnings.map((message) => (
                        <div key={message} className="flex gap-2 text-label text-amber-900 dark:text-amber-200">
                          <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 flex-shrink-0 text-amber-600 dark:text-amber-500" aria-hidden="true" />
                          <span>{message}</span>
                        </div>
                      ))}
                    </div>
                  ) : null}

                  {validation.valid && validation.warnings.length === 0 ? (
                    <div className="flex items-start gap-2 text-label text-foreground">
                      <CheckCircle2 className="mt-0.5 h-5 w-5 shrink-0 text-green-600 dark:text-green-500" aria-hidden="true" />
                      <span>{t("status.ready_to_run")}</span>
                    </div>
                  ) : null}
                </div>
              ) : null}
            </div>
          ) : null}

            {activePanel === "progress" ? (
              <div className="space-y-5 px-5 py-5">
              <div className="space-y-3 rounded border border-sidebar-border bg-sidebar px-4 py-4">
                <SectionTitle title={t("ui.pipeline_status")} />
                <div className="flex items-center justify-between text-label text-foreground">
                  <span className="font-medium">{formatRunStatus(state.runProgress, t)}</span>
                  <span className="text-label font-semibold">{Math.round(state.runProgress.percent)}%</span>
                </div>
                  <Progress
                    value={state.runProgress.percent}
                    className="h-2.5 bg-muted"
                    indicatorClassName={cn(state.runProgress.phase === "error" ? "bg-destructive" : "bg-primary")}
                  />
                <p className="text-label text-muted-foreground leading-relaxed">
                  {runStatus === "idle" ? t("status.idle_instruction") : localizeRunProgressDetail(state.runProgress.detail, t)}
                </p>
              </div>

              <div className="space-y-2">
                {visibleStages.map((stage) => {
                  const stageState = getStageState(state.runProgress, stage);
                  const isError =
                    state.runProgress.phase === "error" &&
                    stageState === "current" &&
                    stage.label.toLowerCase().includes(state.runProgress.stageLabel.toLowerCase());
                  return (
                    <div
                      key={stage.key}
                      className="flex items-center gap-3 rounded border border-sidebar-border bg-sidebar px-4 py-3"
                    >
                      <div
                        className={cn(
                          "flex h-8 w-8 items-center justify-center rounded-full border shrink-0",
                          stageState === "complete" && "border-green-500/40 bg-green-500/10 text-green-500",
                          stageState === "current" && !isError && "border-primary/40 bg-primary/10 text-primary",
                          (stageState === "pending" || isError) && "border-sidebar-border bg-sidebar text-muted-foreground",
                        )}
                      >
                        {stageState === "complete" ? (
                          <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
                        ) : stageState === "current" && !isError ? (
                          <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
                        ) : (
                          <Clock3 className="h-4 w-4" aria-hidden="true" />
                        )}
                      </div>
                      <div className="min-w-0 flex-1">
                        <p className="text-label font-medium text-foreground">{stage.label}</p>
                        <p className="text-caption text-muted-foreground">
                          {isError ? t("status.stage_failed") : stageState === "complete" ? t("status.stage_completed") : stageState === "current" ? t("status.stage_current") : t("status.stage_pending")}
                        </p>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          ) : null}

            {activePanel === "downloads" ? (
              <div className="space-y-5 px-5 py-5">
              {!result ? (
                <div className="rounded border border-sidebar-border bg-sidebar px-4 py-4 text-sm leading-6 text-muted-foreground">
                  {t("ui.run_pipeline_unlock")}
                </div>
              ) : null}

              {result && !result.success ? (
                <div className="rounded border border-destructive/30 bg-destructive/10 px-4 py-4 text-sm leading-6 text-destructive-foreground">
                  {result.error_message ?? t("settings.backend_unsuccessful_response")}
                </div>
              ) : null}

              {result?.success && result.summary ? (
                <>
                  <div className="grid grid-cols-2 gap-3">
                    <div className="rounded border border-sidebar-border bg-sidebar px-4 py-4">
                      <p className="label-xs font-semibold text-muted-foreground uppercase">{t("ui.detected")}</p>
                      <p className="mt-3 text-4xl font-bold leading-none text-foreground">
                        {formatNumber(
                          result.summary.result_semantics === "building_change"
                            ? (result.summary.total_change_polygons ?? 0)
                            : result.summary.total_new_buildings,
                        )}
                      </p>
                    </div>
                    <div className="rounded border border-sidebar-border bg-sidebar px-4 py-4">
                      <p className="label-xs font-semibold text-muted-foreground uppercase">{t("ui.area")}</p>
                      <p className="mt-3 text-4xl font-bold leading-none text-foreground">{formatArea(
                        result.summary.result_semantics === "building_change"
                          ? result.summary.total_change_area_m2
                          : result.summary.total_new_building_area_m2,
                        t("release.not_available"),
                      )}</p>
                    </div>
                  </div>

                  <div className="space-y-3 rounded border border-sidebar-border bg-sidebar px-4 py-4">
                    <SectionTitle title={t("ui.map_layers")} description={t("ui.instructions_map_control")} />
                    <div className="flex flex-wrap gap-2">
                      <span className="rounded border border-sidebar-border bg-surface px-3 py-1.5 text-caption font-medium text-muted-foreground">{t("results.preview.t1")}</span>
                      <span className="rounded border border-sidebar-border bg-surface px-3 py-1.5 text-caption font-medium text-muted-foreground">{t("results.preview.t2")}</span>
                      <span className="rounded border border-sidebar-border bg-surface px-3 py-1.5 text-caption font-medium text-muted-foreground">{t("results.preview.overlay")}</span>
                      {result.preview_images?.change_probability_preview_path ? (
                        <span className="rounded border border-sidebar-border bg-surface px-3 py-1.5 text-caption font-medium text-muted-foreground">{t("results.preview.probability")}</span>
                      ) : null}
                      {result.building_blocks_geojson ? (
                        <span className="rounded border border-sidebar-border bg-surface px-3 py-1.5 text-caption font-medium text-muted-foreground">{t("download.building_blocks")}</span>
                      ) : null}
                      {Object.keys(result.buffer_layers_geojson ?? {}).length > 0 ? (
                        <span className="rounded border border-sidebar-border bg-surface px-3 py-1.5 text-caption font-medium text-muted-foreground">{t("download.buffers")}</span>
                      ) : null}
                    </div>
                  </div>

                  <div className="space-y-3 rounded border border-sidebar-border bg-sidebar px-4 py-4">
                    <SectionTitle title={t("ui.downloads")} />
                    {result.summary?.request_hash ? (
                      <button
                        type="button"
                        onClick={() => {
                          void downloadBundle();
                        }}
                        className="flex items-center justify-between rounded border border-sidebar-border bg-sidebar px-4 py-3 text-label text-foreground transition-all hover:border-primary hover:bg-surface focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2"
                      >
                        <span>{t("download.download_all")}</span>
                        <Download className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
                      </button>
                    ) : null}

                    {artifactCount > 0 ? (
                      <div className="rounded border border-sidebar-border bg-sidebar">
                        <button
                          type="button"
                          onClick={() => setDownloadsExpanded((current) => !current)}
                          aria-expanded={downloadsExpanded}
                          className="flex w-full items-center justify-between px-4 py-3 text-left transition-all hover:bg-surface focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2"
                        >
                          <div>
                            <p className="text-label text-foreground">{downloadsExpanded ? t("ui.hide_files") : t("ui.browse_files")}</p>
                            <p className="mt-1 text-caption text-muted-foreground">
                              {artifactCount} {artifactCount > 1 ? t("badge.artifact_plural") : t("badge.artifact_single")}
                            </p>
                          </div>
                          <Download className="h-4 w-4 text-muted-foreground shrink-0" aria-hidden="true" />
                        </button>

                        {downloadsExpanded ? (
                          <div className="space-y-2 border-t border-sidebar-border px-3 py-3">
                            {result.artifacts.map((artifact) => (
                              <button
                                key={artifact.path}
                                type="button"
                                onClick={() => {
                                  void downloadArtifact(artifact.path, artifact.name);
                                }}
                                className="flex w-full items-center justify-between rounded border border-sidebar-border bg-surface px-3 py-3 text-left text-label text-foreground transition-all hover:border-primary hover:bg-surface/80 focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2"
                              >
                                <div className="min-w-0">
                                  <p className="truncate text-label text-foreground">{artifact.description}</p>
                                  <p className="truncate text-caption text-muted-foreground">{artifact.name}</p>
                                </div>
                                <Download className="ml-3 h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
                              </button>
                            ))}
                          </div>
                        ) : null}
                      </div>
                    ) : null}
                  </div>
                </>
              ) : null}
            </div>
          ) : null}
      </WorkspaceShell>
    </>
  );
}
