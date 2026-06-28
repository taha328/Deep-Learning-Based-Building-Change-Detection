import { CheckCircle2, Clock3, Loader2, XCircle } from "lucide-react";

import { Progress } from "@/components/ui/progress";
import {
  buildTemporalProgressTimeline,
  type RunProgressState,
  type TemporalTimelineStage,
} from "@/lib/run-progress";
import { cn } from "@/lib/utils";

function StageMarker({ stage }: { stage: TemporalTimelineStage }) {
  return (
    <div
      className={cn(
        "relative z-10 flex h-7 w-7 shrink-0 items-center justify-center rounded-full border bg-card",
        stage.state === "complete" && "border-green-600/40 text-green-700 dark:text-green-400",
        stage.state === "current" && "border-primary/50 text-primary",
        stage.state === "pending" && "border-border text-muted-foreground",
        stage.state === "failed" && "border-destructive/60 text-destructive",
      )}
    >
      {stage.state === "complete" ? (
        <CheckCircle2 className="h-4 w-4" />
      ) : stage.state === "current" ? (
        <Loader2 className="h-4 w-4 animate-spin" />
      ) : stage.state === "failed" ? (
        <XCircle className="h-4 w-4" />
      ) : (
        <Clock3 className="h-4 w-4" />
      )}
    </div>
  );
}

function StageRow({
  stage,
  isLast,
  showAnalysisDetails,
  globalPercent,
  pairPercent,
  pairLabel,
  pairStepLabel,
}: {
  stage: TemporalTimelineStage;
  isLast: boolean;
  showAnalysisDetails: boolean;
  globalPercent: number | null;
  pairPercent: number | null;
  pairLabel: string;
  pairStepLabel: string;
}) {
  return (
    <li className="relative flex gap-3 pb-4 last:pb-0">
      {!isLast ? <div className="absolute left-[13px] top-7 h-[calc(100%-1.75rem)] w-px bg-border" /> : null}
      <StageMarker stage={stage} />
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 items-start justify-between gap-3">
          <div className="min-w-0">
            <p
              className={cn(
                "text-sm font-medium",
                stage.state === "pending" ? "text-muted-foreground" : "text-foreground",
                stage.state === "failed" && "text-destructive",
              )}
            >
              {stage.label}
            </p>
            {stage.description ? (
              <p className="mt-0.5 text-xs leading-5 text-muted-foreground">{stage.description}</p>
            ) : null}
          </div>
          <p
            className={cn(
              "shrink-0 text-xs",
              stage.state === "complete" && "text-green-700 dark:text-green-400",
              stage.state === "current" && "text-primary",
              stage.state === "pending" && "text-muted-foreground",
              stage.state === "failed" && "text-destructive",
            )}
          >
            {stage.state === "complete"
              ? "Terminé"
              : stage.state === "current"
                ? "En cours"
                : stage.state === "failed"
                  ? "Erreur"
                  : "À venir"}
          </p>
        </div>

        {showAnalysisDetails ? (
          <div className="mt-3 space-y-3 rounded-md border border-border bg-surface px-3 py-3">
            <div className="flex items-start justify-between gap-3 text-xs">
              <div>
                <p className="font-medium text-foreground">{pairStepLabel}</p>
                <p className="mt-1 text-muted-foreground">{pairLabel}</p>
              </div>
              <p className="text-right text-muted-foreground">
                Analyse globale en cours
              </p>
            </div>
            {globalPercent !== null ? <Progress value={globalPercent} className="h-1.5 bg-secondary" indicatorClassName="bg-primary" /> : null}
            <div className="flex items-center justify-between gap-3 text-xs text-muted-foreground">
              <span>Analyse de la période</span>
              <span>En cours</span>
            </div>
            {pairPercent !== null ? <Progress value={pairPercent} className="h-1.5 bg-secondary" indicatorClassName="bg-primary" /> : null}
          </div>
        ) : null}
      </div>
    </li>
  );
}

export function TemporalVerticalProgressTimeline({ progress }: { progress: RunProgressState }) {
  const timeline = buildTemporalProgressTimeline(progress);

  return (
    <section className="space-y-4 rounded-lg border border-border bg-surface p-4">
      <div className="rounded-md border border-border bg-card px-3 py-3">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="text-sm font-medium text-foreground">Progression du projet</p>
            <p className="mt-1 text-sm text-foreground">{timeline.summaryLabel}</p>
            <p className="mt-1 text-xs leading-5 text-muted-foreground">{timeline.summaryDetail}</p>
          </div>
          <p className="shrink-0 text-right text-xs text-muted-foreground">
            {timeline.pairStepLabel}
          </p>
        </div>
      </div>

      <ol className="space-y-0">
        {timeline.stages.map((stage, index) => (
          <StageRow
            key={stage.id}
            stage={stage}
            isLast={index === timeline.stages.length - 1}
            showAnalysisDetails={stage.id === "inference"}
            globalPercent={timeline.globalPercent}
            pairPercent={timeline.pairPercent}
            pairLabel={timeline.pairLabel}
            pairStepLabel={timeline.pairStepLabel}
          />
        ))}
      </ol>

      <div
        className={cn(
          "rounded-md border px-3 py-3",
          timeline.ready && "border-green-600/30 bg-green-600/10",
          timeline.failed && "border-red-300 bg-red-50 dark:border-red-500/40 dark:bg-red-950/30",
          !timeline.ready && !timeline.failed && "border-border bg-card",
        )}
      >
        <p className={cn("text-sm font-medium", timeline.failed ? "text-red-950 dark:text-red-100" : "text-foreground")}>{timeline.readinessLabel}</p>
        <p className="mt-1 text-xs leading-5 text-muted-foreground">{timeline.readinessDetail}</p>
        {!timeline.ready && !timeline.failed && timeline.currentStageNote ? (
          <p className="mt-2 text-xs text-muted-foreground">{timeline.currentStageNote}</p>
        ) : null}
      </div>
    </section>
  );
}
