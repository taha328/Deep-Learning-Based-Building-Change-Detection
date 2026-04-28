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
  error?: ErrorDetail;
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
    const normalizedDetail =
      detail && typeof detail === "object"
        ? {
            code: detail.code ?? detail.error?.code,
            message: detail.message ?? detail.error?.message,
            details: detail.details ?? detail.error?.details ?? detail.error ?? detail,
          }
        : null;
    const message =
      normalizedDetail
        ? normalizedDetail.message ?? response.statusText
        : typeof detail === "string"
          ? detail
          : response.statusText;

    const error = new ApiClientError(message);
    error.status = response.status;
    if (normalizedDetail) {
      error.code = normalizedDetail.code;
      error.details = normalizedDetail.details;
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
