export const DEFAULT_CHANGE_THRESHOLD = 0.3;
export const MIN_CHANGE_THRESHOLD = 0.01;
export const MAX_CHANGE_THRESHOLD = 0.99;

export function normalizeChangeThreshold(value: number): number {
  if (!Number.isFinite(value)) {
    return DEFAULT_CHANGE_THRESHOLD;
  }
  return Math.round(Math.min(MAX_CHANGE_THRESHOLD, Math.max(MIN_CHANGE_THRESHOLD, value)) * 100) / 100;
}

export function parseChangeThresholdInput(value: string): number | null {
  if (!value.trim()) {
    return null;
  }
  const parsed = Number(value.trim().replace(",", "."));
  if (!Number.isFinite(parsed) || parsed < MIN_CHANGE_THRESHOLD || parsed > MAX_CHANGE_THRESHOLD) {
    return null;
  }
  return Math.round(parsed * 100) / 100;
}

export function buildTemporalRunRequest(changeThreshold: number): { change_threshold: number } {
  return { change_threshold: normalizeChangeThreshold(changeThreshold) };
}
