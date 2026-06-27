import assert from "node:assert/strict";
import test from "node:test";

import { filterProgressWarnings } from "./progress-warnings.ts";

test("progress warnings hide capture-date intersection messages only", () => {
  const visible = filterProgressWarnings([
    "T1 release WB_2025_R03 intersects 3 capture-date regions within the AOI.",
    "T2 release WB_2026_R05 intersects 4 capture-date regions within the AOI.",
    "Export failed because the results file is missing.",
    "BANDON applied an MPS slide-window compatibility patch to the configured crop/stride.",
  ]);

  assert.deepEqual(visible, ["Export failed because the results file is missing."]);
});
