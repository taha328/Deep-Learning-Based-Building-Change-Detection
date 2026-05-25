import assert from "node:assert/strict";
import { test } from "node:test";

import { normalizeTileTemplatePlaceholders } from "./tile-template.ts";

test("tile templates preserve raw MapLibre placeholders", () => {
  const template =
    "http://127.0.0.1:8000/api/temporal-projects/project/milestones/WB_2026_R04/reference/tiles/%7Bz%7D/%7Bx%7D/%7By%7D.png?v=abc";

  const normalized = normalizeTileTemplatePlaceholders(template);

  assert.match(normalized, /\/tiles\/\{z\}\/\{x\}\/\{y\}\.png/);
  assert.doesNotMatch(normalized, /%7Bz%7D|%7Bx%7D|%7By%7D/i);
});
