import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  buildTemporalRunRequest,
  DEFAULT_CHANGE_THRESHOLD,
  normalizeChangeThreshold,
  parseChangeThresholdInput,
} from "./run-detection-threshold.ts";

test("change threshold defaults to 0.5", () => {
  assert.equal(DEFAULT_CHANGE_THRESHOLD, 0.5);
});

test("invalid threshold values are clamped before the request", () => {
  assert.equal(normalizeChangeThreshold(0), 0.01);
  assert.equal(normalizeChangeThreshold(1), 0.99);
  assert.equal(normalizeChangeThreshold(Number.NaN), 0.5);
});

test("temporal run request includes normalized change_threshold without semantic threshold", () => {
  assert.deepEqual(buildTemporalRunRequest(0.604), { change_threshold: 0.6 });
});

test("decimal threshold input rejects empty and out-of-range values", () => {
  assert.equal(parseChangeThresholdInput(""), null);
  assert.equal(parseChangeThresholdInput("0"), null);
  assert.equal(parseChangeThresholdInput("1"), null);
  assert.equal(parseChangeThresholdInput("0.35"), 0.35);
  assert.equal(parseChangeThresholdInput("0,3"), 0.3);
});

test("detection run card renders decimal threshold input and a text-only button", () => {
  const panel = readFileSync(new URL("./TemporalMosaicPanel.tsx", import.meta.url), "utf8");
  const card = panel.slice(
    panel.indexOf("temporal.run_detection_title"),
    panel.indexOf('{activePanel === "aoi"'),
  );
  assert.match(panel, /temporal\.run_detection_title/);
  assert.match(panel, /temporal\.run_detection_button/);
  assert.match(card, /type="text"/);
  assert.match(card, /inputMode="decimal"/);
  assert.doesNotMatch(card, /type="range"/);
  assert.doesNotMatch(card, /Sparkles|Loader2/);
  assert.doesNotMatch(panel, /temporal\.run_timeline/);
});

test("temporal run sends change threshold through async and synchronous request bodies", () => {
  const api = readFileSync(new URL("../../api/fastapi.ts", import.meta.url), "utf8");
  assert.match(api, /startTemporalProjectJob\(projectId, request\)/);
  assert.match(api, /body: JSON\.stringify\(request\)/);
});

test("accepted temporal run navigates to Progress while failed start stays on the card", () => {
  const api = readFileSync(new URL("../../api/fastapi.ts", import.meta.url), "utf8");
  const panel = readFileSync(new URL("./TemporalMosaicPanel.tsx", import.meta.url), "utf8");
  const startGuard = api.indexOf('if (!startResponse)');
  const accepted = api.indexOf("onAccepted?.();", startGuard);
  const poll = api.indexOf("pollJobUntilComplete", accepted);

  assert.ok(startGuard >= 0 && accepted > startGuard && poll > accepted);
  assert.match(panel, /\(\) => setActivePanel\("progress"\)/);
});
