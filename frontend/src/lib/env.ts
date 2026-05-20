export type FrontendRuntimeMode = "local" | "remote";
export type ModelBackendName = "bandon_mps";

export interface FrontendRuntimeConfig {
  mode: FrontendRuntimeMode;
  backendUrl: string;
  modeLabel: string;
  modeDescription: string;
  defaultModelBackend: ModelBackendName;
  showBackendSelector: boolean;
  supportsRequestBackendSelection: boolean;
}

const DEFAULT_REMOTE_BACKEND_URL = "http://127.0.0.1:8000";
const DEFAULT_LOCAL_BACKEND_URL = "http://127.0.0.1:8000";
const DEFAULT_FASTAPI_BACKEND_URL = "http://127.0.0.1:8000";
const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "::1"]);

function readVariable(key: string): string | undefined {
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
  return rawValue === "bandon_mps" ? rawValue : fallback;
}

export function getFrontendRuntimeConfig(): FrontendRuntimeConfig {
  const mode = inferFrontendRuntimeMode();
  const localBackendUrl =
    readVariable("VITE_FASTAPI_BACKEND_URL") ??
    readVariable("LOCAL_BACKEND_URL") ??
    readVariable("VITE_LOCAL_BACKEND_URL") ??
    DEFAULT_LOCAL_BACKEND_URL;
  const remoteBackendUrl =
    readVariable("VITE_FASTAPI_BACKEND_URL") ??
    readVariable("VITE_BACKEND_URL") ??
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
    "bandon_mps",
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
        "This frontend targets your local FastAPI backend and defaults to BANDON MTGCDNet on Apple Silicon MPS.",
      defaultModelBackend,
      showBackendSelector,
      supportsRequestBackendSelection,
    };
  }

  return {
    mode,
    backendUrl,
    modeLabel: "Hosted frontend · Remote backend",
    modeDescription:
      "This deployment targets a FastAPI backend using local change detection backends.",
    defaultModelBackend,
    showBackendSelector,
    supportsRequestBackendSelection,
  };
}

export function getFastApiBaseUrl(): string {
  const value = readVariable("VITE_FASTAPI_BACKEND_URL") ?? DEFAULT_FASTAPI_BACKEND_URL;
  return value.replace(/\/+$/, "");
}

export function getMapboxApiKey(): string {
  return readVariable("MAPBOX_API_KEY") ?? readVariable("VITE_MAPBOX_API_KEY") ?? "";
}
