import { useRef, useState } from "react";
import type { Polygon } from "geojson";
import { AlertCircle, CheckCircle, Upload } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { cn } from "@/lib/utils";
import { useI18n } from "@/lib/i18n";
import type { ParseOutput } from "@/utils/aoi-import";
import { parseGeometryFile, parseGeoJSON, parseWKT } from "@/utils/aoi-import";

interface GeometryImportModalProps {
  open: boolean;
  title: string;
  description: string;
  onOpenChange: (open: boolean) => void;
  onImport: (geometry: Polygon) => void;
}

export function GeometryImportModal({
  open,
  title,
  description,
  onOpenChange,
  onImport,
}: GeometryImportModalProps) {
  const { t } = useI18n();
  const [parseResult, setParseResult] = useState<ParseOutput | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [textInput, setTextInput] = useState("");
  const fileInputRef = useRef<HTMLInputElement>(null);
  const dragZoneRef = useRef<HTMLDivElement>(null);

  const reset = () => {
    setParseResult(null);
    setTextInput("");
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
    }
  };

  const handleClose = (nextOpen: boolean) => {
    onOpenChange(nextOpen);
    if (!nextOpen) {
      reset();
    }
  };

  const handleFileSelect = async (file: File) => {
    setIsLoading(true);
    setTextInput("");
    const result = await parseGeometryFile(file);
    setParseResult(result);
    setIsLoading(false);
  };

  const handleImport = () => {
    if (!parseResult?.valid) {
      return;
    }
    onImport(parseResult.geometry);
    handleClose(false);
  };

  return (
    <Dialog open={open} onOpenChange={handleClose}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>

        <div className="space-y-6">
          <div
            ref={dragZoneRef}
            onDragOver={(e) => {
              e.preventDefault();
              e.stopPropagation();
              dragZoneRef.current?.classList.add("border-primary", "bg-primary/5");
            }}
            onDragLeave={(e) => {
              e.preventDefault();
              e.stopPropagation();
              dragZoneRef.current?.classList.remove("border-primary", "bg-primary/5");
            }}
            onDrop={async (e) => {
              e.preventDefault();
              e.stopPropagation();
              dragZoneRef.current?.classList.remove("border-primary", "bg-primary/5");
              if (e.dataTransfer.files?.[0]) {
                await handleFileSelect(e.dataTransfer.files[0]);
              }
            }}
            onClick={() => !isLoading && fileInputRef.current?.click()}
            role="button"
            tabIndex={isLoading ? -1 : 0}
            onKeyDown={(e) => {
              if ((e.key === "Enter" || e.key === " ") && !isLoading) {
                e.preventDefault();
                fileInputRef.current?.click();
              }
            }}
            aria-busy={isLoading}
            aria-label={t("aoi.upload_shapefile")}
            className={cn(
              "flex cursor-pointer flex-col items-center justify-center rounded border-2 border-dashed border-border bg-surface px-6 py-8 text-center transition-all hover:border-primary hover:bg-primary/5 focus:ring-2 focus:ring-ring focus:ring-offset-2",
              isLoading && "cursor-wait opacity-50 pointer-events-none"
            )}
          >
            <Upload className={cn("mb-2 h-6 w-6 text-muted-foreground transition-colors", isLoading && "animate-pulse")} />
            <p className="text-label font-medium text-foreground">
              {isLoading ? t("common.loading") : t("instruction.drag_drop_aoi")}
            </p>
            <p className="mt-1 text-caption text-muted-foreground">
              {t("help.aoi_import_formats")}
            </p>
            <input
              id="geometry-file-upload"
              ref={fileInputRef}
              type="file"
              accept=".json,.geojson,.wkt,.txt,.kml,.kmz,.gpx,.zip,.shp"
              onChange={(e) => {
                if (e.target.files?.[0]) {
                  void handleFileSelect(e.target.files[0]);
                }
              }}
              className="hidden"
              aria-hidden="true"
            />
          </div>

          <div>
            <label htmlFor="geometry-text-input" className="label-xs block mb-2">
              {t("label.paste_geometry")}
            </label>
            <textarea
              id="geometry-text-input"
              value={textInput}
              onChange={(e) => {
                const text = e.target.value;
                setTextInput(text);

                if (text.trim()) {
                  const trimmed = text.trim();
                  if (trimmed.startsWith("{")) {
                    void parseGeoJSON(text).then(setParseResult);
                  } else if (trimmed.toUpperCase().includes("POLYGON")) {
                    void parseWKT(text).then(setParseResult);
                  } else {
                    setParseResult(null);
                  }
                } else {
                  setParseResult(null);
                }
              }}
              placeholder={t("aoi.import_placeholder")}
              className={cn(
                "mt-3 w-full rounded border border-border bg-card px-4 py-3 font-mono text-caption text-foreground placeholder-muted-foreground transition-colors focus:border-primary focus:outline-none focus:ring-2 focus:ring-ring/50",
                "min-h-[120px]"
              )}
            />
          </div>

          {parseResult && (
            <div
              role="alert"
              className={cn(
                "rounded border px-4 py-3 animate-in fade-in-0 duration-200",
                parseResult.valid
                  ? "border-green-300/50 bg-green-500/10 dark:border-green-600/40 dark:bg-green-900/15"
                  : "border-red-300/50 bg-red-500/10 dark:border-red-600/40 dark:bg-red-900/15"
              )}
            >
              <div className="flex items-start gap-3">
                {parseResult.valid ? (
                  <CheckCircle className="mt-0.5 h-5 w-5 text-green-600 dark:text-green-500 flex-shrink-0" />
                ) : (
                  <AlertCircle className="mt-0.5 h-5 w-5 text-red-600 dark:text-red-500 flex-shrink-0" />
                )}
                <div className="flex-1 min-w-0">
                  {parseResult.valid ? (
                    <div className="space-y-1">
                      <p className="text-label font-medium text-foreground">Géométrie importée avec succès.</p>
                      <p className="text-caption text-muted-foreground truncate">
                        Emprise détectée : [{parseResult.bounds[0].toFixed(4)}, {parseResult.bounds[1].toFixed(4)}, {parseResult.bounds[2].toFixed(4)}, {parseResult.bounds[3].toFixed(4)}]
                      </p>
                    </div>
                  ) : (
                    <div>
                      <p className="text-label font-medium text-red-900 dark:text-red-200">
                        {parseResult.format ? "Impossible de lire la géométrie." : "Géométrie invalide."}
                      </p>
                      <p className="mt-1 text-caption text-red-800 dark:text-red-300">
                        Vérifiez le fichier importé ou le contenu collé.
                      </p>
                    </div>
                  )}
                </div>
              </div>
            </div>
          )}
        </div>

        <DialogFooter>
          <button
            type="button"
            onClick={() => handleClose(false)}
            className="rounded border border-border bg-transparent px-4 py-2 text-label font-medium text-foreground transition-colors hover:bg-muted focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2"
          >
            {t("common.cancel")}
          </button>
          <button
            type="button"
            onClick={handleImport}
            disabled={!parseResult?.valid || isLoading}
            className={cn(
              "rounded border px-4 py-2 text-label font-medium transition-all focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2 disabled:cursor-not-allowed",
              parseResult?.valid && !isLoading
                ? "border-primary bg-primary/10 text-foreground hover:bg-primary/20 active:bg-primary/30"
                : "border-border bg-muted text-muted-foreground opacity-50"
            )}
          >
            {t("common.import")}
          </button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
