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

function detailToMessage(detail: unknown): string | null {
  if (typeof detail === "string" && detail.trim().length > 0) {
    return detail;
  }
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => {
        if (typeof item === "string") return item;
        if (item && typeof item === "object" && "msg" in item && typeof item.msg === "string") return item.msg;
        return null;
      })
      .filter((item): item is string => Boolean(item));
    return messages.length ? messages.join(" ") : null;
  }
  if (detail && typeof detail === "object") {
    if ("message" in detail && typeof detail.message === "string") {
      return detail.message;
    }
    if ("detail" in detail) {
      return detailToMessage(detail.detail);
    }
  }
  return null;
}

async function errorMessageFromResponse(response: Response): Promise<string> {
  const contentType = response.headers.get("Content-Type") ?? "";
  if (contentType.includes("application/json")) {
    const payload = await response.json().catch(() => null);
    const message = detailToMessage(payload);
    if (message) {
      return message;
    }
  } else {
    const text = await response.text().catch(() => "");
    if (text.trim()) {
      return text.trim();
    }
  }
  return `Download failed with HTTP ${response.status}.`;
}

function normalizeFetchError(error: unknown): Error {
  if (error instanceof TypeError) {
    return new Error("Téléchargement impossible : connexion au backend, proxy ou autorisations CORS indisponibles.");
  }
  if (error instanceof Error) {
    return error;
  }
  return new Error("Téléchargement impossible.");
}

async function fetchDownloadResponse(url: string, init?: RequestInit): Promise<Response> {
  try {
    return await fetch(url, init);
  } catch (error) {
    throw normalizeFetchError(error);
  }
}

async function downloadFromResponse(response: Response, fallbackFilename: string): Promise<void> {
  if (!response.ok) {
    throw new Error(await errorMessageFromResponse(response));
  }

  const blob = await response.blob();
  if (blob.size <= 0) {
    throw new Error("Le fichier exporté est vide.");
  }

  const filename = safeDownloadFilename(
    filenameFromContentDisposition(response.headers.get("Content-Disposition")),
    fallbackFilename,
  );
  const objectUrl = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = objectUrl;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(objectUrl), 10_000);
}

export async function downloadFileFromUrl(url: string, filename: string): Promise<void> {
  const response = await fetchDownloadResponse(url);
  await downloadFromResponse(response, filename);
}

export async function downloadFileFromRequest(url: string, filename: string, body: unknown): Promise<void> {
  const response = await fetchDownloadResponse(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/octet-stream, application/zip, application/json;q=0.8, */*;q=0.5",
    },
    body: JSON.stringify(body),
  });
  await downloadFromResponse(response, filename);
}
