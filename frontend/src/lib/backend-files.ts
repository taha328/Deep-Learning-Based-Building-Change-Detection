import { getFastApiBaseUrl } from "@/lib/env";

export function buildBackendFileUrl(_backendUrl: string, filePath: string): string {
  const params = new URLSearchParams({ path: filePath });
  return new URL(`/api/files?${params.toString()}`, getFastApiBaseUrl()).toString();
}
