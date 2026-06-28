import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

function source(relativePath: string): string {
  return readFileSync(new URL(relativePath, import.meta.url), "utf8");
}

test("temporal project UI exposes available releases without a latest-source selector", () => {
  const panel = source("./TemporalMosaicPanel.tsx");

  assert.match(panel, /temporal\.available_releases/);
  assert.doesNotMatch(panel, /latest_source|Latest milestone source|Current Mapbox Satellite basemap|mapbox\.satellite/);
});

test("frontend contracts and project creation do not send latest-source fields", () => {
  const contracts = source("../../api/contracts.ts");
  const settings = source("../settings/SettingsPanel.tsx");

  assert.doesNotMatch(contracts, /latest_source|latestMilestoneSource|currentMapboxSatellite/);
  assert.doesNotMatch(settings, /latest_source|latestMilestoneSource|currentMapboxSatellite/);
});

test("overview omits save and export buttons while download exports remain available", () => {
  const panel = source("./TemporalMosaicPanel.tsx");
  const overview = panel.slice(
    panel.indexOf('activePanel === "overview"'),
    panel.indexOf('activePanel === "releases"'),
  );

  assert.doesNotMatch(overview, /temporal\.save_button/);
  assert.doesNotMatch(overview, /temporal\.export_button/);
  assert.doesNotMatch(overview, /handleSave|handleDownloadBundle/);
  assert.match(panel, /activePanel === "downloads"/);
  assert.match(panel, /temporal\.download_bundle/);
  assert.match(panel, /temporal\.download_results/);
  assert.match(panel, /handleDownloadResults/);
});
