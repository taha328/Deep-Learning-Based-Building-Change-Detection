import assert from "node:assert/strict";
import { test } from "node:test";

import { getMilestoneColorMap, TEMPORAL_MILESTONE_COLOR_PALETTE } from "./temporal-layer-colors.ts";

test("latest milestone maps to dark red", () => {
  const colors = getMilestoneColorMap(["2016", "2020", "2026"]);
  assert.equal(colors["2026"], "#B91C1C");
});

test("previous milestone maps to dark blue", () => {
  const colors = getMilestoneColorMap(["2016", "2020", "2026"]);
  assert.equal(colors["2020"], "#1D4ED8");
});

test("third newest maps to dark orange", () => {
  const colors = getMilestoneColorMap(["2016", "2020", "2026"]);
  assert.equal(colors["2016"], "#C2410C");
});

test("colors are stable regardless of input order", () => {
  const first = getMilestoneColorMap(["2016", "2020", "2026"]);
  const second = getMilestoneColorMap(["2026", "2016", "2020"]);
  assert.deepEqual(second, first);
});

test("older milestones do not use red", () => {
  const colors = getMilestoneColorMap(["2016", "2018", "2020", "2022", "2024", "2026"]);
  assert.equal(colors["2026"], "#B91C1C");
  for (const [releaseIdentifier, color] of Object.entries(colors)) {
    if (releaseIdentifier !== "2026") {
      assert.notEqual(color, "#B91C1C");
    }
  }
});

test("colors are not cycled", () => {
  const milestones = Array.from({ length: TEMPORAL_MILESTONE_COLOR_PALETTE.length + 4 }, (_, index) =>
    String(2000 + index),
  );
  const colors = Object.values(getMilestoneColorMap(milestones));
  assert.equal(new Set(colors).size, colors.length);
});

test("generated colors are deterministic and not duplicates", () => {
  const milestones = Array.from({ length: TEMPORAL_MILESTONE_COLOR_PALETTE.length + 6 }, (_, index) =>
    String(2000 + index),
  );
  const first = getMilestoneColorMap(milestones);
  const second = getMilestoneColorMap(milestones);
  assert.deepEqual(second, first);
  assert.equal(new Set(Object.values(first)).size, milestones.length);
});

test("same milestone list returns the same mapping across calls", () => {
  const milestones = [
    { releaseIdentifier: "WB_2016_R01", releaseDate: "2016-01-01" },
    { releaseIdentifier: "WB_2020_R01", releaseDate: "2020-01-01" },
    { releaseIdentifier: "WB_2026_R01", releaseDate: "2026-01-01" },
  ];
  assert.deepEqual(getMilestoneColorMap(milestones), getMilestoneColorMap(milestones));
});
