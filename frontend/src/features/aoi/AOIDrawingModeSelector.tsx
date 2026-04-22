import { Pen, Square } from "lucide-react";
import { cn } from "@/lib/utils";
import { useI18n } from "@/lib/i18n";

interface AOIDrawingModeSelectorProps {
  isActive: boolean;
  drawingSubMode: "polygon" | "rectangle";
  onSelectMode: (mode: "polygon" | "rectangle") => void;
  fullWidth?: boolean;
}

export function AOIDrawingModeSelector({
  isActive,
  drawingSubMode,
  onSelectMode,
  fullWidth = false,
}: AOIDrawingModeSelectorProps) {
  const { t } = useI18n();
  return (
    <div
      className={cn(
        "flex gap-2 rounded border border-border bg-surface p-1",
        fullWidth && "w-full"
      )}
    >
      <button
        type="button"
        onClick={() => onSelectMode("polygon")}
        disabled={!isActive}
        className={cn(
          "flex-1 flex items-center justify-center gap-2 rounded px-3 py-2 text-sm font-medium transition",
          isActive
            ? drawingSubMode === "polygon"
              ? "border border-primary/50 bg-primary/10 text-foreground"
              : "border border-transparent bg-transparent text-muted-foreground hover:text-foreground"
            : "border border-transparent bg-transparent text-muted-foreground cursor-not-allowed opacity-50"
        )}
      >
        <Pen className="h-4 w-4" />
        <span>{t("ui.polygon")}</span>
      </button>
      <button
        type="button"
        onClick={() => onSelectMode("rectangle")}
        disabled={!isActive}
        className={cn(
          "flex-1 flex items-center justify-center gap-2 rounded px-3 py-2 text-sm font-medium transition",
          isActive
            ? drawingSubMode === "rectangle"
              ? "border border-primary/50 bg-primary/10 text-foreground"
              : "border border-transparent bg-transparent text-muted-foreground hover:text-foreground"
            : "border border-transparent bg-transparent text-muted-foreground cursor-not-allowed opacity-50"
        )}
      >
        <Square className="h-4 w-4" />
        <span>{t("ui.rectangle")}</span>
      </button>
    </div>
  );
}
