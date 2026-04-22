import { useMemo, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  Download,
  FileImage,
  FileJson,
  Files,
  Map,
} from "lucide-react";

import { useAppStore } from "@/app/store";
import { useI18n } from "@/lib/i18n";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { RunProgressPanel } from "@/features/results/RunProgressPanel";
import { buildGradioFileUrl } from "@/lib/gradio-files";
import { cn, formatNumber } from "@/lib/utils";

function resolvePreviewSource(backendUrl: string, path?: string | null, dataUrl?: string | null): string | null {
  if (path && path.length > 0) {
    return buildGradioFileUrl(backendUrl, path);
  }
  if (dataUrl && dataUrl.length > 0) {
    return dataUrl;
  }
  return null;
}

function InspectorSection({
  title,
  children,
  compact = false,
}: {
  title: string;
  children: React.ReactNode;
  compact?: boolean;
}) {
  return (
    <section className={compact ? "space-y-2" : "space-y-3"}>
      <h3 className="text-xs font-medium uppercase tracking-[0.08em] text-muted-foreground">{title}</h3>
      {children}
    </section>
  );
}

function KeyValueRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-start justify-between gap-3 text-sm">
      <span className="text-muted-foreground">{label}</span>
      <span className="text-right font-medium text-foreground">{value}</span>
    </div>
  );
}

function MetricTile({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border border-border bg-card px-3 py-3">
      <p className="text-caption text-muted-foreground">{label}</p>
      <p className="mt-1 text-heading-sm font-semibold text-foreground">{value}</p>
    </div>
  );
}

function ArtifactIcon({ mediaType }: { mediaType: string }) {
  if (mediaType.includes("json")) {
    return <FileJson className="h-4 w-4 text-primary" />;
  }
  if (mediaType.includes("image") || mediaType.includes("tif")) {
    return <FileImage className="h-4 w-4 text-accent" />;
  }
  return <Map className="h-4 w-4 text-foreground" />;
}

function localizeRunStatus(text: string, t: (key: string) => string): string {
  switch (text) {
    case "Queued":
      return t("status.waiting");
    case "Running":
    case "Processing":
    case "Starting run":
      return t("status.active");
    case "Completed":
      return t("status.completed");
    case "Run failed":
      return t("status.stage_failed");
    default:
      return text;
  }
}

