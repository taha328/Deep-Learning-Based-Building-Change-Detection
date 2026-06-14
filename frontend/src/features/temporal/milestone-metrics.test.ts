import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(new URL("./MilestoneMetricCards.tsx", import.meta.url), "utf8");

test("comparison uses per-milestone addition metrics without subtracting the previous milestone", () => {
  assert.match(source, /const additionsCount = milestone\.metrics\.additions_feature_count;/);
  assert.match(source, /const blockCount = milestone\.metrics\.added_block_count;/);
  assert.doesNotMatch(source, /additions_feature_count - previousMilestone\.metrics\.additions_feature_count/);
  assert.doesNotMatch(source, /added_block_count - previousMilestone\.metrics\.added_block_count/);
});

test("area formatting distinguishes true zero from unavailable values", () => {
  assert.match(source, /areaM2 === undefined \|\| areaM2 === null \|\| !Number\.isFinite\(areaM2\)/);
  assert.match(source, /if \(areaM2 <= 0\) return "0 m²";/);
});

test("comparison growth percentage is unavailable when the previous footprint is zero", () => {
  assert.match(source, /previousMilestone\.metrics\.total_area_m2 > 0[\s\S]*: null;/);
  assert.match(source, /formatOptionalPercent\(footprintGrowthPercent, 1\)/);
});
