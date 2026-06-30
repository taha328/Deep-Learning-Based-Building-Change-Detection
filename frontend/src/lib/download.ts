function safeDownloadFilename(value: string | null | undefined, fallback: string): string {
  const candidate = (value ?? "").trim().replace(/[\\/:*?"<>|\r\n]+/g, "_");
  return candidate.length > 0 ? candidate : fallback;
}

export function filenameFromContentDisposition(header: string | null): string | null {
  if (!header) {
    return null;
  }
  const encodedMatch = header.match(/filename\*\s*=\s*UTF-8''([^;]+)/i);
  if (encodedMatch?.[1]) {
    try {
      return decodeURIComponent(encodedMatch[1].trim().replace(/^"|"$/g, ""));
    } catch {
      return encodedMatch[1].trim().replace(/^"|"$/g, "");
    }
  }
  const quotedMatch = header.match(/filename\s*=\s*"([^"]+)"/i);
  if (quotedMatch?.[1]) {
    return quotedMatch[1];
  }
  const plainMatch = header.match(/filename\s*=\s*([^;]+)/i);
  return plainMatch?.[1]?.trim().replace(/^"|"$/g, "") ?? null;
}

export function triggerBrowserDownload(url: string, filename: string): void {
  const link = document.createElement("a");
  link.href = url;
  link.download = safeDownloadFilename(filename, "export.zip");
  link.rel = "noopener";
  document.body.appendChild(link);
  link.click();
  link.remove();
}

export async function downloadFileFromUrl(url: string, filename: string): Promise<void> {
  triggerBrowserDownload(url, filename);
}
