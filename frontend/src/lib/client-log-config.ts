type EnvLike = Record<string, string | boolean | undefined>;

export function isDevClientLogEnabled(env: EnvLike): boolean {
  return env.VITE_DEV_CLIENT_LOG_ENABLED === "true" || env.VITE_ENABLE_CLIENT_LOG_RELAY === "true";
}
