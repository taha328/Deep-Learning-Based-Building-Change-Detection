import { getFastApiBaseUrl } from "@/lib/env";
import { normalizeTileTemplatePlaceholders } from "@/lib/tile-template";

export function buildBackendFileUrl(_backendUrl: string, filePath: string): string {
  const params = new URLSearchParams({ path: filePath });
  return new URL(`/api/files?${params.toString()}`, getFastApiBaseUrl()).toString();
}

export function resolveBackendUrl(backendUrl: string, value: string | null | undefined): string | null {
  if (!value) {
    return null;
  }
  if (value.startsWith("data:")) {
    return value;
  }
  if (/^https?:\/\//i.test(value)) {
    return normalizeTileTemplatePlaceholders(value);
  }
  return normalizeTileTemplatePlaceholders(new URL(value, backendUrl).toString());
}
