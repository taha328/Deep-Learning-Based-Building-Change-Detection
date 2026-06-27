import assert from "node:assert/strict";
import { test } from "node:test";

import {
  TEMPORAL_VERTICAL_TIMELINE_STAGES,
  buildTemporalProgressTimeline,
  buildTemporalPeriodLabel,
  createActiveRunProgress,
  createCompletedRunProgress,
  createErrorRunProgress,
  formatArchiveDateDmy,
  friendlyTemporalStageLabel,
  shouldShowExecutionProgressPanel,
  temporalGlobalProgressPercent,
  temporalPairProgressPercent,
  type RunProgressState,
} from "./run-progress.ts";

function progressPatch(patch: Partial<RunProgressState>): RunProgressState {
  return {
    ...createActiveRunProgress(),
    ...patch,
  };
}

function temporalProgressPatch(patch: Partial<RunProgressState>): RunProgressState {
  return progressPatch({
    phase: "running",
    percent: 92,
    stageLabel: "inference",
    detail: "Running tiled local BANDON change detection",
    rawEvent: "running",
    temporalPairDetails: {
      currentPairIndex: 2,
      totalPairCount: 2,
      pairFraction: 0.4,
      pairStage: "Running tiled local BANDON change detection",
      fromReleaseIdentifier: "WB_2024_R02",
      toReleaseIdentifier: "WB_2025_R03",
      fromReleaseDate: "2024-03-21",
      toReleaseDate: "2025-03-27",
    },
    ...patch,
  });
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

test("archive dates render as DD/MM/YYYY", () => {
  assert.equal(formatArchiveDateDmy("2025-03-27"), "27/03/2025");
  assert.equal(formatArchiveDateDmy("2026-05-28T00:00:00Z"), "28/05/2026");
  assert.equal(formatArchiveDateDmy(null), null);
});

test("temporal period label prefers archive dates over release identifiers", () => {
  const label = buildTemporalPeriodLabel({
    currentPairIndex: 2,
    totalPairCount: 4,
    pairFraction: 0.36,
    pairStage: "Running tiled local BANDON change detection",
    fromReleaseIdentifier: "WB_2025_R03",
    toReleaseIdentifier: "WB_2026_R05",
    fromReleaseDate: "2025-03-27",
    toReleaseDate: "2026-05-28",
  });

  assert.equal(label, "Période en cours : 27/03/2025 → 28/05/2026");
  assert.equal(label.includes("WB_"), false);
});

test("temporal period label has a clean missing-date fallback", () => {
  assert.equal(buildTemporalPeriodLabel(null), "Période en cours non disponible");
  assert.equal(
    buildTemporalPeriodLabel({
      currentPairIndex: 1,
      totalPairCount: 2,
      pairFraction: null,
      pairStage: null,
      fromReleaseIdentifier: null,
      toReleaseIdentifier: null,
      fromReleaseDate: null,
      toReleaseDate: null,
    }),
    "Période en cours non disponible",
  );
});

test("temporal pair and global progress use real pair fraction", () => {
  const details = {
    currentPairIndex: 2,
    totalPairCount: 4,
    pairFraction: 0.36,
    pairStage: "Running tiled local BANDON change detection",
    fromReleaseIdentifier: "WB_2025_R03",
    toReleaseIdentifier: "WB_2026_R05",
    fromReleaseDate: "2025-03-27",
    toReleaseDate: "2026-05-28",
  };

  assert.equal(Math.round(temporalPairProgressPercent(details) ?? -1), 36);
  assert.equal(Math.round(temporalGlobalProgressPercent(details) ?? -1), 34);
});

test("backend stages map to friendly temporal stage labels", () => {
  assert.equal(friendlyTemporalStageLabel("Checking tile availability"), "Préparation des images");
  assert.equal(friendlyTemporalStageLabel("Downloading Wayback imagery"), "Téléchargement des images");
  assert.equal(friendlyTemporalStageLabel("Running tiled local BANDON change detection"), "Analyse des changements");
  assert.equal(friendlyTemporalStageLabel("Vectorizing results"), "Génération des résultats");
  assert.equal(friendlyTemporalStageLabel("Persisting compact job metadata"), "Finalisation");
  assert.equal(friendlyTemporalStageLabel("unexpected backend stage"), "Traitement en cours");
});

test("temporal timeline keeps finalization active when pair analysis is complete but job is still running", () => {
  const timeline = buildTemporalProgressTimeline(
    temporalProgressPatch({
      percent: 100,
      stageLabel: "saving_artifacts",
      detail: "Saving temporal outputs and generated artifacts.",
      temporalPairDetails: {
        currentPairIndex: 2,
        totalPairCount: 2,
        pairFraction: 1,
        pairStage: "Saving temporal outputs and generated artifacts.",
        fromReleaseIdentifier: "WB_2024_R02",
        toReleaseIdentifier: "WB_2025_R03",
        fromReleaseDate: "2024-03-21",
        toReleaseDate: "2025-03-27",
      },
    }),
  );

  assert.equal(timeline.activeStageId, "publication");
  assert.equal(timeline.ready, false);
  assert.equal(timeline.finalizing, true);
  assert.equal(timeline.summaryDetail, "Analyse terminée — finalisation en cours.");
  assert.equal(timeline.readinessLabel, "Résultats pas encore prêts.");
  assert.equal(timeline.stages.find((stage) => stage.id === "done")?.state, "pending");
});

test("temporal timeline marks done only after the job has completed", () => {
  const running = buildTemporalProgressTimeline(
    temporalProgressPatch({
      percent: 100,
      stageLabel: "saving_artifacts",
      detail: "Saving temporal outputs and generated artifacts.",
      temporalPairDetails: {
        currentPairIndex: 1,
        totalPairCount: 1,
        pairFraction: 1,
        pairStage: "Saving temporal outputs and generated artifacts.",
        fromReleaseIdentifier: "WB_2024_R02",
        toReleaseIdentifier: "WB_2025_R03",
        fromReleaseDate: "2024-03-21",
        toReleaseDate: "2025-03-27",
      },
    }),
  );
  const completed = buildTemporalProgressTimeline(
    temporalProgressPatch({
      phase: "complete",
      percent: 100,
      stageLabel: "completed",
      detail: "Artifacts are ready.",
      rawEvent: "completed",
    }),
  );

  assert.equal(running.activeStageId, "publication");
  assert.equal(running.ready, false);
  assert.equal(completed.activeStageId, "done");
  assert.equal(completed.ready, true);
  assert.equal(completed.readinessLabel, "Résultats prêts.");
  assert.equal(completed.stages.every((stage) => stage.state === "complete"), true);
});

test("temporal timeline shows queued stage without fake progress", () => {
  const timeline = buildTemporalProgressTimeline(
    progressPatch({
      phase: "queued",
      percent: 0,
      stageLabel: "Queued",
      detail: "Waiting for a worker slot.",
      rawEvent: "queued",
      temporalPairDetails: null,
    }),
  );

  assert.equal(timeline.activeStageId, "queued");
  assert.equal(timeline.globalPercent, null);
  assert.equal(timeline.stages[0].state, "current");
  assert.equal(timeline.stages[1].state, "pending");
});

test("temporal timeline marks failed finalization with a useful error state", () => {
  const timeline = buildTemporalProgressTimeline(
    temporalProgressPatch({
      phase: "error",
      percent: 100,
      stageLabel: "saving_artifacts",
      detail: "Finalization failed after publication.",
      rawEvent: "failed",
      temporalPairDetails: {
        currentPairIndex: 1,
        totalPairCount: 1,
        pairFraction: 1,
        pairStage: "Saving temporal outputs and generated artifacts.",
        fromReleaseIdentifier: "WB_2024_R02",
        toReleaseIdentifier: "WB_2025_R03",
        fromReleaseDate: "2024-03-21",
        toReleaseDate: "2025-03-27",
      },
    }),
  );

  assert.equal(timeline.activeStageId, "publication");
  assert.equal(timeline.failed, true);
  assert.equal(timeline.readinessLabel, "Résultats non disponibles.");
  assert.equal(timeline.stages.find((stage) => stage.id === "publication")?.state, "failed");
  assert.equal(timeline.stages.find((stage) => stage.id === "done")?.state, "pending");
});

test("temporal vertical timeline exposes French stage labels", () => {
  assert.deepEqual(
    TEMPORAL_VERTICAL_TIMELINE_STAGES.map((stage) => stage.label),
    [
      "En attente",
      "Préparation du projet",
      "Vérification des tuiles",
      "Téléchargement des images",
      "Alignement",
      "Inférence",
      "Post-traitement",
      "Vectorisation",
      "Publication des couches",
      "Génération des exports",
      "Écriture des métadonnées",
      "Nettoyage",
      "Terminé",
    ],
  );
});
