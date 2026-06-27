import assert from "node:assert/strict";
import { test } from "node:test";

import {
  formatDateDmy,
  formatDiagnosticStageLabel,
  formatDurationLabel,
  formatGeometryTypeLabel,
  formatMilestoneActionLabel,
  formatMilestonePrimaryLabel,
  formatMilestoneSecondaryLabel,
  formatReferenceLayerKindLabel,
  formatStorageStrategyLabel,
} from "./display-labels.ts";

test("milestone labels prefer formatted dates over raw Wayback identifiers", () => {
  const milestone = {
    release_identifier: "WB_2025_R03",
    release_date: "2025-03-27",
  } as any;

  assert.equal(formatMilestonePrimaryLabel(milestone), "27/03/2025");
  assert.equal(formatMilestoneSecondaryLabel(milestone), "Archive R03");
  assert.equal(formatMilestoneActionLabel(milestone), "27/03/2025 (Archive R03)");
  assert.equal(formatMilestoneActionLabel(milestone).includes("WB_"), false);
});

test("milestone labels have a clean missing-date fallback", () => {
  const milestone = {
    release_identifier: "WB_2025_R03",
    release_date: null,
  } as any;

  assert.equal(formatMilestonePrimaryLabel(milestone), "Date non disponible");
  assert.equal(formatMilestoneActionLabel(milestone), "Date non disponible (Archive R03)");
});

test("reference layer enum labels render as French user-facing labels", () => {
  assert.equal(formatGeometryTypeLabel("polygon"), "Polygone");
  assert.equal(formatGeometryTypeLabel("mixed"), "Géométries mixtes");
  assert.equal(formatStorageStrategyLabel("geojson"), "Fichier GeoJSON");
  assert.equal(formatStorageStrategyLabel("pmtiles"), "Tuiles vectorielles optimisées");
  assert.equal(formatReferenceLayerKindLabel("polygon", "pmtiles"), "Polygone / Tuiles vectorielles optimisées");
});

test("diagnostics format advanced stages and durations without raw backend keys", () => {
  assert.equal(formatDateDmy("2026-04-30"), "30/04/2026");
  assert.equal(formatDiagnosticStageLabel("mosaic_download"), "Préparation des images");
  assert.equal(formatDiagnosticStageLabel("inference"), "Analyse des changements");
  assert.equal(formatDurationLabel(134.2), "2 min 14 s");
});