export function ResultsPanel({ backendUrl }: { backendUrl: string }) {
  const { t } = useI18n();
  const [showDiagnostics, setShowDiagnostics] = useState(false);
  const [selectedPreviewKey, setSelectedPreviewKey] = useState<string | null>(null);

  const settings = useAppStore((state) => state.settings);
  const validation = useAppStore((state) => state.validation);
  const result = useAppStore((state) => state.result);
  const runStatus = useAppStore((state) => state.runStatus);
  const runProgress = useAppStore((state) => state.runProgress);
  const isRunning = useAppStore((state) => state.isRunning);
  const aoi = useAppStore((state) => state.aoi);

  const artifactLinks = useMemo(
    () =>
      result?.artifacts.map((artifact) => ({
        ...artifact,
        href: buildGradioFileUrl(backendUrl, artifact.path),
      })) ?? [],
    [backendUrl, result?.artifacts],
  );

  const previewSources = useMemo(
    () => ({
      t1: resolvePreviewSource(
        backendUrl,
        result?.preview_images?.t1_preview_path,
        result?.preview_images?.t1_preview_png_data_url,
      ),
      t2: resolvePreviewSource(
        backendUrl,
        result?.preview_images?.t2_preview_path,
        result?.preview_images?.t2_preview_png_data_url,
      ),
      changeProbability: resolvePreviewSource(
        backendUrl,
        result?.preview_images?.change_probability_preview_path,
        result?.preview_images?.change_probability_preview_png_data_url,
      ),
      changeOverlay: resolvePreviewSource(
        backendUrl,
        result?.preview_images?.change_overlay_preview_path,
        result?.preview_images?.change_overlay_preview_png_data_url,
      ),
    }),
    [backendUrl, result?.preview_images],
  );

  const previewItems = [
    { key: "t1", label: t("results.preview.t1"), src: previewSources.t1 },
    { key: "t2", label: t("results.preview.t2"), src: previewSources.t2 },
    { key: "changeProbability", label: t("results.preview.probability"), src: previewSources.changeProbability },
    { key: "changeOverlay", label: t("results.preview.overlay"), src: previewSources.changeOverlay },
  ].filter((item) => item.src);
  const selectedPreview = previewItems.find((item) => item.key === selectedPreviewKey) ?? previewItems[0] ?? null;

  const requestSummary = [
    { label: t("results.mode"), value: settings.mode === "fast_preview" ? t("results.fast_preview") : t("results.full_run") },
    { label: t("results.backend"), value: settings.modelBackend === "bandon_mps" ? t("results.bandon_mps") : t("results.sam3") },
    { label: t("results.change_threshold"), value: settings.changeThreshold.toFixed(2) },
    { label: t("results.semantic_threshold"), value: settings.semanticThreshold.toFixed(2) },
    { label: t("results.merge_close_gap"), value: `${settings.mergeCloseGapM} m` },
    { label: t("results.building_block_gap"), value: `${settings.buildingBlockGapM} m` },
    { label: t("results.buffers"), value: settings.bufferDistancesText },
  ];

  const validationTone = validation?.valid ? "emerald" : validation?.blocking_errors.length ? "red" : "amber";

  return (
    <aside className="overflow-hidden rounded-lg border border-border bg-card shadow-panel xl:h-[calc(100vh-146px)] xl:overflow-y-auto">
      <div className="border-b border-border bg-surface px-4 py-4">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 className="text-sm font-semibold tracking-tight text-foreground">{t("results.inspector")}</h2>
            <p className="mt-1 text-sm text-muted-foreground">
              {t("results.inspector_description")}
            </p>
          </div>
          <Badge className="border border-border bg-card text-foreground">
            {result?.success ? t("badge.results") : isRunning ? t("badge.running") : t("badge.setup")}
          </Badge>
        </div>
      </div>

      <div className="space-y-5 p-4">
          {(isRunning || runProgress.phase !== "idle") && <RunProgressPanel progress={runProgress} />}

          {!result ? (
            <>
              <InspectorSection title={t("results.request_summary")}>
                <div className="space-y-2 rounded-md border border-border bg-surface p-3">
                  {requestSummary.map((item) => (
                    <KeyValueRow key={item.label} label={item.label} value={item.value} />
                  ))}
                  <KeyValueRow label={t("result.aoi_status")} value={aoi ? t("status.polygon_ready") : t("status.not_set")} />
                </div>
              </InspectorSection>

              <InspectorSection title={t("results.validation")}>
                {validation ? (
                  <div
                    className={cn(
                      "space-y-3 rounded-md border p-3",
                      validationTone === "emerald" && "border-emerald-300/60 bg-emerald-100/70 dark:border-emerald-500/40 dark:bg-emerald-500/10",
                      validationTone === "red" && "border-red-300/60 bg-red-100/70 dark:border-red-500/40 dark:bg-red-500/10",
                      validationTone === "amber" && "border-amber-300/60 bg-amber-100/70 dark:border-amber-500/40 dark:bg-amber-500/10",
                    )}
                  >
                    <div className="flex items-start gap-2">
                      {validation.valid ? (
                        <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-emerald-700" />
                      ) : (
                        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-700" />
                      )}
                      <div className="text-sm">
                        <p className="font-medium text-foreground">
                          {validation.valid ? t("status.ready_to_run") : validation.blocking_errors.length ? t("status.validation_blocked") : t("status.needs_review")}
                        </p>
                        <p className="mt-1 text-muted-foreground">
                          {formatNumber(validation.estimated_total_tiles)} tiles across both releases ·{" "}
                          {formatNumber(validation.estimated_area_m2)} m²
                        </p>
                      </div>
                    </div>

                    {validation.blocking_errors.map((message) => (
                      <p key={message} className="text-sm text-red-800 dark:text-red-200">
                        {message}
                      </p>
                    ))}

                    {validation.warnings.map((message) => (
                      <p key={message} className="text-sm text-amber-900 dark:text-amber-100">
                        {message}
                      </p>
                    ))}
                  </div>
                ) : (
                  <div className="rounded-md border border-border bg-surface p-3 text-sm text-muted-foreground">
                    {t("info.validation_description")}
                  </div>
                )}
              </InspectorSection>

              <InspectorSection title={t("ui.validation")}>
                <div className="rounded-md border border-border bg-surface p-3 text-sm text-muted-foreground">{localizeRunStatus(runStatus, t)}</div>
              </InspectorSection>
            </>
          ) : !result.success || !result.summary ? (
            <InspectorSection title={t("section.run_error")}>
              <div className="rounded-md border border-red-300/60 bg-red-100/70 p-3 text-sm text-red-800 dark:border-red-500/40 dark:bg-red-500/10 dark:text-red-200">
                {result.error_message ?? t("error.incomplete_response")}
              </div>
            </InspectorSection>
          ) : (
            <>
              <InspectorSection title={t("results.result_summary")}>
                <div className="grid gap-2 sm:grid-cols-2">
                  <MetricTile
                    label={result.summary.result_semantics === "building_change" ? t("metric.change_polygons") : t("metric.new_buildings")}
                    value={formatNumber(
                      result.summary.result_semantics === "building_change"
                        ? (result.summary.total_change_polygons ?? 0)
                        : result.summary.total_new_buildings,
                    )}
                  />
                  <MetricTile label={t("metric.building_blocks")} value={formatNumber(result.summary.total_building_blocks)} />
                  <MetricTile
                    label={result.summary.result_semantics === "building_change" ? t("metric.change_area") : t("metric.new_area")}
                    value={`${formatNumber(
                      result.summary.result_semantics === "building_change"
                        ? (result.summary.total_change_area_m2 ?? 0)
                        : result.summary.total_new_building_area_m2,
                    )} m²`}
                  />
                  <MetricTile label={t("metric.tiles_processed")} value={formatNumber(result.summary.tile_count_t1 + result.summary.tile_count_t2)} />
                </div>
              </InspectorSection>

              {previewItems.length > 0 ? (
                <>
                  <Separator />
                  <InspectorSection title={t("results.preview_images")}>
                    <div className="space-y-3">
                      <div className="flex flex-wrap gap-2">
                        {previewItems.map((item) => (
                          <button
                            key={item.key}
                            type="button"
                            onClick={() => setSelectedPreviewKey(item.key)}
                            className={cn(
                              "rounded-md border px-2.5 py-1.5 text-xs font-medium transition-colors",
                              selectedPreview?.key === item.key
                                ? "border-primary/30 bg-primary/10 text-foreground"
                                : "border-border bg-card text-muted-foreground hover:bg-secondary",
                            )}
                          >
                            {item.label}
                          </button>
                        ))}
                      </div>

                      {selectedPreview ? (
                        <div className="overflow-hidden rounded-md border border-border bg-card">
                          <div className="border-b border-border px-3 py-2 text-xs font-medium text-muted-foreground">{selectedPreview.label}</div>
                          <img src={selectedPreview.src!} alt={selectedPreview.label} className="h-52 w-full object-cover" />
                        </div>
                      ) : null}
                    </div>
                  </InspectorSection>
                </>
              ) : null}

              <Separator />

              <InspectorSection title={t("results.artifacts")}>
                <div className="space-y-2">
                  {result.downloadable_zip_path ? (
                    <a
                      href={buildGradioFileUrl(backendUrl, result.downloadable_zip_path)}
                      target="_blank"
                      rel="noreferrer"
                      className="flex items-center gap-3 rounded-md border border-primary/20 bg-primary/10 px-3 py-3 text-sm transition-colors hover:bg-primary/15"
                    >
                      <Files className="h-4 w-4 text-primary" />
                      <div className="min-w-0 flex-1">
                        <p className="font-medium text-foreground">{t("artifact.package_archive")}</p>
                        <p className="text-xs text-muted-foreground">{t("artifact.package_description")}</p>
                      </div>
                      <Download className="h-4 w-4 text-muted-foreground" />
                    </a>
                  ) : null}

                  {artifactLinks.map((artifact) => (
                    <a
                      key={artifact.name}
                      href={artifact.href}
                      target="_blank"
                      rel="noreferrer"
                      className="flex items-center gap-3 rounded-md border border-border bg-card px-3 py-3 text-sm transition-colors hover:bg-secondary"
                    >
                      <ArtifactIcon mediaType={artifact.media_type} />
                      <div className="min-w-0 flex-1">
                        <p className="truncate font-medium text-foreground">{artifact.description}</p>
                        <p className="truncate text-xs text-muted-foreground">{artifact.name}</p>
                      </div>
                      <Download className="h-4 w-4 text-muted-foreground" />
                    </a>
                  ))}
                </div>
              </InspectorSection>

              {result.diagnostics ? (
                <>
                  <Separator />
                  <InspectorSection title={t("results.diagnostics")} compact>
                    <button
                      type="button"
                      onClick={() => setShowDiagnostics((current) => !current)}
                      className="flex w-full items-center justify-between rounded-md border border-border bg-card px-3 py-2 text-sm"
                    >
                      <span className="text-muted-foreground">{t("section.diagnostics_description")}</span>
                      <ChevronDown className={cn("h-4 w-4 text-muted-foreground transition-transform", showDiagnostics ? "rotate-180" : "")} />
                    </button>
                    {showDiagnostics ? (
                      <div className="space-y-2 rounded-md border border-border bg-surface p-3">
                        {Object.entries(result.diagnostics.stage_seconds).map(([stage, seconds]) => (
                          <KeyValueRow key={stage} label={stage} value={`${seconds.toFixed(2)}s`} />
                        ))}
                        {result.diagnostics.warnings?.map((warning) => (
                          <p key={warning} className="text-sm text-amber-900 dark:text-amber-100">
                            {warning}
                          </p>
                        ))}
                      </div>
                    ) : null}
                  </InspectorSection>
                </>
              ) : null}
            </>
          )}
      </div>
    </aside>
  );
}
