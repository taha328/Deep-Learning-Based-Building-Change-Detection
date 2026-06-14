import type { Polygon } from "geojson";

export type ExportPerimeterMode = "project_aoi" | "drawn" | "imported";
export type ExportDrawingPhase = "idle" | "drawing_polygon" | "drawing_rectangle" | "completed" | "cancelled";
export type ResultsExportPerimeter =
  | { mode: "project_aoi" }
  | { mode: "custom_geometry"; source: "drawn" | "imported"; geometry: Polygon | null };

export function canDownloadExport(
  perimeterMode: ExportPerimeterMode,
  hasDrawnGeometry: boolean,
  hasImportedGeometry: boolean,
): boolean {
  if (perimeterMode === "project_aoi") {
    return true;
  }
  return perimeterMode === "drawn" ? hasDrawnGeometry : hasImportedGeometry;
}

export function shouldRestoreExportModal(phase: ExportDrawingPhase): boolean {
  return phase === "completed" || phase === "cancelled";
}

export function selectedExportGeometry(
  perimeterMode: ExportPerimeterMode,
  drawnGeometry: Polygon | null,
  importedGeometry: Polygon | null,
): Polygon | null {
  return perimeterMode === "drawn"
    ? drawnGeometry
    : perimeterMode === "imported"
      ? importedGeometry
      : null;
}

export function buildResultsExportPerimeter(
  perimeterMode: ExportPerimeterMode,
  drawnGeometry: Polygon | null,
  importedGeometry: Polygon | null,
): ResultsExportPerimeter {
  if (perimeterMode === "project_aoi") {
    return { mode: "project_aoi" };
  }
  return {
    mode: "custom_geometry",
    source: perimeterMode,
    geometry: selectedExportGeometry(perimeterMode, drawnGeometry, importedGeometry),
  };
}
