import assert from "node:assert/strict";
import { test } from "node:test";

import {
  createActiveRunProgress,
  createCompletedRunProgress,
  createErrorRunProgress,
  shouldShowExecutionProgressPanel,
  type RunProgressState,
} from "./run-progress.ts";

function progressPatch(patch: Partial<RunProgressState>): RunProgressState {
  return {
    ...createActiveRunProgress(),
    ...patch,
  };
}

test("execution progress panel is visible while a run is running", () => {
  assert.equal(shouldShowExecutionProgressPanel(createActiveRunProgress()), true);
});

test("execution progress panel is visible while progress is below 100 percent", () => {
  assert.equal(shouldShowExecutionProgressPanel(progressPatch({ phase: "running", percent: 42 })), true);
});

test("execution progress panel is hidden for successful completed statuses", () => {
  for (const rawEvent of ["process_completed", "completed", "success", "done"]) {
    assert.equal(shouldShowExecutionProgressPanel(progressPatch({ phase: "complete", percent: 100, rawEvent })), false);
  }
  assert.equal(shouldShowExecutionProgressPanel(createCompletedRunProgress()), false);
});

test("execution progress panel is hidden when all stages are complete with no error", () => {
  assert.equal(
    shouldShowExecutionProgressPanel(
      progressPatch({
        phase: "complete",
        percent: 100,
        stageLabel: "Completed",
        detail: "Artifacts are ready.",
      }),
    ),
    false,
  );
});

test("execution progress panel remains visible for failed, error, and cancelled states", () => {
  assert.equal(shouldShowExecutionProgressPanel(createErrorRunProgress("Backend failed")), true);
  assert.equal(shouldShowExecutionProgressPanel(progressPatch({ phase: "error", percent: 100, rawEvent: "error" })), true);
  assert.equal(shouldShowExecutionProgressPanel(progressPatch({ phase: "complete", percent: 100, rawEvent: "cancelled" })), true);
});

test("execution progress panel remains visible when any stage failed", () => {
  assert.equal(
    shouldShowExecutionProgressPanel(
      progressPatch({
        phase: "complete",
        percent: 100,
        stageLabel: "Export failed",
        detail: "Artifacts are ready.",
      }),
    ),
    true,
  );
});

test("execution progress panel remains visible when an error message exists", () => {
  assert.equal(
    shouldShowExecutionProgressPanel(
      progressPatch({
        phase: "complete",
        percent: 100,
        stageLabel: "Completed",
        detail: "Export failed after vectorization.",
      }),
    ),
    true,
  );
});
