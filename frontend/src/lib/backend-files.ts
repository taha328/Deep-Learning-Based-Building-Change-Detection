import { getFastApiBaseUrl } from "@/lib/env";

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
    return value;
  }
  return new URL(value, backendUrl).toString();
}
