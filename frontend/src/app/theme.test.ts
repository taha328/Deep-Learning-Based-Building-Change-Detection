import assert from "node:assert/strict";
import test from "node:test";

import { resolveInitialTheme } from "./theme.ts";

test("theme defaults to light without persisted preference", () => {
  assert.equal(resolveInitialTheme(null), "light");
  assert.equal(resolveInitialTheme(""), "light");
  assert.equal(resolveInitialTheme("system"), "light");
});

test("theme respects explicit persisted preference", () => {
  assert.equal(resolveInitialTheme("light"), "light");
  assert.equal(resolveInitialTheme("dark"), "dark");
});
