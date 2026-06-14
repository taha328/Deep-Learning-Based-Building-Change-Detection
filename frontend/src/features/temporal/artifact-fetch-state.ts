export type ArtifactFetchStatus = "in-flight" | "loaded" | "loaded-empty" | "error";

const artifactFetchStatuses = new Map<string, ArtifactFetchStatus>();
const artifactFetchPromises = new Map<string, Promise<Record<string, unknown>>>();

export function isFeatureCollection(value: unknown): value is Record<string, unknown> & { features: unknown[] } {
  return Boolean(
    value &&
      typeof value === "object" &&
      (value as Record<string, unknown>).type === "FeatureCollection" &&
      Array.isArray((value as Record<string, unknown>).features),
  );
}

export function buildArtifactFetchKey(
  projectId: string,
  releaseIdentifier: string,
  artifactKey: string,
  projectVersion: string,
): string {
  return `${projectId}::${releaseIdentifier}::${artifactKey}::${projectVersion}`;
}

export function artifactFetchStatus(cacheKey: string): ArtifactFetchStatus | undefined {
  return artifactFetchStatuses.get(cacheKey);
}

export function fetchArtifactOnce(
  cacheKey: string,
  loader: () => Promise<Record<string, unknown>>,
): Promise<Record<string, unknown> | null> {
  const status = artifactFetchStatuses.get(cacheKey);
  if (status === "loaded" || status === "loaded-empty") {
    return Promise.resolve(null);
  }
  const inFlight = artifactFetchPromises.get(cacheKey);
  if (status === "in-flight" && inFlight) {
    return Promise.resolve(null);
  }

  artifactFetchStatuses.set(cacheKey, "in-flight");
  const request = loader()
    .then((payload) => {
      artifactFetchStatuses.set(
        cacheKey,
        isFeatureCollection(payload) && payload.features.length === 0 ? "loaded-empty" : "loaded",
      );
      return payload;
    })
    .catch((error: unknown) => {
      artifactFetchStatuses.set(cacheKey, "error");
      throw error;
    })
    .finally(() => {
      artifactFetchPromises.delete(cacheKey);
    });
  artifactFetchPromises.set(cacheKey, request);
  return request;
}

export function resetArtifactFetchState(): void {
  artifactFetchStatuses.clear();
  artifactFetchPromises.clear();
}
