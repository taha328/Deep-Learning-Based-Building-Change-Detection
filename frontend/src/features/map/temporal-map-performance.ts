export function dedupeStable<T extends string>(values: readonly T[]): T[] {
  return Array.from(new Set(values));
}

export function stableJsonStringify(value: unknown): string {
  return JSON.stringify(normalizeStableValue(value));
}

export function stableHash(value: unknown): string {
  const input = typeof value === "string" ? value : stableJsonStringify(value);
  let hash = 0x811c9dc5;
  for (let index = 0; index < input.length; index += 1) {
    hash ^= input.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return (hash >>> 0).toString(16).padStart(8, "0");
}

export function mapStyleValueEquals(left: unknown, right: unknown): boolean {
  if (Object.is(left, right)) {
    return true;
  }
  if (left == null || right == null) {
    return left === right;
  }
  if (typeof left !== "object" || typeof right !== "object") {
    return false;
  }
  return stableJsonStringify(left) === stableJsonStringify(right);
}

export function shouldApplyMapValue(currentValue: unknown, nextValue: unknown): boolean {
  return !mapStyleValueEquals(currentValue, nextValue);
}

export function shouldSkipReferenceRegistration(params: {
  previousSignature: string | null;
  nextSignature: string | null;
  sourceExists: boolean;
  layerExists: boolean;
}): boolean {
  return Boolean(
    params.nextSignature &&
      params.previousSignature === params.nextSignature &&
      params.sourceExists &&
      params.layerExists,
  );
}

export function shouldSkipPostVisibilityLayerWork(visible: boolean): boolean {
  return !visible;
}

function normalizeStableValue(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map((item) => normalizeStableValue(item));
  }
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return Object.keys(record)
      .sort()
      .reduce<Record<string, unknown>>((normalized, key) => {
        const item = record[key];
        if (item !== undefined) {
          normalized[key] = normalizeStableValue(item);
        }
        return normalized;
      }, {});
  }
  return value;
}
