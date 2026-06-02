import assert from "node:assert/strict";
import { test } from "node:test";

import {
  dedupeStable,
  mapStyleValueEquals,
  shouldApplyMapValue,
  shouldSkipPostVisibilityLayerWork,
  shouldSkipReferenceRegistration,
  stableHash,
} from "./temporal-map-performance.ts";

test("enabled layer keys are deduplicated while preserving order", () => {
  assert.deepEqual(dedupeStable(["temporalCumulativeBuffer15m", "temporalCumulativeBuffer15m", "buffer10m"]), [
    "temporalCumulativeBuffer15m",
    "buffer10m",
  ]);
});

test("reference signature hash is stable for identical metadata", () => {
  const first = stableHash({
    releaseIdentifier: "WB_2026_R04",
    tileSize: 256,
    bounds: [-8, 33, -7, 34],
  });
  const second = stableHash({
    bounds: [-8, 33, -7, 34],
    tileSize: 256,
    releaseIdentifier: "WB_2026_R04",
  });

  assert.equal(second, first);
});

test("layer paint setter can skip no-op updates", () => {
  assert.equal(mapStyleValueEquals(["literal", ["a", "b"]], ["literal", ["a", "b"]]), true);
  assert.equal(shouldApplyMapValue("#B91C1C", "#B91C1C"), false);
  assert.equal(shouldApplyMapValue("#B91C1C", "#1D4ED8"), true);
});

test("paint guard never treats different release colors as the same buffer paint", () => {
  assert.equal(shouldApplyMapValue("#E31A1C", "#00B050"), true);
  assert.equal(shouldApplyMapValue("#E31A1C", "#FFD700"), true);
  assert.equal(shouldApplyMapValue("#E31A1C", "#0066FF"), true);
  assert.equal(shouldApplyMapValue("#E31A1C", "#E31A1C"), false);
});

test("layer layout and filter guards detect equivalent arrays", () => {
  const filter = ["in", ["geometry-type"], ["literal", ["Polygon", "MultiPolygon"]]];

  assert.equal(shouldApplyMapValue(filter, ["in", ["geometry-type"], ["literal", ["Polygon", "MultiPolygon"]]]), false);
  assert.equal(shouldApplyMapValue("visible", "none"), true);
});

test("reference registration can be skipped when signature and map objects are unchanged", () => {
  assert.equal(
    shouldSkipReferenceRegistration({
      previousSignature: "abc123",
      nextSignature: "abc123",
      sourceExists: true,
      layerExists: true,
    }),
    true,
  );
  assert.equal(
    shouldSkipReferenceRegistration({
      previousSignature: "abc123",
      nextSignature: "def456",
      sourceExists: true,
      layerExists: true,
    }),
    false,
  );
});

test("disabled hidden layers skip post-visibility style and order work", () => {
  assert.equal(shouldSkipPostVisibilityLayerWork(false), true);
  assert.equal(shouldSkipPostVisibilityLayerWork(true), false);
});
