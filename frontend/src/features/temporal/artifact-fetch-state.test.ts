import assert from "node:assert/strict";
import test from "node:test";

import {
  artifactFetchStatus,
  buildArtifactFetchKey,
  fetchArtifactOnce,
  resetArtifactFetchState,
} from "./artifact-fetch-state.ts";

const empty = { type: "FeatureCollection", features: [] };
const nonEmpty = { type: "FeatureCollection", features: [{ type: "Feature", properties: {}, geometry: null }] };

test.beforeEach(resetArtifactFetchState);

test("successful empty artifact is terminal loaded-empty and is not fetched again", async () => {
  const key = buildArtifactFetchKey("project", "release", "automated_building_blocks", "v1");
  let calls = 0;
  const loader = async () => {
    calls += 1;
    return empty;
  };

  assert.deepEqual(await fetchArtifactOnce(key, loader), empty);
  assert.equal(artifactFetchStatus(key), "loaded-empty");
  assert.equal(await fetchArtifactOnce(key, loader), null);
  assert.equal(await fetchArtifactOnce(key, loader), null);
  assert.equal(calls, 1);
});

test("concurrent artifact requests share one in-flight network request", async () => {
  const key = buildArtifactFetchKey("project", "release", "automated_building_blocks", "v1");
  let calls = 0;
  let resolveRequest!: (value: Record<string, unknown>) => void;
  const loader = () => {
    calls += 1;
    return new Promise<Record<string, unknown>>((resolve) => {
      resolveRequest = resolve;
    });
  };

  const first = fetchArtifactOnce(key, loader);
  const second = fetchArtifactOnce(key, loader);
  assert.equal(calls, 1);
  assert.equal(artifactFetchStatus(key), "in-flight");
  resolveRequest(empty);
  assert.deepEqual(await first, empty);
  assert.equal(await second, null);
});

test("non-empty artifacts load normally and new project, release, or version keys fetch independently", async () => {
  let calls = 0;
  const loader = async () => {
    calls += 1;
    return nonEmpty;
  };
  const firstKey = buildArtifactFetchKey("project-a", "release-a", "automated_building_blocks", "v1");
  const projectKey = buildArtifactFetchKey("project-b", "release-a", "automated_building_blocks", "v1");
  const releaseKey = buildArtifactFetchKey("project-a", "release-b", "automated_building_blocks", "v1");
  const versionKey = buildArtifactFetchKey("project-a", "release-a", "automated_building_blocks", "v2");

  assert.deepEqual(await fetchArtifactOnce(firstKey, loader), nonEmpty);
  assert.equal(artifactFetchStatus(firstKey), "loaded");
  await fetchArtifactOnce(projectKey, loader);
  await fetchArtifactOnce(releaseKey, loader);
  await fetchArtifactOnce(versionKey, loader);
  assert.equal(calls, 4);
});

test("failed artifacts are not marked loaded-empty and may retry", async () => {
  const key = buildArtifactFetchKey("project", "release", "automated_building_blocks", "v1");
  let calls = 0;
  const loader = async () => {
    calls += 1;
    if (calls === 1) {
      throw new Error("network failure");
    }
    return empty;
  };

  await assert.rejects(fetchArtifactOnce(key, loader), /network failure/);
  assert.equal(artifactFetchStatus(key), "error");
  assert.deepEqual(await fetchArtifactOnce(key, loader), empty);
  assert.equal(artifactFetchStatus(key), "loaded-empty");
});
