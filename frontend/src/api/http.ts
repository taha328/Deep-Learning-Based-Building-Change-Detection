import { getFastApiBaseUrl } from "@/lib/env";

export class ApiClientError extends Error {
  status: number;
  code?: string;
  details?: unknown;

  constructor(message: string) {
    super(message);
    this.name = "ApiClientError";
    this.status = 0;
  }
}

type ErrorDetail = {
  code?: string;
  message?: string;
  details?: unknown;
};

export async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(new URL(path, getFastApiBaseUrl()).toString(), {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options?.headers ?? {}),
    },
  });

  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    const detail = (payload?.detail ?? payload?.error ?? payload) as ErrorDetail | string | null;
    const message =
      detail && typeof detail === "object"
        ? detail.message ?? response.statusText
        : typeof detail === "string"
          ? detail
          : response.statusText;

    const error = new ApiClientError(message);
    error.status = response.status;
    if (detail && typeof detail === "object") {
      error.code = detail.code;
      error.details = detail.details ?? detail;
    } else {
      error.details = detail;
    }
    throw error;
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return response.json() as Promise<T>;
}
