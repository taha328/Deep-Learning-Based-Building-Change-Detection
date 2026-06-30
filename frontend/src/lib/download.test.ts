import assert from "node:assert/strict";
import test from "node:test";

import { downloadFileFromUrl, filenameFromContentDisposition, triggerBrowserDownload } from "./download.ts";

type AnchorStub = {
  href: string;
  download: string;
  click: () => void;
  remove: () => void;
};

function installDownloadDom() {
  const clickedDownloads: string[] = [];
  const clickedHrefs: string[] = [];
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
          clickedHrefs.push(anchor.href);
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
    clickedHrefs,
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

test("triggerBrowserDownload uses direct href without object URL or blob fetch", () => {
  const dom = installDownloadDom();
  const originalCreateObjectURL = URL.createObjectURL;
  let objectUrlCalled = false;
  URL.createObjectURL = () => {
    objectUrlCalled = true;
    return "blob:unexpected";
  };

  try {
    triggerBrowserDownload("/api/temporal-projects/demo/exports/jobs/job/download?token=signed", "resultats.zip");
    assert.equal(objectUrlCalled, false);
    assert.deepEqual(dom.clickedDownloads, ["resultats.zip"]);
    assert.deepEqual(dom.clickedHrefs, ["/api/temporal-projects/demo/exports/jobs/job/download?token=signed"]);
  } finally {
    URL.createObjectURL = originalCreateObjectURL;
    dom.restore();
  }
});

test("downloadFileFromUrl triggers direct browser download without fetch", async () => {
  const dom = installDownloadDom();
  const originalFetch = globalThis.fetch;
  let fetchCalled = false;
  globalThis.fetch = async () => {
    fetchCalled = true;
    return new Response();
  };

  try {
    await downloadFileFromUrl("/api/files?path=resultats.zip", "resultats.zip");
    assert.equal(fetchCalled, false);
    assert.deepEqual(dom.clickedDownloads, ["resultats.zip"]);
    assert.deepEqual(dom.clickedHrefs, ["/api/files?path=resultats.zip"]);
  } finally {
    globalThis.fetch = originalFetch;
    dom.restore();
  }
});
