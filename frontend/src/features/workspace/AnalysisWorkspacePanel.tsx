import type { FrontendRuntimeConfig } from "@/lib/env";
import type { BackendAvailability, ReleaseMetadata } from "@/api/contracts";
import type { TemporalMapPresentation } from "@/features/temporal/types";
import { SettingsPanel } from "@/features/settings/SettingsPanel";
import { TemporalMosaicPanel } from "@/features/temporal/TemporalMosaicPanel";

export function AnalysisWorkspacePanel({
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
  onTemporalMapPresentationChange,
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
  onTemporalMapPresentationChange: (presentation: TemporalMapPresentation | null) => void;
}) {
  return (
    <>
      <div hidden={workflowMode !== "pairwise"} className="h-full">
      <SettingsPanel
        workflowMode={workflowMode}
        onWorkflowModeChange={onWorkflowModeChange}
        backendUrl={backendUrl}
        releases={releases}
        releasesLoading={releasesLoading}
        releasesError={releasesError}
        backendAvailability={backendAvailability}
        backendAvailabilityLoading={backendAvailabilityLoading}
        backendAvailabilityError={backendAvailabilityError}
        runtimeConfig={runtimeConfig}
        isCollapsed={isCollapsed}
        onToggleCollapse={onToggleCollapse}
      />
      </div>
      <div hidden={workflowMode !== "temporal"} className="h-full">
        <TemporalMosaicPanel
          workflowMode={workflowMode}
          onWorkflowModeChange={onWorkflowModeChange}
          backendUrl={backendUrl}
          runtimeConfig={runtimeConfig}
          releases={releases}
          releasesLoading={releasesLoading}
          releasesError={releasesError}
          backendAvailability={backendAvailability}
          backendAvailabilityLoading={backendAvailabilityLoading}
          backendAvailabilityError={backendAvailabilityError}
          isCollapsed={isCollapsed}
          onToggleCollapse={onToggleCollapse}
          onMapPresentationChange={onTemporalMapPresentationChange}
        />
      </div>
    </>
  );
}
