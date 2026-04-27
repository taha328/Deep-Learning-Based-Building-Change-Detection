import { Upload } from "lucide-react";

import { Button } from "@/components/ui/button";
import { AOIDrawingModeSelector } from "@/features/aoi/AOIDrawingModeSelector";
import { useI18n } from "@/lib/i18n";
import { cn } from "@/lib/utils";

export function SharedAoiSection({
  sectionTitle,
  readyText,
  emptyText,
  helpText,
  drawingSubMode,
  drawingMode,
  aoiReady,
  vertexCount,
  onSelectMode,
  onStartDrawing,
  onStartEditing,
  onClear,
  onImport,
  importLabel,
}: {
  sectionTitle: string;
  readyText: string;
  emptyText: string;
  helpText: string;
  drawingSubMode: "polygon" | "rectangle";
  drawingMode: "idle" | "drawing" | "editing";
  aoiReady: boolean;
  vertexCount: number;
  onSelectMode: (mode: "polygon" | "rectangle") => void;
  onStartDrawing: () => void;
  onStartEditing: () => void;
  onClear: () => void;
  onImport?: () => void;
  importLabel?: string;
}) {
  const { t } = useI18n();

  return (
    <div className="space-y-6 px-5 py-5">
      <div className="space-y-3 rounded border border-sidebar-border bg-sidebar px-4 py-4">
        <div className="flex items-center justify-between">
          <label className="label-xs">{sectionTitle}</label>
          {aoiReady ? (
            <span className="label-xs-upper-accent rounded-full bg-emerald-500/20 px-2 py-1 text-emerald-300">
              {t("aoi.active")}
            </span>
          ) : null}
        </div>

        <div className="rounded border border-sidebar-border bg-surface px-4 py-3 text-sm text-foreground">
          {aoiReady ? readyText : emptyText}
        </div>

        <AOIDrawingModeSelector
          isActive={drawingMode !== "editing"}
          drawingSubMode={drawingSubMode}
          onSelectMode={onSelectMode}
          fullWidth
        />

        <div className="grid grid-cols-2 gap-3">
          <Button variant="outline" className="border-sidebar-border bg-sidebar" onClick={onStartDrawing}>
            {t("aoi.draw")}
          </Button>
          <Button variant="outline" className="border-sidebar-border bg-sidebar" onClick={onStartEditing} disabled={!aoiReady}>
            {t("aoi.edit")}
          </Button>
          <Button
            variant="outline"
            className={cn(
              "border-sidebar-border bg-sidebar",
              (aoiReady || drawingMode !== "idle") && "border-red-600/50 text-red-300 hover:bg-red-900/20",
            )}
            onClick={onClear}
            disabled={!aoiReady && drawingMode === "idle"}
          >
            {t("aoi.clear")}
          </Button>
          {onImport ? (
            <Button variant="outline" className="border-sidebar-border bg-sidebar" onClick={onImport}>
              <Upload className="mr-2 h-4 w-4" />
              {importLabel || t("aoi.import")}
            </Button>
          ) : null}
        </div>

        <div className="grid grid-cols-2 gap-4 rounded border border-sidebar-border bg-surface px-4 py-4">
          <div>
            <p className="label-xs">{t("aoi.status")}</p>
            <div className="mt-3 flex items-center gap-2">
              <div className={cn("h-2 w-2 rounded-full", aoiReady ? "bg-emerald-400" : "bg-slate-600")} />
              <p className="text-heading-sm text-foreground">
                {aoiReady ? t("aoi.active_status") : drawingMode === "drawing" ? t("aoi.drawing_status") : t("aoi.missing_status")}
              </p>
            </div>
          </div>
          <div>
            <p className="label-xs">{t("aoi.vertices_label")}</p>
            <p className="mt-3 text-heading-sm text-foreground">{vertexCount}</p>
          </div>
        </div>

        <div className="rounded border border-sidebar-border bg-surface px-4 py-4 text-sm leading-6 text-muted-foreground">
          {helpText}
        </div>
      </div>
    </div>
  );
}
