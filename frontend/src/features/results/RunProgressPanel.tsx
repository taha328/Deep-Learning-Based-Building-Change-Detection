import { CheckCircle2, Clock3, Loader2, XCircle } from "lucide-react";

import { useI18n } from "@/lib/i18n";
import { Progress } from "@/components/ui/progress";
import { TemporalVerticalProgressTimeline } from "@/features/results/TemporalVerticalProgressTimeline";
import {
  PIPELINE_STAGES,
  formatRunStatus,
  getStageState,
  type RunProgressState,
} from "@/lib/run-progress";
import { cn } from "@/lib/utils";

function formatEta(etaSeconds: number | null): string | null {
  if (etaSeconds === null) {
    return null;
  }
  const rounded = Math.max(0, Math.round(etaSeconds));
  if (rounded < 60) {
    return `${rounded}s`;
  }
  const minutes = Math.floor(rounded / 60);
  const seconds = rounded % 60;
  return seconds === 0 ? `${minutes}m` : `${minutes}m ${seconds}s`;
}

function localizeProgressDetail(detail: string, t: (key: string) => string): string {
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

function tileProgressPercent(progress: RunProgressState): number | null {
  const details = progress.tileDetails;
  if (!details || details.totalTileCount <= 0) {
    return null;
  }
  return Math.max(0, Math.min(100, (details.processedTileCount / details.totalTileCount) * 100));
}

export function RunProgressPanel({ progress, variant = "default" }: { progress: RunProgressState; variant?: "default" | "temporal" }) {
  const { t } = useI18n();
  const eta = formatEta(progress.etaSeconds);
  const tileEta = formatEta(progress.tileDetails?.etaSeconds ?? null);
  const statusText = formatRunStatus(progress, t);
  const visibleStages = PIPELINE_STAGES.filter((stage) => stage.key !== "queue");
  const imagePreparationPercent = tileProgressPercent(progress);

  if (variant === "temporal" || progress.temporalPairDetails) {
    return <TemporalVerticalProgressTimeline progress={progress} />;
  }

  return (
    <section className="space-y-4 rounded-lg border border-border bg-surface p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-sm font-medium text-foreground">{t("results.run_progress")}</p>
          <p className="mt-1 text-sm text-muted-foreground">{statusText}</p>
        </div>
        <div className="text-right text-xs text-muted-foreground">
          <p>{Math.round(progress.percent)}%</p>
          <p>{eta ? `${t("progress.eta")} ${eta}` : progress.phase === "queued" ? t("status.waiting") : t("status.active")}</p>
        </div>
      </div>

      <Progress value={progress.percent} className="h-2 bg-secondary" indicatorClassName="bg-primary" />

      {progress.tileDetails ? (
        <div className="space-y-2 rounded-md border border-border bg-card px-3 py-3 text-xs text-muted-foreground">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <p className="font-medium text-foreground">Préparation des images satellite</p>
              <p className="mt-1">
                {progress.tileDetails.fallbackApplied
                  ? "Les images disponibles sont préparées à une résolution compatible avec la zone sélectionnée."
                  : "Les images nécessaires à l'analyse sont en cours de préparation."}
              </p>
            </div>
            <p className="text-right">
              {imagePreparationPercent !== null
                ? `Avancement des images : ${Math.round(imagePreparationPercent)} %`
                : "Préparation en cours"}
            </p>
          </div>
          {imagePreparationPercent !== null ? (
            <Progress value={imagePreparationPercent} className="h-2 bg-secondary" indicatorClassName="bg-primary" />
          ) : null}
          <p>{tileEta ? `Temps estimé : ${tileEta}` : "Le temps restant sera affiché dès qu'il sera disponible."}</p>
        </div>
      ) : null}

      <div className="space-y-2">
        {visibleStages.map((stage) => {
          const state = getStageState(progress, stage);

          return (
            <div key={stage.key} className="flex items-center gap-3 rounded-md border border-border bg-card px-3 py-2.5">
              <div
                className={cn(
                  "flex h-7 w-7 items-center justify-center rounded-full border",
                  state === "complete" && "border-emerald-300 bg-emerald-100 dark:border-emerald-500/40 dark:bg-emerald-500/10",
                  state === "current" && "border-primary/30 bg-primary/10",
                  state === "pending" && "border-border bg-secondary",
                )}
              >
                {state === "complete" ? (
                  <CheckCircle2 className="h-4 w-4 text-emerald-700" />
                ) : state === "current" ? (
                  <Loader2 className="h-4 w-4 animate-spin text-primary" />
                ) : progress.phase === "error" && progress.stageLabel.toLowerCase().includes(stage.label.toLowerCase()) ? (
                  <XCircle className="h-4 w-4 text-red-700" />
                ) : (
                  <Clock3 className="h-4 w-4 text-muted-foreground" />
                )}
              </div>

              <div className="min-w-0 flex-1">
                <p className="text-label font-medium text-foreground">{t(stage.translationKey as any)}</p>
                <p
                  className={cn(
                    "text-caption font-medium",
                    state === "complete" && "text-green-700 dark:text-green-400",
                    state === "current" && "text-primary",
                    state === "pending" && "text-muted-foreground",
                  )}
                >
                  {state === "complete" ? t("status.completed") : state === "current" ? t("status.current_stage") : t("status.pending")}
                </p>
              </div>
            </div>
          );
        })}
      </div>

      <p className="rounded-md border border-border bg-card px-3 py-2 text-xs text-muted-foreground">{localizeProgressDetail(progress.detail, t)}</p>
    </section>
  );
}
