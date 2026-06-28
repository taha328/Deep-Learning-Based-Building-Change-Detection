import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const panel = readFileSync(new URL("./TemporalMosaicPanel.tsx", import.meta.url), "utf8");
const mapView = readFileSync(new URL("../map/MapView.tsx", import.meta.url), "utf8");

const allowed = [
  "automated_building_blocks",
  "additions",
  "building_change_buffer_10m",
  "building_change_buffer_15m",
  "building_change_buffer_20m",
  "cumulative_building_change_buffer_10m",
  "cumulative_building_change_buffer_15m",
  "cumulative_building_change_buffer_20m",
];

const deprecated = [
  "automated_additions",
  "automated_candidate_footprint",
  "effective_footprint",
  "cumulative_union",
  "cumulative_growth_blocks",
  "cumulative_growth_envelope",
];

test("temporal artifact allowlist contains exactly the approved keys", () => {
  const allowlistSource = panel.match(/const TEMPORAL_ALLOWED_ARTIFACT_KEYS = new Set\(\[([\s\S]*?)\]\);/)?.[1] ?? "";
  const keys = [...allowlistSource.matchAll(/"([^"]+)"/g)].map((match) => match[1]);
  assert.deepEqual(keys, allowed);
});

test("deprecated temporal artifacts are absent from lazy fetch and visible layer registries", () => {
  const lazyRegistry = panel.match(/const TEMPORAL_LAZY_ARTIFACT_FIELDS = \[([\s\S]*?)\] as const;/)?.[1] ?? "";
  const visibleRegistry = mapView.match(/const TEMPORAL_ADDED_LAYER_DEFINITIONS:[\s\S]*?= \[([\s\S]*?)\];/)?.[1] ?? "";
  for (const key of deprecated) {
    assert.doesNotMatch(lazyRegistry, new RegExp(key));
  }
  assert.doesNotMatch(visibleRegistry, /kind: "automated"/);
  assert.doesNotMatch(visibleRegistry, /kind: "effectiveBuildingBlocks"/);
  assert.doesNotMatch(visibleRegistry, /kind: "cumulative"/);
  assert.doesNotMatch(visibleRegistry, /kind: "cumulativeGrowthBlocks"/);
  assert.doesNotMatch(visibleRegistry, /kind: "cumulativeGrowthEnvelope"/);
});

test("automated building blocks remains fetchable and visible", () => {
  assert.match(panel, /\["automated_building_blocks", "automated_building_blocks_geojson"\]/);
  assert.match(mapView, /kind: "automatedBuildingBlocks"/);
});
