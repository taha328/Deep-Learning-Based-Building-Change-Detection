import assert from "node:assert/strict";
import test from "node:test";

import {
  buildResultsExportJobRequest,
  buildResultsExportPerimeter,
  canDownloadExport,
  formatExportFileSize,
  runResultsExportJobDownload,
  selectedExportGeometry,
  shouldRestoreExportModal,
  type ResultsExportJob,
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

test("shapefile export job request keeps raster package disabled by default", () => {
  assert.deepEqual(buildResultsExportJobRequest("shapefile", { mode: "project_aoi" }, false), {
    format: "shapefile",
    perimeter: { mode: "project_aoi" },
    includeRasters: false,
    includeOfflinePackage: false,
  });
  assert.deepEqual(buildResultsExportJobRequest("shapefile", { mode: "project_aoi" }, true), {
    format: "shapefile",
    perimeter: { mode: "project_aoi" },
    includeRasters: true,
    includeOfflinePackage: true,
  });
});

test("export job polling triggers direct browser download without binary fetch", async () => {
  const originalFetch = globalThis.fetch;
  let fetchCalled = false;
  globalThis.fetch = async () => {
    fetchCalled = true;
    return new Response();
  };
  const seenJobs: string[] = [];
  const downloads: Array<{ url: string; filename: string }> = [];
  const queued: ResultsExportJob = {
    job_id: "job-1",
    project_id: "temporal-demo",
    status: "queued",
    format: "shapefile",
  };
  const running: ResultsExportJob = {
    ...queued,
    status: "running",
  };
  const ready: ResultsExportJob = {
    ...queued,
    status: "succeeded",
    file_size_bytes: 123_456_789,
    filename: "resultats_temporal-demo_results_shapefile.zip",
    download_url: "/api/temporal-projects/temporal-demo/exports/jobs/job-1/download?token=signed",
  };
  const statuses = [running, ready];

  try {
    const result = await runResultsExportJobDownload({
      projectId: "temporal-demo",
      backendUrl: "http://127.0.0.1:8000",
      fallbackFilename: "fallback.zip",
      request: buildResultsExportJobRequest("shapefile", { mode: "project_aoi" }, false),
      createJob: async () => queued,
      getJob: async () => statuses.shift() ?? ready,
      triggerDownload: (url, filename) => downloads.push({ url, filename }),
      onJob: (job) => seenJobs.push(job.status),
      pollIntervalMs: 0,
      sleepForTest: async () => undefined,
    });

    assert.equal(result.status, "succeeded");
    assert.deepEqual(seenJobs, ["queued", "running", "succeeded"]);
    assert.deepEqual(downloads, [{
      url: "http://127.0.0.1:8000/api/temporal-projects/temporal-demo/exports/jobs/job-1/download?token=signed",
      filename: "resultats_temporal-demo_results_shapefile.zip",
    }]);
    assert.equal(fetchCalled, false);
    assert.equal(formatExportFileSize(123_456_789), "117.7 MB");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("export job polling surfaces backend failure messages", async () => {
  await assert.rejects(
    () => runResultsExportJobDownload({
      projectId: "temporal-demo",
      backendUrl: "",
      fallbackFilename: "fallback.zip",
      request: buildResultsExportJobRequest("geojson", { mode: "project_aoi" }, false),
      createJob: async () => ({
        job_id: "job-2",
        project_id: "temporal-demo",
        status: "failed",
        format: "geojson",
        error_message: "Zone export vide.",
      }),
      getJob: async () => {
        throw new Error("should not poll failed job");
      },
      triggerDownload: () => undefined,
      sleepForTest: async () => undefined,
    }),
    /Zone export vide/,
  );
});
