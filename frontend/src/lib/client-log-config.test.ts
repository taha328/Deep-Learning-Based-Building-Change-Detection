import assert from "node:assert/strict";
import { test } from "node:test";

import { isDevClientLogEnabled } from "./client-log-config.ts";

test("dev client logs are disabled by default", () => {
  assert.equal(isDevClientLogEnabled({}), false);
});

test("dev client logs are emitted when explicitly enabled", () => {
  assert.equal(isDevClientLogEnabled({ VITE_DEV_CLIENT_LOG_ENABLED: "true" }), true);
});

test("legacy explicit relay flag still enables dev client logs", () => {
  assert.equal(isDevClientLogEnabled({ VITE_ENABLE_CLIENT_LOG_RELAY: "true" }), true);
});
