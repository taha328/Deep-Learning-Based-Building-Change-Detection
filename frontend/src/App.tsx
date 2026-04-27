import { lazy, Suspense, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { listReleases, probeBackends } from "@/api/client";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useAppStore } from "@/app/store";
import { I18nProvider } from "@/app/I18nProvider";
import { getFrontendRuntimeConfig, getMapboxApiKey, type FrontendRuntimeConfig } from "@/lib/env";
import { useI18n } from "@/lib/i18n";
import type { TemporalMapPresentation } from "@/features/temporal/types";

const AnalysisWorkspacePanel = lazy(() =>
  import("@/features/workspace/AnalysisWorkspacePanel").then((module) => ({
    default: module.AnalysisWorkspacePanel,
  })),
);

const MapView = lazy(() =>
  import("@/features/map/MapView").then((module) => ({
    default: module.MapView,
  })),
);

function WorkspaceSkeleton() {
  return (
    <div className="hidden h-full w-[30rem] max-w-[42vw] border-r border-border bg-sidebar lg:block">
      <div className="space-y-4 p-5">
        <div className="h-8 w-40 rounded-md bg-surface" />
        <div className="h-28 rounded-xl border border-sidebar-border bg-surface" />
        <div className="h-48 rounded-xl border border-sidebar-border bg-surface" />
        <div className="h-56 rounded-xl border border-sidebar-border bg-surface" />
      </div>
    </div>
  );
}

function MapSkeleton({ label }: { label: string }) {
  return (
    <div className="relative flex min-h-0 flex-1 items-center justify-center bg-surface">
      <div className="absolute inset-0 bg-[radial-gradient(circle_at_top,_oklch(var(--card))_0%,_oklch(var(--surface))_48%,_oklch(var(--background))_100%)]" />
      <div className="relative rounded-lg border border-border bg-card px-4 py-3 text-sm text-muted-foreground shadow-panel">
        {label}
      </div>
    </div>
  );
}

function AppContent() {
  const { t } = useI18n();
  const isRunning = useAppStore((state) => state.isRunning);
  const [panelCollapsed, setPanelCollapsed] = useState(false);
  const [workflowMode, setWorkflowModeState] = useState<"pairwise" | "temporal">("temporal");
  const [temporalMapPresentation, setTemporalMapPresentation] = useState<TemporalMapPresentation | null>(null);
  const setWorkflowMode = () => setWorkflowModeState("temporal");
  let runtimeConfig: FrontendRuntimeConfig | null = null;
  let backendUrl = "";
  let mapboxApiKey = "";
  let envError: string | null = null;

  try {
    runtimeConfig = getFrontendRuntimeConfig();
    backendUrl = runtimeConfig.backendUrl;
    mapboxApiKey = getMapboxApiKey();
  } catch (error) {
    envError = error instanceof Error ? error.message : t("error.missing_config");
  }

  const releasesQuery = useQuery({
    queryKey: ["wayback-releases", backendUrl],
    queryFn: listReleases,
    enabled: envError === null,
  });

  const backendsQuery = useQuery({
    queryKey: ["backend-availability", backendUrl],
    queryFn: probeBackends,
    enabled: envError === null && !isRunning,
    staleTime: 300_000,
    refetchInterval: false,
    refetchOnWindowFocus: false,
  });

  if (envError) {
    return (
      <main className="flex min-h-screen items-center justify-center bg-background px-6 py-10 text-foreground">
        <Card className="w-full max-w-3xl">
          <CardHeader>
            <CardTitle>{t("error.missing_config")}</CardTitle>
          </CardHeader>
          <CardContent className="text-sm text-muted-foreground">{envError}</CardContent>
        </Card>
      </main>
    );
  }

  if (!runtimeConfig) {
    return null;
  }

  return (
    <main className="relative flex h-screen flex-col overflow-hidden bg-background text-foreground lg:flex-row">
      <div className="flex min-h-0 flex-1 flex-col lg:flex-row">
        <Suspense fallback={<WorkspaceSkeleton />}>
          <AnalysisWorkspacePanel
            workflowMode={workflowMode}
            onWorkflowModeChange={setWorkflowMode}
            backendUrl={backendUrl}
            releases={releasesQuery.data ?? []}
            releasesLoading={releasesQuery.isLoading}
            releasesError={releasesQuery.error instanceof Error ? releasesQuery.error.message : null}
            backendAvailability={backendsQuery.data ?? []}
            backendAvailabilityLoading={backendsQuery.isLoading}
            backendAvailabilityError={backendsQuery.error instanceof Error ? backendsQuery.error.message : null}
            runtimeConfig={runtimeConfig}
            isCollapsed={panelCollapsed}
            onToggleCollapse={() => setPanelCollapsed((prev) => !prev)}
            onTemporalMapPresentationChange={setTemporalMapPresentation}
          />
        </Suspense>
        <Suspense fallback={<MapSkeleton label="Loading…" />}>
          <MapView
            apiKey={mapboxApiKey}
            backendUrl={backendUrl}
            workflowMode={workflowMode}
            temporalPresentation={temporalMapPresentation}
          />
        </Suspense>
      </div>
    </main>
  );
}

export default function App() {
  return (
    <I18nProvider>
      <AppContent />
    </I18nProvider>
  );
}
