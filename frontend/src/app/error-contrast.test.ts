import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

function sourceFiles(dir: string): string[] {
  const entries = readdirSync(dir);
  return entries.flatMap((entry) => {
    const path = join(dir, entry);
    const stat = statSync(path);
    if (stat.isDirectory()) {
      return sourceFiles(path);
    }
    return /\.(ts|tsx|css)$/.test(path) && !path.endsWith(".test.ts") ? [path] : [];
  });
}

test("light error banners do not use destructive foreground text", () => {
  const root = new URL("../", import.meta.url).pathname;
  const offenders = sourceFiles(root).filter((path) => {
    const source = readFileSync(path, "utf8");
    return source.includes("bg-destructive/10") && source.includes("text-destructive-foreground");
  });

  assert.deepEqual(offenders, []);
});
