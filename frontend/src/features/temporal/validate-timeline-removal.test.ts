import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

function source(relativePath: string): string {
  return readFileSync(new URL(relativePath, import.meta.url), "utf8");
}

test("run detection exposes one run action without manual validation", () => {
  const panel = source("./TemporalMosaicPanel.tsx");

  assert.match(panel, /temporal\.run_detection_button/);
  assert.match(panel, /runProjectMutation\.mutateAsync/);
  assert.doesNotMatch(panel, /handleValidate|validateProjectMutation|temporal\.validate_timeline/);
});

test("run timeline keeps backend validation failures visible through run error state", () => {
  const panel = source("./TemporalMosaicPanel.tsx");

  assert.match(panel, /setRunProgress\(createErrorRunProgress/);
  assert.match(panel, /runProjectMutation\.error/);
});

test("run detection copy no longer asks for manual validation", () => {
  const translations = source("../../lib/translations.ts");

  assert.doesNotMatch(translations, /Validate timeline|validate or rerun|Valider la chronologie|validez ou relancez/);
});
