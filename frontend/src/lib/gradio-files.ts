export function buildGradioFileUrl(baseUrl: string, path: string): string {
  const normalizedBase = baseUrl.endsWith("/") ? baseUrl : `${baseUrl}/`;
  return new URL(`gradio_api/file=${encodeURIComponent(path)}`, normalizedBase).toString();
}
