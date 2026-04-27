import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useI18n } from "@/lib/i18n";
import { cn } from "@/lib/utils";

export function ModeSection({
  workflowMode,
  onWorkflowModeChange,
}: {
  workflowMode: "pairwise" | "temporal";
  onWorkflowModeChange: (mode: "pairwise" | "temporal") => void;
}) {
  const { t } = useI18n();

  return (
    <div className="space-y-4 px-5 py-5">
      <Card className="border-sidebar-border bg-sidebar">
        <CardHeader className="space-y-2">
          <CardTitle className="text-foreground">{t("mode.title")}</CardTitle>
          <CardDescription>{t("mode.description")}</CardDescription>
        </CardHeader>
        <CardContent>
          <div role="radiogroup" aria-label={t("mode.workflow_radiogroup")} className="grid grid-cols-2 gap-3">
            <Button
              type="button"
              variant={workflowMode === "pairwise" ? "default" : "outline"}
              role="radio"
              aria-checked={workflowMode === "pairwise"}
              className={cn(
                "justify-start",
                workflowMode !== "pairwise" && "border-sidebar-border bg-sidebar text-foreground hover:bg-surface",
              )}
              onClick={() => onWorkflowModeChange("pairwise")}
            >
              {t("mode.pairwise")}
            </Button>
            <Button
              type="button"
              variant={workflowMode === "temporal" ? "default" : "outline"}
              role="radio"
              aria-checked={workflowMode === "temporal"}
              className={cn(
                "justify-start",
                workflowMode !== "temporal" && "border-sidebar-border bg-sidebar text-foreground hover:bg-surface",
              )}
              onClick={() => onWorkflowModeChange("temporal")}
            >
              {t("mode.temporal_mosaic")}
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
