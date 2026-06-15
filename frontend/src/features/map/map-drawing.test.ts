import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  drawingGeometryCommit,
  drawingKeyboardAction,
  drawingPreviewFeatureCollection,
  isNearFirstVertex,
  resolveDrawingClick,
  shouldShowProjectAoiOverlay,
  toGeoJsonPolygon,
  type DrawingVertex,
} from "./map-drawing.ts";

const triangle: DrawingVertex[] = [[0, 0], [1, 0], [1, 1]];

test("polygon closes when clicking near its first vertex after three points", () => {
  const result = resolveDrawingClick("polygon", triangle, [0.001, 0.001], true);
  assert.equal(result.complete, true);
  assert.deepEqual(result.vertices, triangle);
});

test("polygon does not close before three points", () => {
  const result = resolveDrawingClick("polygon", triangle.slice(0, 2), [0, 0], true);
  assert.equal(result.complete, false);
  assert.equal(result.vertices.length, 3);
});

test("first click creates one fixed vertex", () => {
  const result = resolveDrawingClick("polygon", [], [2, 3]);
  assert.equal(result.complete, false);
  assert.deepEqual(result.vertices, [[2, 3]]);
});

test("additional clicks preserve fixed polygon edges", () => {
  const result = resolveDrawingClick("polygon", [[0, 0], [1, 0]], [1, 1]);
  const preview = drawingPreviewFeatureCollection("polygon", result.vertices, null);
  const line = preview.features.find((feature) => feature.properties?.role === "drawing-preview-line");
  assert.deepEqual(line?.geometry, { type: "LineString", coordinates: [[0, 0], [1, 0], [1, 1]] });
});

test("rectangle completes after its second click", () => {
  const first = resolveDrawingClick("rectangle", [], [2, 3]);
  const second = resolveDrawingClick("rectangle", first.vertices, [5, 7]);
  assert.equal(first.complete, false);
  assert.equal(second.complete, true);
  assert.deepEqual(second.vertices, [[2, 3], [5, 3], [5, 7], [2, 7]]);
});

test("close target uses screen-space tolerance", () => {
  assert.equal(isNearFirstVertex([10, 10], [20, 10]), true);
  assert.equal(isNearFirstVertex([10, 10], [23, 10]), false);
});

test("Escape cancels and Enter remains an optional completion fallback", () => {
  assert.equal(drawingKeyboardAction("Escape", 0), "cancel");
  assert.equal(drawingKeyboardAction("Enter", 2), null);
  assert.equal(drawingKeyboardAction("Enter", 3), "complete");
});

test("polygon preview connects two committed vertices", () => {
  const preview = drawingPreviewFeatureCollection("polygon", [[0, 0], [1, 0]], null);
  assert.deepEqual(preview.features[0]?.geometry, { type: "LineString", coordinates: [[0, 0], [1, 0]] });
});

test("polygon preview line includes the current cursor coordinate", () => {
  const preview = drawingPreviewFeatureCollection("polygon", [[0, 0], [1, 0]], [1, 1]);
  const line = preview.features.find((feature) => feature.properties?.role === "drawing-preview-line");
  assert.deepEqual(line?.geometry, { type: "LineString", coordinates: [[0, 0], [1, 0], [1, 1]] });
});

test("rectangle preview expands from its fixed corner to the current cursor coordinate", () => {
  const preview = drawingPreviewFeatureCollection("rectangle", [[2, 3]], [5, 7]);
  assert.deepEqual(preview.features[0]?.geometry, {
    type: "Polygon",
    coordinates: [[[2, 3], [5, 3], [5, 7], [2, 7], [2, 3]]],
  });
});

test("drawing preview clears when fixed vertices are cleared", () => {
  assert.deepEqual(drawingPreviewFeatureCollection("polygon", [], [1, 1]).features, []);
  assert.deepEqual(drawingPreviewFeatureCollection("rectangle", [], [1, 1]).features, []);
});

test("AOI panels do not render the removed raw temporal help key", () => {
  for (const relativePath of [
    "../workspace/SharedAoiSection.tsx",
    "../temporal/TemporalMosaicPanel.tsx",
    "../settings/SettingsPanel.tsx",
  ]) {
    assert.doesNotMatch(readFileSync(new URL(relativePath, import.meta.url), "utf8"), /temporal\.aoi_help/);
  }
});

test("polygon preview includes a closed transparent fill geometry after three points", () => {
  const preview = drawingPreviewFeatureCollection("polygon", triangle, null);
  const polygon = preview.features.find((feature) => feature.properties?.role === "drawing-preview-fill");
  assert.deepEqual(polygon?.geometry, {
    type: "Polygon",
    coordinates: [[[0, 0], [1, 0], [1, 1], [0, 0]]],
  });
});

test("completed polygon GeoJSON repeats the first coordinate at the end", () => {
  assert.deepEqual(toGeoJsonPolygon(triangle), {
    type: "Polygon",
    coordinates: [[[0, 0], [1, 0], [1, 1], [0, 0]]],
  });
  assert.equal(toGeoJsonPolygon(triangle.slice(0, 2)), null);
});

test("drawing commits keep project AOI and export area separate", () => {
  const polygon: GeoJSON.Polygon = { type: "Polygon", coordinates: [[[0, 0], [1, 0], [1, 1], [0, 0]]] };
  assert.deepEqual(Object.keys(drawingGeometryCommit("aoi", polygon)), ["aoi"]);
  assert.deepEqual(Object.keys(drawingGeometryCommit("export", polygon)), ["exportGeometry", "exportDrawnGeometry"]);
});

test("project AOI overlay remains visible while drawing, editing, or running", () => {
  for (const drawingMode of ["drawing", "editing"] as const) {
    assert.equal(shouldShowProjectAoiOverlay({
      drawingMode,
      isRunning: false,
      workflowMode: "temporal",
      pairwiseResultComplete: false,
      temporalOverlayVisible: false,
    }), true);
  }
  assert.equal(shouldShowProjectAoiOverlay({
    drawingMode: "idle",
    isRunning: true,
    workflowMode: "temporal",
    pairwiseResultComplete: false,
    temporalOverlayVisible: false,
  }), true);
});

test("project AOI overlay is hidden after completed results without affecting result visibility state", () => {
  assert.equal(shouldShowProjectAoiOverlay({
    drawingMode: "idle",
    isRunning: false,
    workflowMode: "pairwise",
    pairwiseResultComplete: true,
    temporalOverlayVisible: true,
  }), false);
  assert.equal(shouldShowProjectAoiOverlay({
    drawingMode: "idle",
    isRunning: false,
    workflowMode: "temporal",
    pairwiseResultComplete: false,
    temporalOverlayVisible: false,
  }), false);
});
