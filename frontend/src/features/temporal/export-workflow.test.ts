import assert from "node:assert/strict";
import test from "node:test";

import {
  buildResultsExportPerimeter,
  canDownloadExport,
  selectedExportGeometry,
  shouldRestoreExportModal,
} from "./export-workflow.ts";

const polygon: GeoJSON.Polygon = {
  type: "Polygon",
  coordinates: [[[0, 0], [1, 0], [1, 1], [0, 0]]],
};

test("full-project export is immediately downloadable", () => {
  assert.equal(canDownloadExport("project_aoi", false, false), true);
});

test("drawn export requires drawn geometry only", () => {
  assert.equal(canDownloadExport("drawn", false, true), false);
  assert.equal(canDownloadExport("drawn", true, false), true);
});

test("imported export requires imported geometry only", () => {
  assert.equal(canDownloadExport("imported", true, false), false);
  assert.equal(canDownloadExport("imported", false, true), true);
});

test("completed drawings restore the export modal", () => {
  assert.equal(shouldRestoreExportModal("drawing_polygon"), false);
  assert.equal(shouldRestoreExportModal("drawing_rectangle"), false);
  assert.equal(shouldRestoreExportModal("completed"), true);
});

test("cancelled drawing restores the export modal", () => {
  assert.equal(shouldRestoreExportModal("cancelled"), true);
  assert.equal(shouldRestoreExportModal("idle"), false);
});

test("temporary export overlay can hide without deleting selected clipping geometry", () => {
  assert.equal(selectedExportGeometry("drawn", polygon, null), polygon);
  assert.equal(selectedExportGeometry("project_aoi", polygon, null), null);
});

test("specific-zone export request still includes retained selected geometry", () => {
  assert.deepEqual(buildResultsExportPerimeter("drawn", polygon, null), {
    mode: "custom_geometry",
    source: "drawn",
    geometry: polygon,
  });
  assert.deepEqual(buildResultsExportPerimeter("project_aoi", polygon, null), { mode: "project_aoi" });
});
