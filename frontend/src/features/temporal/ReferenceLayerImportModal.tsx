import { useRef, useState } from "react";
import { AlertCircle, CheckCircle, FileUp, Loader2 } from "lucide-react";

import type { ReferenceLayer, ReferenceLayerPreflight, ReferenceLayerScope, ReferenceLayerStrategy } from "@/api/contracts";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { useI18n } from "@/lib/i18n";
import { cn } from "@/lib/utils";

interface ReferenceLayerImportModalProps {
  open: boolean;
  projectId: string | null;
  onOpenChange: (open: boolean) => void;
  onPreflight: (file: File, scope: ReferenceLayerScope) => Promise<ReferenceLayerPreflight>;
  onImport: (
    file: File,
    name: string,
    scope: ReferenceLayerScope,
    renderingStrategy: ReferenceLayerStrategy,
  ) => Promise<ReferenceLayer>;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[unitIndex]}`;
}

export function ReferenceLayerImportModal({
  open,
  projectId,
  onOpenChange,
  onPreflight,
  onImport,
}: ReferenceLayerImportModalProps) {
  const { t } = useI18n();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const dragZoneRef = useRef<HTMLDivElement>(null);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [layerName, setLayerName] = useState("");
  const [scope, setScope] = useState<ReferenceLayerScope>("aoi_clipped");
  const [strategy, setStrategy] = useState<ReferenceLayerStrategy>("auto");
  const [preflight, setPreflight] = useState<ReferenceLayerPreflight | null>(null);
  const [isPreflighting, setIsPreflighting] = useState(false);
  const [isImporting, setIsImporting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reset = () => {
    setSelectedFile(null);
    setLayerName("");
    setScope("aoi_clipped");
    setStrategy("auto");
    setPreflight(null);
    setIsPreflighting(false);
    setIsImporting(false);
    setError(null);
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

  const runPreflight = async (file: File, nextScope: ReferenceLayerScope) => {
    if (!projectId) {
      return;
    }
    setSelectedFile(file);
    setLayerName((current) => current || file.name.replace(/\.[^.]+$/, ""));
    setPreflight(null);
    setError(null);
    setIsPreflighting(true);
    try {
      const result = await onPreflight(file, nextScope);
      setPreflight(result);
      setStrategy(result.storage_strategy === "pmtiles" ? "pmtiles" : result.storage_strategy === "geojson" ? "geojson" : "auto");
    } catch (err) {
      setError(err instanceof Error ? err.message : t("reference_layer.error_preflight"));
    } finally {
      setIsPreflighting(false);
    }
  };

  const handleScopeChange = (value: ReferenceLayerScope) => {
    setScope(value);
    if (selectedFile) {
      void runPreflight(selectedFile, value);
    }
  };

  const handleImport = async () => {
    if (!selectedFile || !projectId || preflight?.errors.length) {
      return;
    }
    setIsImporting(true);
    setError(null);
    try {
      await onImport(selectedFile, layerName.trim() || selectedFile.name, scope, strategy);
      handleClose(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : t("reference_layer.error_import"));
    } finally {
      setIsImporting(false);
    }
  };

  const importDisabled = !selectedFile || !layerName.trim() || isPreflighting || isImporting || Boolean(preflight?.errors.length);

  return (
    <Dialog open={open} onOpenChange={handleClose}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>{t("reference_layer.title")}</DialogTitle>
          <DialogDescription>{t("reference_layer.description")}</DialogDescription>
        </DialogHeader>

        <div className="space-y-5">
          <div
            ref={dragZoneRef}
            role="button"
            tabIndex={isPreflighting || isImporting ? -1 : 0}
            onClick={() => !isPreflighting && !isImporting && fileInputRef.current?.click()}
            onKeyDown={(event) => {
              if ((event.key === "Enter" || event.key === " ") && !isPreflighting && !isImporting) {
                event.preventDefault();
                fileInputRef.current?.click();
              }
            }}
            onDragOver={(event) => {
              event.preventDefault();
              dragZoneRef.current?.classList.add("border-primary", "bg-primary/5");
            }}
            onDragLeave={(event) => {
              event.preventDefault();
              dragZoneRef.current?.classList.remove("border-primary", "bg-primary/5");
            }}
            onDrop={(event) => {
              event.preventDefault();
              dragZoneRef.current?.classList.remove("border-primary", "bg-primary/5");
              const file = event.dataTransfer.files?.[0];
              if (file) {
                void runPreflight(file, scope);
              }
            }}
            aria-busy={isPreflighting}
            className={cn(
              "flex cursor-pointer flex-col items-center justify-center rounded border-2 border-dashed border-border bg-surface px-6 py-8 text-center transition-all hover:border-primary hover:bg-primary/5 focus:ring-2 focus:ring-ring focus:ring-offset-2",
              (isPreflighting || isImporting) && "pointer-events-none cursor-wait opacity-60",
            )}
          >
            {isPreflighting ? <Loader2 className="mb-2 h-6 w-6 animate-spin text-primary" /> : <FileUp className="mb-2 h-6 w-6 text-muted-foreground" />}
            <p className="text-label font-medium text-foreground">
              {selectedFile ? selectedFile.name : t("reference_layer.drop_file")}
            </p>
            <p className="mt-1 text-caption text-muted-foreground">{t("reference_layer.supported_formats")}</p>
            <input
              ref={fileInputRef}
              type="file"
              accept=".geojson,.json,.gpkg,.zip,.shp.zip,.shz,.kml,.kmz,.gpx,.tif,.tiff"
              className="hidden"
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (file) {
                  void runPreflight(file, scope);
                }
              }}
            />
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="reference-layer-name">{t("reference_layer.name")}</Label>
              <Input
                id="reference-layer-name"
                value={layerName}
                onChange={(event) => setLayerName(event.target.value)}
                placeholder={t("reference_layer.name_placeholder")}
              />
            </div>
            <div className="space-y-2">
              <Label>{t("reference_layer.strategy")}</Label>
              <Select
                value={strategy}
                onChange={(event) => setStrategy(event.target.value as ReferenceLayerStrategy)}
                disabled={scope === "full_layer"}
              >
                <option value="auto">{t("reference_layer.strategy_auto")}</option>
                <option value="geojson">{t("reference_layer.strategy_geojson")}</option>
                <option value="pmtiles">{t("reference_layer.strategy_pmtiles")}</option>
              </Select>
            </div>
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <button
              type="button"
              onClick={() => handleScopeChange("aoi_clipped")}
              className={cn(
                "rounded border px-4 py-3 text-left transition-colors",
                scope === "aoi_clipped" ? "border-primary bg-primary/10" : "border-border bg-card hover:bg-surface",
              )}
            >
              <span className="block text-sm font-medium text-foreground">{t("reference_layer.scope_aoi")}</span>
              <span className="mt-1 block text-caption text-muted-foreground">{t("reference_layer.scope_aoi_help")}</span>
            </button>
            <button
              type="button"
              onClick={() => handleScopeChange("full_layer")}
              className={cn(
                "rounded border px-4 py-3 text-left transition-colors",
                scope === "full_layer" ? "border-primary bg-primary/10" : "border-border bg-card hover:bg-surface",
              )}
            >
              <span className="block text-sm font-medium text-foreground">{t("reference_layer.scope_full")}</span>
              <span className="mt-1 block text-caption text-muted-foreground">{t("reference_layer.scope_full_help")}</span>
            </button>
          </div>

          {preflight ? (
            <div className="rounded border border-border bg-card px-4 py-3">
              <div className="mb-3 flex items-center gap-2">
                {preflight.errors.length ? (
                  <AlertCircle className="h-4 w-4 text-destructive" />
                ) : (
                  <CheckCircle className="h-4 w-4 text-green-600" />
                )}
                <p className="text-label font-medium text-foreground">{t("reference_layer.metadata")}</p>
              </div>
              <dl className="grid gap-2 text-caption text-muted-foreground sm:grid-cols-2">
                <div><dt>{t("reference_layer.format")}</dt><dd className="text-foreground">{preflight.original_format}</dd></div>
                <div><dt>{t("reference_layer.kind")}</dt><dd className="text-foreground">{preflight.layer_kind} / {preflight.geometry_type}</dd></div>
                <div><dt>{t("reference_layer.crs")}</dt><dd className="text-foreground">{preflight.crs ?? t("common.unknown")}</dd></div>
                <div><dt>{t("reference_layer.size")}</dt><dd className="text-foreground">{formatBytes(preflight.file_size_bytes)}</dd></div>
                <div><dt>{t("reference_layer.features")}</dt><dd className="text-foreground">{preflight.feature_count ?? t("common.not_available")}</dd></div>
                <div><dt>{t("reference_layer.chosen_strategy")}</dt><dd className="text-foreground">{preflight.storage_strategy}</dd></div>
              </dl>
              {preflight.bounds_wgs84 ? (
                <p className="mt-3 truncate text-caption text-muted-foreground">
                  {t("reference_layer.bounds")}: [{preflight.bounds_wgs84.map((value) => value.toFixed(4)).join(", ")}]
                </p>
              ) : null}
              {Object.keys(preflight.tool_status).length ? (
                <div className="mt-3 space-y-1 text-caption text-muted-foreground">
                  <p>{t("reference_layer.tool_status")}</p>
                  <p>{preflight.tool_status.tippecanoe ? `tippecanoe: ${preflight.tool_status.tippecanoe}` : null}</p>
                  <p>{preflight.tool_status.pmtiles_cli ? `pmtiles: ${preflight.tool_status.pmtiles_cli}` : null}</p>
                  {preflight.tool_status.reason ? <p>{preflight.tool_status.reason}</p> : null}
                </div>
              ) : null}
              {[...preflight.warnings, ...preflight.errors].length ? (
                <div className="mt-3 space-y-1 text-caption">
                  {preflight.warnings.map((warning) => <p key={warning} className="text-amber-700 dark:text-amber-300">{warning}</p>)}
                  {preflight.errors.map((item) => <p key={item} className="text-destructive">{item}</p>)}
                </div>
              ) : null}
            </div>
          ) : null}

          {error ? (
            <div role="alert" className="rounded border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive-foreground">
              {error}
            </div>
          ) : null}
        </div>

        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => handleClose(false)}>
            {t("common.cancel")}
          </Button>
          <Button type="button" onClick={() => void handleImport()} disabled={importDisabled}>
            {isImporting ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <FileUp className="mr-2 h-4 w-4" />}
            {t("reference_layer.import")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
