import type { FeatureCollection, LineString, Polygon } from "geojson";

export type DrawingVertex = [number, number];
export type DrawingToolMode = "polygon" | "rectangle";
export type DrawingToolPurpose = "aoi" | "export";
export type ProjectAoiOverlayContext = {
  drawingMode: "idle" | "drawing" | "editing";
  isRunning: boolean;
  workflowMode: "pairwise" | "temporal";
  pairwiseResultComplete: boolean;
  temporalOverlayVisible: boolean;
};

export const CLOSE_TOLERANCE_PX = 12;
export const AOI_DRAW_STROKE_COLOR = "#facc15";
export const AOI_DRAW_VERTEX_FILL = "#ffffff";
export const AOI_DRAW_VERTEX_RADIUS = 6;
export const AOI_DRAW_CLOSE_TARGET_RADIUS = 8;
export const AOI_DRAW_STROKE_WIDTH = 3;
export const AOI_DRAW_PREVIEW_FILL_OPACITY = 0.14;

export type DrawingClickResult = {
  vertices: DrawingVertex[];
  complete: boolean;
};
export type DrawingKeyboardAction = "complete" | "cancel" | null;

export function isNearFirstVertex(
  cursorPx: [number, number],
  firstVertexPx: [number, number],
  tolerancePx = CLOSE_TOLERANCE_PX,
): boolean {
  return Math.hypot(cursorPx[0] - firstVertexPx[0], cursorPx[1] - firstVertexPx[1]) <= tolerancePx;
}

export function rectangleVertices(first: DrawingVertex, opposite: DrawingVertex): DrawingVertex[] {
  const minLng = Math.min(first[0], opposite[0]);
  const maxLng = Math.max(first[0], opposite[0]);
  const minLat = Math.min(first[1], opposite[1]);
  const maxLat = Math.max(first[1], opposite[1]);
  return [
    [minLng, minLat],
    [maxLng, minLat],
    [maxLng, maxLat],
    [minLng, maxLat],
  ];
}

export function resolveDrawingClick(
  mode: DrawingToolMode,
  vertices: DrawingVertex[],
  vertex: DrawingVertex,
  nearFirstVertex = false,
): DrawingClickResult {
  if (mode === "rectangle") {
    if (vertices.length === 0) {
      return { vertices: [vertex], complete: false };
    }
    return { vertices: rectangleVertices(vertices[0], vertex), complete: true };
  }
  if (vertices.length >= 3 && nearFirstVertex) {
    return { vertices, complete: true };
  }
  return { vertices: [...vertices, vertex], complete: false };
}

export function drawingHelperMessage(
  mode: DrawingToolMode,
  vertexCount: number,
  nearFirstVertex = false,
): string {
  if (mode === "rectangle") {
    return vertexCount === 0
      ? "Cliquez pour placer le premier coin"
      : "Déplacez la souris puis cliquez pour placer le coin opposé";
  }
  if (vertexCount === 0) {
    return "Cliquez pour placer le premier point";
  }
  if (vertexCount >= 3 && nearFirstVertex) {
    return "Cliquez sur le premier point pour fermer la zone";
  }
  return "Cliquez pour ajouter des points. Cliquez sur le premier point pour fermer la zone.";
}

export function drawingKeyboardAction(key: string, vertexCount: number): DrawingKeyboardAction {
  if (key === "Escape") {
    return "cancel";
  }
  if (key === "Enter" && vertexCount >= 3) {
    return "complete";
  }
  return null;
}

export function drawingPreviewFeatureCollection(
  mode: DrawingToolMode,
  vertices: DrawingVertex[],
  cursor: DrawingVertex | null,
): FeatureCollection {
  if (vertices.length === 0) {
    return { type: "FeatureCollection", features: [] };
  }
  if (mode === "rectangle") {
    if (!cursor || vertices.length !== 1) {
      return { type: "FeatureCollection", features: [] };
    }
    const corners = rectangleVertices(vertices[0], cursor);
    return {
      type: "FeatureCollection",
      features: [{
        type: "Feature",
        geometry: { type: "Polygon", coordinates: [[...corners, corners[0]]] },
        properties: { role: "drawing-preview-fill" },
      }],
    };
  }

  const previewCoordinates = cursor ? [...vertices, cursor] : vertices;
  const features: FeatureCollection["features"] = [];
  if (previewCoordinates.length >= 2) {
    features.push({
      type: "Feature",
      geometry: { type: "LineString", coordinates: previewCoordinates } satisfies LineString,
      properties: { role: "drawing-preview-line" },
    });
  }
  if (previewCoordinates.length >= 3) {
    features.unshift({
      type: "Feature",
      geometry: { type: "Polygon", coordinates: [[...previewCoordinates, previewCoordinates[0]]] },
      properties: { role: "drawing-preview-fill" },
    });
  }
  return { type: "FeatureCollection", features };
}

export function toGeoJsonPolygon(vertices: DrawingVertex[]): Polygon | null {
  if (vertices.length < 3) {
    return null;
  }
  return {
    type: "Polygon",
    coordinates: [[...vertices, vertices[0]]],
  };
}

export function drawingGeometryCommit(purpose: DrawingToolPurpose, polygon: Polygon) {
  return purpose === "export"
    ? { exportGeometry: polygon, exportDrawnGeometry: polygon }
    : { aoi: polygon };
}

export function shouldShowProjectAoiOverlay(context: ProjectAoiOverlayContext): boolean {
  if (context.drawingMode !== "idle" || context.isRunning) {
    return true;
  }
  return context.workflowMode === "temporal"
    ? context.temporalOverlayVisible
    : !context.pairwiseResultComplete;
}
