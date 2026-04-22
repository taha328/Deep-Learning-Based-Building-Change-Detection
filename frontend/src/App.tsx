import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { listReleases, probeBackends } from "@/api/gradio";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useAppStore } from "@/app/store";
import { I18nProvider } from "@/app/I18nProvider";
import { MapView } from "@/features/map/MapView";
import { AnalysisWorkspacePanel } from "@/features/workspace/AnalysisWorkspacePanel";
import { getFrontendRuntimeConfig, getMapboxApiKey, type FrontendRuntimeConfig } from "@/lib/env";
import { useI18n } from "@/lib/i18n";
import type { TemporalMapPresentation } from "@/features/temporal/types";

function AppContent() {
  const { t } = useI18n();
  const isRunning = useAppStore((state) => state.isRunning);
  const [panelCollapsed, setPanelCollapsed] = useState(false);
  const [workflowMode, setWorkflowMode] = useState<"pairwise" | "temporal">("pairwise");
  const [temporalMapPresentation, setTemporalMapPresentation] = useState<TemporalMapPresentation | null>(null);
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
        <MapView
          apiKey={mapboxApiKey}
          backendUrl={backendUrl}
          workflowMode={workflowMode}
          temporalPresentation={workflowMode === "temporal" ? temporalMapPresentation : null}
        />
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
