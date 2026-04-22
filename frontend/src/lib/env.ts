declare global {
  interface Window {
    huggingface?: {
      variables?: Record<string, string | undefined>;
    };
  }
}

export type FrontendRuntimeMode = "local" | "remote";
export type ModelBackendName = "sam3" | "bandon_mps";
export type Sam3BackendMode = "public_zerogpu" | "local" | "huggingface_gpu";

export interface FrontendRuntimeConfig {
  mode: FrontendRuntimeMode;
  backendUrl: string;
  modeLabel: string;
  modeDescription: string;
  defaultModelBackend: ModelBackendName;
  defaultSam3BackendMode: Sam3BackendMode;
  showBackendSelector: boolean;
  supportsRequestBackendSelection: boolean;
}

const DEFAULT_REMOTE_BACKEND_URL = "https://taha321-building-change-backend.hf.space";
const DEFAULT_LOCAL_BACKEND_URL = "http://127.0.0.1:7860";
const DEFAULT_MAPBOX_API_KEY = "pk.eyJ1IjoidGFoYWVsIiwiYSI6ImNtbnl6dHdqcjA3Z3EycXNmZHQyM3FkZWQifQ.IDf_zeGoMaPHcrsLOD5q7A";
const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "::1"]);

function readVariable(key: string): string | undefined {
  const fromSpace = window.huggingface?.variables?.[key];
  if (fromSpace) {
    return fromSpace;
  }
  return import.meta.env[key] as string | undefined;
}

function readBooleanVariable(key: string): boolean | undefined {
  const value = readVariable(key);
  if (value === undefined) {
    return undefined;
  }
  const normalized = value.trim().toLowerCase();
  if (["1", "true", "yes", "on"].includes(normalized)) {
    return true;
  }
  if (["0", "false", "no", "off"].includes(normalized)) {
    return false;
  }
  return undefined;
}

function inferFrontendRuntimeMode(): FrontendRuntimeMode {
  const explicitMode = readVariable("FRONTEND_MODE") ?? readVariable("VITE_FRONTEND_MODE");
  if (explicitMode === "local" || explicitMode === "remote") {
    return explicitMode;
  }
  return LOCAL_HOSTS.has(window.location.hostname) ? "local" : "remote";
}

function inferModelBackend(rawValue: string | undefined, fallback: ModelBackendName): ModelBackendName {
  return rawValue === "sam3" || rawValue === "bandon_mps" ? rawValue : fallback;
}

function inferSam3BackendMode(rawValue: string | undefined, fallback: Sam3BackendMode): Sam3BackendMode {
  return rawValue === "public_zerogpu" || rawValue === "local" || rawValue === "huggingface_gpu"
    ? rawValue
    : fallback;
}

export function getFrontendRuntimeConfig(): FrontendRuntimeConfig {
  const mode = inferFrontendRuntimeMode();
  const localBackendUrl =
    readVariable("LOCAL_BACKEND_URL") ??
    readVariable("VITE_LOCAL_BACKEND_URL") ??
    DEFAULT_LOCAL_BACKEND_URL;
  const remoteBackendUrl =
    readVariable("BACKEND_SPACE_URL") ??
    readVariable("VITE_BACKEND_SPACE_URL") ??
    DEFAULT_REMOTE_BACKEND_URL;
  const explicitBackendUrl = readVariable("BACKEND_URL") ?? readVariable("VITE_BACKEND_URL");
  const backendUrl = explicitBackendUrl ?? (mode === "local" ? localBackendUrl : remoteBackendUrl);

  if (!backendUrl) {
    throw new Error("Missing backend URL configuration.");
  }

  const defaultModelBackend = inferModelBackend(
    readVariable("DEFAULT_MODEL_BACKEND") ?? readVariable("VITE_DEFAULT_MODEL_BACKEND"),
    mode === "local" ? "bandon_mps" : "sam3",
  );
  const defaultSam3BackendMode = inferSam3BackendMode(
    readVariable("DEFAULT_SAM3_BACKEND_MODE") ?? readVariable("VITE_DEFAULT_SAM3_BACKEND_MODE"),
    "public_zerogpu",
  );
  const showBackendSelector =
    readBooleanVariable("SHOW_BACKEND_SELECTOR") ??
    readBooleanVariable("VITE_SHOW_BACKEND_SELECTOR") ??
    mode === "local";
  const supportsRequestBackendSelection =
    readBooleanVariable("ENABLE_REQUEST_BACKEND_SELECTION") ??
    readBooleanVariable("VITE_ENABLE_REQUEST_BACKEND_SELECTION") ??
    mode === "local";

  if (mode === "local") {
    return {
      mode,
      backendUrl,
      modeLabel: "Local frontend · Local backend",
      modeDescription:
        "This frontend targets your local Gradio backend and defaults to BANDON MTGCDNet on Apple Silicon MPS.",
      defaultModelBackend,
      defaultSam3BackendMode,
      showBackendSelector,
      supportsRequestBackendSelection,
    };
  }

  return {
    mode,
    backendUrl,
    modeLabel: "Hosted frontend · Remote backend",
    modeDescription:
      "This deployment keeps the existing Cloudflare Pages and Hugging Face compatibility path available.",
    defaultModelBackend,
    defaultSam3BackendMode,
    showBackendSelector,
    supportsRequestBackendSelection,
  };
}

export function getBackendSpaceUrl(): string {
  return getFrontendRuntimeConfig().backendUrl;
}

export function getMapboxApiKey(): string {
  return readVariable("MAPBOX_API_KEY") ?? readVariable("VITE_MAPBOX_API_KEY") ?? DEFAULT_MAPBOX_API_KEY;
}
