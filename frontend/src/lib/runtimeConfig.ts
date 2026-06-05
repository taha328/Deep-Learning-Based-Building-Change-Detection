type RuntimeConfig = {
  [key: string]: string | boolean | undefined;
  VITE_FASTAPI_BACKEND_URL?: string;
  FASTAPI_BACKEND_URL?: string;
  FRONTEND_MODE?: string;
  BACKEND_URL?: string;
  DEFAULT_MODEL_BACKEND?: string;
  SHOW_BACKEND_SELECTOR?: string | boolean;
  ENABLE_REQUEST_BACKEND_SELECTION?: string | boolean;
  MAPBOX_API_KEY?: string;
};

function runtimeConfig(): RuntimeConfig {
  const value = (globalThis as unknown as { BUILDING_CHANGE_RUNTIME_CONFIG?: RuntimeConfig })
    .BUILDING_CHANGE_RUNTIME_CONFIG;
  return value && typeof value === "object" ? value : {};
}

export function readRuntimeConfigVariable(key: keyof RuntimeConfig): string | undefined {
  const value = runtimeConfig()[key];
  if (typeof value === "boolean") {
    return value ? "true" : "false";
  }
  return typeof value === "string" && value.length > 0 ? value : undefined;
}
