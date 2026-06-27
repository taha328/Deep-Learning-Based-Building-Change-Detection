import { useState, useRef } from "react";
import { Upload, AlertCircle, CheckCircle } from "lucide-react";
import { cn } from "@/lib/utils";
import { useI18n } from "@/lib/i18n";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type { ParseOutput } from "@/utils/aoi-import";
import { parseGeometryFile, parseGeoJSON, parseWKT } from "@/utils/aoi-import";
import type { Polygon } from "geojson";

interface AOIImportModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onImport: (geometry: Polygon, bounds: [number, number, number, number]) => void;
}

export function AOIImportModal({ open, onOpenChange, onImport }: AOIImportModalProps) {
  const { t } = useI18n();
  const [parseResult, setParseResult] = useState<ParseOutput | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [textInput, setTextInput] = useState("");
  const fileInputRef = useRef<HTMLInputElement>(null);
  const dragZoneRef = useRef<HTMLDivElement>(null);

  const handleFileSelect = async (file: File) => {
    setIsLoading(true);
    setTextInput(""); // Clear text input when file is selected
    const result = await parseGeometryFile(file);
    setParseResult(result);
    setIsLoading(false);
  };

  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (dragZoneRef.current) {
      dragZoneRef.current.classList.add("border-blue-500", "bg-blue-500/5");
    }
  };

  const handleDragLeave = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (dragZoneRef.current) {
      dragZoneRef.current.classList.remove("border-blue-500", "bg-blue-500/5");
    }
  };

  const handleDrop = async (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (dragZoneRef.current) {
      dragZoneRef.current.classList.remove("border-blue-500", "bg-blue-500/5");
    }

    const files = e.dataTransfer.files;
    if (files.length > 0) {
      await handleFileSelect(files[0]);
    }
  };

  const handleTextInputChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const text = e.target.value;
    setTextInput(text);

    // Auto-parse as we type
    if (text.trim()) {
      const trimmed = text.trim();
      // Try GeoJSON first
      if (trimmed.startsWith("{")) {
        const result = parseGeoJSON(text).then((r) => {
          setParseResult(r);
        });
      }
      // Try WKT
      else if (trimmed.toUpperCase().includes("POLYGON")) {
        parseWKT(text).then((r) => {
          setParseResult(r);
        });
      } else {
        setParseResult(null);
      }
    } else {
      setParseResult(null);
    }
  };

  const handleImport = () => {
    if (parseResult?.valid) {
      onImport(parseResult.geometry, parseResult.bounds);
      onOpenChange(false);
      setParseResult(null);
      setTextInput("");
      if (fileInputRef.current) {
        fileInputRef.current.value = "";
      }
    }
  };

  const handleClose = () => {
    onOpenChange(false);
    setParseResult(null);
    setTextInput("");
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
    }
  };

  return (
    <Dialog open={open} onOpenChange={handleClose}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>{t("aoi.import_modal_title")}</DialogTitle>
          <DialogDescription>
            {t("aoi.import_modal_description")}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-6">
          {/* File Upload Drop Zone */}
          <div>
            <label htmlFor="file-upload" className="label-xs block mb-2">
              {t("aoi.upload_shapefile")}
            </label>
            <div
              ref={dragZoneRef}
              onDragOver={handleDragOver}
              onDragLeave={handleDragLeave}
              onDrop={handleDrop}
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
                "mt-3 flex flex-col items-center justify-center rounded border-2 border-dashed border-border bg-surface px-6 py-8 cursor-pointer transition-all hover:border-primary hover:bg-primary/5 focus-within:ring-2 focus-within:ring-ring focus-within:ring-offset-2",
                isLoading && "opacity-50 cursor-wait pointer-events-none"
              )}
            >
              <Upload className={cn("mb-2 h-6 w-6 text-muted-foreground transition-colors", isLoading && "animate-pulse")} />
              <p className="text-center text-label font-medium text-foreground">
                {isLoading ? t("common.loading") : t("instruction.drag_drop_aoi")}
              </p>
              <p className="mt-1 text-caption text-muted-foreground">
                {t("help.aoi_import_formats")}
              </p>
              <input
                id="file-upload"
                ref={fileInputRef}
                type="file"
                accept=".json,.geojson,.wkt,.txt,.kml,.kmz,.gpx,.zip,.shp"
                onChange={(e) => {
                  if (e.target.files?.[0]) {
                    handleFileSelect(e.target.files[0]);
                  }
                }}
                className="hidden"
                aria-hidden="true"
              />
            </div>
          </div>

          {/* Text Input Area */}
          <div>
            <label htmlFor="geometry-input" className="label-xs block mb-2">
              {t("label.paste_geometry")}
            </label>
            <textarea
              id="geometry-input"
              value={textInput}
              onChange={handleTextInputChange}
              placeholder={t("aoi.import_placeholder")}
              className={cn(
                "mt-3 w-full rounded border border-border bg-card px-4 py-3 font-mono text-caption text-foreground placeholder-muted-foreground transition-colors focus:border-primary focus:outline-none focus:ring-2 focus:ring-ring/50",
                "min-h-[120px]"
              )}
            />
          </div>

          {/* Validation Feedback */}
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
            onClick={handleClose}
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
