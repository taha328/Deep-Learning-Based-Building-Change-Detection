import assert from "node:assert/strict";
import test from "node:test";

import { downloadFileFromRequest, filenameFromContentDisposition } from "./download.ts";

type AnchorStub = {
  href: string;
  download: string;
  click: () => void;
  remove: () => void;
};

function installDownloadDom() {
  const clickedDownloads: string[] = [];
  const originalDocument = globalThis.document;
  const originalWindow = globalThis.window;
  const originalCreateObjectURL = URL.createObjectURL;
  const originalRevokeObjectURL = URL.revokeObjectURL;

  (globalThis as typeof globalThis & { document: unknown }).document = {
    body: {
      appendChild: () => undefined,
    },
    createElement: () => {
      const anchor: AnchorStub = {
        href: "",
        download: "",
        click: () => {
          clickedDownloads.push(anchor.download);
        },
        remove: () => undefined,
      };
      return anchor;
    },
  };
  (globalThis as typeof globalThis & { window: unknown }).window = {
    setTimeout: () => 1,
  };
  URL.createObjectURL = () => "blob:test-download";
  URL.revokeObjectURL = () => undefined;

  return {
    clickedDownloads,
    restore: () => {
      (globalThis as typeof globalThis & { document: unknown }).document = originalDocument;
      (globalThis as typeof globalThis & { window: unknown }).window = originalWindow;
      URL.createObjectURL = originalCreateObjectURL;
      URL.revokeObjectURL = originalRevokeObjectURL;
    },
  };
}

test("filenameFromContentDisposition handles encoded and quoted filenames", () => {
  assert.equal(
    filenameFromContentDisposition("attachment; filename*=UTF-8''resultats_temporal.zip"),
    "resultats_temporal.zip",
  );
  assert.equal(filenameFromContentDisposition('attachment; filename="resultats.geojson"'), "resultats.geojson");
  assert.equal(filenameFromContentDisposition(null), null);
});

test("download request reads successful binary responses as blobs, not JSON", async () => {
  const dom = installDownloadDom();
  const originalFetch = globalThis.fetch;
  let blobCalled = false;
  let jsonCalled = false;
  const response = new Response(new Blob(["zip-bytes"]), {
    status: 200,
    headers: {
      "Content-Disposition": "attachment; filename*=UTF-8''resultats_temporal.zip",
      "Content-Type": "application/zip",
    },
  });
  const originalBlob = response.blob.bind(response);
  response.blob = async () => {
    blobCalled = true;
    return originalBlob();
  };
  response.json = async () => {
    jsonCalled = true;
    return {};
  };
  globalThis.fetch = async () => response;

  try {
    await downloadFileFromRequest("/api/export", "fallback.zip", { format: "shapefile" });
    assert.equal(blobCalled, true);
    assert.equal(jsonCalled, false);
    assert.deepEqual(dom.clickedDownloads, ["resultats_temporal.zip"]);
  } finally {
    globalThis.fetch = originalFetch;
    dom.restore();
  }
});

test("download request preserves backend JSON errors for non-2xx responses", async () => {
  const originalFetch = globalThis.fetch;
  let jsonCalled = false;
  const response = new Response(JSON.stringify({ detail: { message: "Zone export vide." } }), {
    status: 400,
    headers: { "Content-Type": "application/json" },
  });
  const originalJson = response.json.bind(response);
  response.json = async () => {
    jsonCalled = true;
    return originalJson();
  };
  globalThis.fetch = async () => response;

  try {
    await assert.rejects(
      () => downloadFileFromRequest("/api/export", "fallback.zip", { format: "shapefile" }),
      /Zone export vide/,
    );
    assert.equal(jsonCalled, true);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("download request distinguishes network failure from backend validation failure", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    throw new TypeError("Failed to fetch");
  };

  try {
    await assert.rejects(
      () => downloadFileFromRequest("/api/export", "fallback.zip", { format: "shapefile" }),
      /connexion au backend, proxy ou autorisations CORS/,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});
