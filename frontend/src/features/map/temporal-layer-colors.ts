export type TemporalMilestoneColorInput =
  | string
  | {
      releaseIdentifier?: string | null;
      release_identifier?: string | null;
      id?: string | null;
      date?: string | null;
      releaseDate?: string | null;
      release_date?: string | null;
    };

export type TemporalStyledLayerKind =
  | "additions"
  | "buffer10m"
  | "buffer15m"
  | "buffer20m"
  | "cumulativeBuffer10m"
  | "cumulativeBuffer15m"
  | "cumulativeBuffer20m";

export type TemporalLayerPaint = {
  fillPaint: {
    "fill-color": string;
    "fill-opacity": number;
    "fill-outline-color": string;
  };
  linePaint: {
    "line-color": string;
    "line-opacity": number;
    "line-width": number;
  };
  fillOpacity: number;
  lineOpacity: number;
};

export type IncludedTemporalMilestone = {
  releaseIdentifier: string;
  label: string;
};

export type TemporalLayerLabels = {
  allPreviousAdditions: string;
  selectedAdditions: string;
  buffer10m: string;
  buffer15m: string;
  buffer20m: string;
  cumulativeBuffer10m: string;
  cumulativeBuffer15m: string;
  cumulativeBuffer20m: string;
};

export type TemporalLayerLabelText = {
  allNewBuildings: string;
  addedBuildingIn: string;
  buffer10m: string;
  buffer15m: string;
  buffer20m: string;
  rangeSeparator: string;
};

export type TemporalTimelineLabelText = {
  before: string;
};

export type TemporalTimelineLabel = {
  releaseIdentifier: string;
  label: string;
};

export type TemporalReleaseMode = "selected" | "cumulative";

export type TemporalLayerContract = {
  artifactKey:
    | "additions"
    | "building_change_buffer_10m"
    | "building_change_buffer_15m"
    | "building_change_buffer_20m"
    | "cumulative_building_change_buffer_10m"
    | "cumulative_building_change_buffer_15m"
    | "cumulative_building_change_buffer_20m";
  mode: TemporalReleaseMode;
};

export type TemporalLayerPlanningKey =
  | "allNewBuildings"
  | "selectedAdditions"
  | "buffer10m"
  | "buffer15m"
  | "buffer20m"
  | "temporalCumulativeBuffer10m"
  | "temporalCumulativeBuffer15m"
  | "temporalCumulativeBuffer20m"
  | "cumulativeUnion"
  | "manualOverride";

export const TEMPORAL_LAYER_CONTRACTS: Record<TemporalLayerPlanningKey, TemporalLayerContract> = {
  allNewBuildings: { artifactKey: "additions", mode: "cumulative" },
  selectedAdditions: { artifactKey: "additions", mode: "selected" },
  buffer10m: { artifactKey: "building_change_buffer_10m", mode: "selected" },
  buffer15m: { artifactKey: "building_change_buffer_15m", mode: "selected" },
  buffer20m: { artifactKey: "building_change_buffer_20m", mode: "selected" },
  temporalCumulativeBuffer10m: { artifactKey: "cumulative_building_change_buffer_10m", mode: "cumulative" },
  temporalCumulativeBuffer15m: { artifactKey: "cumulative_building_change_buffer_15m", mode: "cumulative" },
  temporalCumulativeBuffer20m: { artifactKey: "cumulative_building_change_buffer_20m", mode: "cumulative" },
  cumulativeUnion: { artifactKey: "additions", mode: "selected" },
  manualOverride: { artifactKey: "additions", mode: "selected" },
};

export const HIGH_CONTRAST_TEMPORAL_COLORS = [
  "#00B050",
  "#FFD700",
  "#0066FF",
  "#E31A1C",
  "#00C8C8",
  "#FF1493",
  "#7FFF00",
  "#8B4513",
] as const;

export const TEMPORAL_MILESTONE_COLOR_PALETTE = HIGH_CONTRAST_TEMPORAL_COLORS;

const BASELINE_MILESTONE_COLOR = "#64748B";
const LATEST_MILESTONE_COLOR = HIGH_CONTRAST_TEMPORAL_COLORS[3];
const TRANSPARENT_COLOR = "rgba(0, 0, 0, 0)";
const TEMPORAL_BUFFER_FILL_OPACITY = 0.5;
const MIN_GENERATED_RGB_DISTANCE = 90;
const GOLDEN_ANGLE = 137.508;

function normalizeIdentifier(milestone: TemporalMilestoneColorInput): string {
  if (typeof milestone === "string") {
    return milestone;
  }
  return milestone.releaseIdentifier ?? milestone.release_identifier ?? milestone.id ?? "";
}

function parseMilestoneDateInput(value: string | null | undefined): { date: Date | null; hasFullDate: boolean } {
  if (!value) {
    return { date: null, hasFullDate: false };
  }
  const raw = String(value).trim();
  const yearOnlyMatch = raw.match(/^(\d{4})$/);
  if (yearOnlyMatch) {
    return { date: new Date(Date.UTC(Number(yearOnlyMatch[1]), 0, 1)), hasFullDate: false };
  }
  const isoMatch = raw.match(/^(\d{4})-(\d{2})(?:-(\d{2}))?/);
  if (isoMatch) {
    return {
      date: new Date(Date.UTC(Number(isoMatch[1]), Number(isoMatch[2]) - 1, Number(isoMatch[3] ?? "1"))),
      hasFullDate: true,
    };
  }
  const dayMonthYearMatch = raw.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})$/);
  if (dayMonthYearMatch) {
    return {
      date: new Date(
        Date.UTC(Number(dayMonthYearMatch[3]), Number(dayMonthYearMatch[2]) - 1, Number(dayMonthYearMatch[1])),
      ),
      hasFullDate: true,
    };
  }
  const parsed = Date.parse(raw);
  if (Number.isFinite(parsed)) {
    const date = new Date(parsed);
    return { date, hasFullDate: true };
  }
  return { date: null, hasFullDate: false };
}

function milestoneDateSource(milestone: TemporalMilestoneColorInput): string | null {
  return typeof milestone === "string" ? null : milestone.releaseDate ?? milestone.release_date ?? milestone.date ?? null;
}

function milestoneDateValue(milestone: TemporalMilestoneColorInput): number {
  const dateValue = milestoneDateSource(milestone);
  const parsedDate = parseMilestoneDateInput(dateValue);
  if (parsedDate.date) {
    return parsedDate.date.getTime();
  }
  const identifier = normalizeIdentifier(milestone);
  const yearMatch = identifier.match(/(?:19|20)\d{2}/);
  if (yearMatch) {
    return Date.UTC(Number(yearMatch[0]), 0, 1);
  }
  return Number.NEGATIVE_INFINITY;
}

function milestoneDisplayLabel(milestone: TemporalMilestoneColorInput): string {
  const dateValue = milestoneDateSource(milestone);
  const parsedDate = parseMilestoneDateInput(dateValue);
  if (parsedDate.date) {
    const year = parsedDate.date.getUTCFullYear();
    if (parsedDate.hasFullDate) {
      return `${year} Q${Math.floor(parsedDate.date.getUTCMonth() / 3) + 1}`;
    }
    return String(year);
  }
  const identifier = normalizeIdentifier(milestone);
  const yearMatch = identifier.match(/(?:19|20)\d{2}/);
  return yearMatch?.[0] ?? identifier;
}

function sortedUniqueMilestones(milestones: TemporalMilestoneColorInput[]): Array<[string, TemporalMilestoneColorInput]> {
  const uniqueMilestones = new Map<string, TemporalMilestoneColorInput>();
  for (const milestone of milestones) {
    const identifier = normalizeIdentifier(milestone);
    if (identifier && !uniqueMilestones.has(identifier)) {
      uniqueMilestones.set(identifier, milestone);
    }
  }
  return Array.from(uniqueMilestones.entries()).sort(([leftId, left], [rightId, right]) => {
    const dateDelta = milestoneDateValue(left) - milestoneDateValue(right);
    return dateDelta === 0 ? leftId.localeCompare(rightId) : dateDelta;
  });
}

function hexToRgb(color: string): [number, number, number] {
  const value = color.replace("#", "");
  return [
    Number.parseInt(value.slice(0, 2), 16),
    Number.parseInt(value.slice(2, 4), 16),
    Number.parseInt(value.slice(4, 6), 16),
  ];
}

function rgbDistance(left: string, right: string): number {
  const [lr, lg, lb] = hexToRgb(left);
  const [rr, rg, rb] = hexToRgb(right);
  return Math.sqrt((lr - rr) ** 2 + (lg - rg) ** 2 + (lb - rb) ** 2);
}

function hslToHex(hue: number, saturation: number, lightness: number): string {
  const h = (((hue % 360) + 360) % 360) / 360;
  const s = saturation / 100;
  const l = lightness / 100;
  const hueToRgb = (p: number, q: number, t: number) => {
    let adjusted = t;
    if (adjusted < 0) adjusted += 1;
    if (adjusted > 1) adjusted -= 1;
    if (adjusted < 1 / 6) return p + (q - p) * 6 * adjusted;
    if (adjusted < 1 / 2) return q;
    if (adjusted < 2 / 3) return p + (q - p) * (2 / 3 - adjusted) * 6;
    return p;
  };
  const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
  const p = 2 * l - q;
  const rgb = [hueToRgb(p, q, h + 1 / 3), hueToRgb(p, q, h), hueToRgb(p, q, h - 1 / 3)];
  return `#${rgb.map((channel) => Math.round(channel * 255).toString(16).padStart(2, "0")).join("")}`.toUpperCase();
}

function isRedLikeHue(hue: number): boolean {
  const normalized = ((hue % 360) + 360) % 360;
  return normalized <= 25 || normalized >= 335;
}

function generateDarkMilestoneColor(index: number, previousColor: string, usedColors: Set<string>): string {
  for (let attempt = 0; attempt < 48; attempt += 1) {
    const hue = (211 + (index + attempt * 3) * GOLDEN_ANGLE) % 360;
    if (isRedLikeHue(hue)) {
      continue;
    }
    const saturation = 68 + ((index + attempt) % 4) * 6;
    const lightness = 26 + ((index + attempt * 2) % 7) * 2;
    const color = hslToHex(hue, saturation, Math.min(lightness, 38));
    if (!usedColors.has(color) && rgbDistance(color, previousColor) >= MIN_GENERATED_RGB_DISTANCE) {
      return color;
    }
  }

  for (let attempt = 0; attempt < 360; attempt += 17) {
    const hue = (89 + index * 53 + attempt) % 360;
    if (isRedLikeHue(hue)) {
      continue;
    }
    const color = hslToHex(hue, 72, 31);
    if (!usedColors.has(color)) {
      return color;
    }
  }

  throw new Error("Unable to generate a distinct temporal milestone color.");
}

export function getMilestoneColorMap(milestones: TemporalMilestoneColorInput[]): Record<string, string> {
  const sorted = sortedUniqueMilestones(milestones);
  const baselineReleaseIdentifier = sorted[0]?.[0] ?? null;
  const usedColors = new Set<string>(baselineReleaseIdentifier ? [BASELINE_MILESTONE_COLOR] : []);
  const colorByReleaseIdentifier: Record<string, string> = {};
  if (baselineReleaseIdentifier) {
    colorByReleaseIdentifier[baselineReleaseIdentifier] = BASELINE_MILESTONE_COLOR;
  }

  sorted.forEach(([identifier], chronologicalIndex) => {
    if (identifier === baselineReleaseIdentifier) {
      return;
    }
    const nonBaselineIndex = chronologicalIndex - (baselineReleaseIdentifier ? 1 : 0);
    const previousIdentifier = sorted[chronologicalIndex - 1]?.[0] ?? baselineReleaseIdentifier;
    const previousColor = previousIdentifier ? (colorByReleaseIdentifier[previousIdentifier] ?? LATEST_MILESTONE_COLOR) : LATEST_MILESTONE_COLOR;
    const color =
      nonBaselineIndex < HIGH_CONTRAST_TEMPORAL_COLORS.length
        ? HIGH_CONTRAST_TEMPORAL_COLORS[nonBaselineIndex]
        : generateDarkMilestoneColor(nonBaselineIndex, previousColor, usedColors);
    usedColors.add(color);
    colorByReleaseIdentifier[identifier] = color;
  });

  return colorByReleaseIdentifier;
}

export function getIncludedTemporalMilestones(
  milestones: TemporalMilestoneColorInput[],
  selectedReleaseIdentifier: string | null | undefined,
): IncludedTemporalMilestone[] {
  if (!selectedReleaseIdentifier) {
    return [];
  }
  const sorted = sortedUniqueMilestones(milestones);
  const selectedIndex = sorted.findIndex(([identifier]) => identifier === selectedReleaseIdentifier);
  if (selectedIndex < 0) {
    return [];
  }
  return sorted.slice(0, selectedIndex + 1).map(([releaseIdentifier, milestone]) => ({
    releaseIdentifier,
    label: milestoneDisplayLabel(milestone),
  }));
}

export function buildTimelineLabelsFromReleases(
  milestones: TemporalMilestoneColorInput[],
  text: TemporalTimelineLabelText,
): TemporalTimelineLabel[] {
  const sorted = sortedUniqueMilestones(milestones);
  const firstComparison = sorted[1]?.[1] ?? sorted[0]?.[1] ?? null;
  const firstComparisonLabel = firstComparison ? milestoneDisplayLabel(firstComparison) : "";
  return sorted.map(([releaseIdentifier, milestone], index) => ({
    releaseIdentifier,
    label: index === 0 ? `${text.before} ${firstComparisonLabel || milestoneDisplayLabel(milestone)}` : milestoneDisplayLabel(milestone),
  }));
}

export function getIncludedAdditionReleasesForCumulativeLayer(
  milestones: TemporalMilestoneColorInput[],
  selectedReleaseIdentifier: string | null | undefined,
  additionsAvailableReleaseIdentifiers: Iterable<string>,
): string[] {
  return getCumulativeReleaseSet(milestones, selectedReleaseIdentifier, additionsAvailableReleaseIdentifiers);
}

export function getSelectedReleaseSet(
  selectedReleaseIdentifier: string | null | undefined,
  availableReleaseIdentifiers?: Iterable<string>,
): string[] {
  if (!selectedReleaseIdentifier) {
    return [];
  }
  if (!availableReleaseIdentifiers) {
    return [selectedReleaseIdentifier];
  }
  return new Set(availableReleaseIdentifiers).has(selectedReleaseIdentifier) ? [selectedReleaseIdentifier] : [];
}

export function getCumulativeReleaseSet(
  milestones: TemporalMilestoneColorInput[],
  selectedReleaseIdentifier: string | null | undefined,
  availableReleaseIdentifiers: Iterable<string>,
): string[] {
  if (!selectedReleaseIdentifier) {
    return [];
  }
  const sorted = sortedUniqueMilestones(milestones);
  const baselineReleaseIdentifier = sorted[0]?.[0] ?? null;
  const selectedIndex = sorted.findIndex(([identifier]) => identifier === selectedReleaseIdentifier);
  if (selectedIndex < 0) {
    return [];
  }
  const availableReleaseIdentifierSet = new Set(availableReleaseIdentifiers);
  return sorted
    .slice(0, selectedIndex + 1)
    .map(([releaseIdentifier]) => releaseIdentifier)
    .filter(
      (releaseIdentifier) =>
        releaseIdentifier !== baselineReleaseIdentifier && availableReleaseIdentifierSet.has(releaseIdentifier),
    );
}

export function getTemporalLayerExpectedReleases(params: {
  layerKey: TemporalLayerPlanningKey;
  milestones: TemporalMilestoneColorInput[];
  selectedReleaseIdentifier: string | null | undefined;
  availableReleaseIdentifiers: Iterable<string>;
}): string[] {
  const mode = TEMPORAL_LAYER_CONTRACTS[params.layerKey].mode;
  if (mode === "selected") {
    return getSelectedReleaseSet(params.selectedReleaseIdentifier, params.availableReleaseIdentifiers);
  }
  return getCumulativeReleaseSet(
    params.milestones,
    params.selectedReleaseIdentifier,
    params.availableReleaseIdentifiers,
  );
}

export function temporalAdditionVisibilityReason(params: {
  releaseIdentifier: string;
  selectedReleaseIdentifier: string | null | undefined;
  includedAdditionReleaseIdentifiers: Iterable<string>;
  allNewBuildingsEnabled: boolean;
  selectedAdditionsEnabled: boolean;
}): "allNewBuildings" | "selectedMilestoneAdditions" | null {
  const includedReleaseIdentifiers = new Set(params.includedAdditionReleaseIdentifiers);
  if (params.allNewBuildingsEnabled && includedReleaseIdentifiers.has(params.releaseIdentifier)) {
    return "allNewBuildings";
  }
  if (params.selectedAdditionsEnabled && params.releaseIdentifier === params.selectedReleaseIdentifier) {
    return "selectedMilestoneAdditions";
  }
  return null;
}

export function getTemporalMilestoneLabel(
  milestones: TemporalMilestoneColorInput[],
  releaseIdentifier: string | null | undefined,
): string {
  const sorted = sortedUniqueMilestones(milestones);
  const match = sorted.find(([identifier]) => identifier === releaseIdentifier);
  if (match) {
    return milestoneDisplayLabel(match[1]);
  }
  return releaseIdentifier ? milestoneDisplayLabel(releaseIdentifier) : "";
}

export function getTemporalMilestoneRangeLabel(
  milestones: TemporalMilestoneColorInput[],
  selectedReleaseIdentifier: string | null | undefined,
  separator = "->",
): string {
  const sorted = sortedUniqueMilestones(milestones);
  if (!sorted.length) {
    return selectedReleaseIdentifier ? getTemporalMilestoneLabel(milestones, selectedReleaseIdentifier) : "";
  }
  const firstLabel = milestoneDisplayLabel(sorted[0][1]);
  const selected = sorted.find(([identifier]) => identifier === selectedReleaseIdentifier) ?? sorted[sorted.length - 1];
  const selectedLabel = milestoneDisplayLabel(selected[1]);
  return firstLabel === selectedLabel ? firstLabel : `${firstLabel} ${separator} ${selectedLabel}`;
}

export function buildTemporalLayerLabels(
  milestones: TemporalMilestoneColorInput[],
  selectedReleaseIdentifier: string | null | undefined,
  text: TemporalLayerLabelText = {
    allNewBuildings: "All new buildings",
    addedBuildingIn: "Added building in",
    buffer10m: "Buffer 10m",
    buffer15m: "Buffer 15m",
    buffer20m: "Buffer 20m",
    rangeSeparator: "->",
  },
): TemporalLayerLabels {
  const selectedLabel = getTemporalMilestoneLabel(milestones, selectedReleaseIdentifier);
  const rangeLabel = getTemporalMilestoneRangeLabel(milestones, selectedReleaseIdentifier, text.rangeSeparator);
  return {
    allPreviousAdditions: rangeLabel ? `${text.allNewBuildings} ${rangeLabel}` : text.allNewBuildings,
    selectedAdditions: selectedLabel ? `${text.addedBuildingIn} ${selectedLabel}` : text.addedBuildingIn,
    buffer10m: selectedLabel ? `${text.buffer10m} ${selectedLabel}` : text.buffer10m,
    buffer15m: selectedLabel ? `${text.buffer15m} ${selectedLabel}` : text.buffer15m,
    buffer20m: selectedLabel ? `${text.buffer20m} ${selectedLabel}` : text.buffer20m,
    cumulativeBuffer10m: rangeLabel ? `${text.buffer10m} ${rangeLabel}` : text.buffer10m,
    cumulativeBuffer15m: rangeLabel ? `${text.buffer15m} ${rangeLabel}` : text.buffer15m,
    cumulativeBuffer20m: rangeLabel ? `${text.buffer20m} ${rangeLabel}` : text.buffer20m,
  };
}

export function usesGeneratedMilestoneColors(milestoneCount: number): boolean {
  return milestoneCount > TEMPORAL_MILESTONE_COLOR_PALETTE.length;
}

export function getTemporalLayerPaint(layerKind: TemporalStyledLayerKind, milestoneColor: string): TemporalLayerPaint {
  if (layerKind.startsWith("cumulativeBuffer")) {
    return {
      fillPaint: {
        "fill-color": milestoneColor,
        "fill-opacity": TEMPORAL_BUFFER_FILL_OPACITY,
        "fill-outline-color": TRANSPARENT_COLOR,
      },
      linePaint: {
        "line-color": milestoneColor,
        "line-opacity": 0,
        "line-width": 0,
      },
      fillOpacity: TEMPORAL_BUFFER_FILL_OPACITY,
      lineOpacity: 0,
    };
  }

  if (layerKind.startsWith("buffer")) {
    return {
      fillPaint: {
        "fill-color": milestoneColor,
        "fill-opacity": TEMPORAL_BUFFER_FILL_OPACITY,
        "fill-outline-color": TRANSPARENT_COLOR,
      },
      linePaint: {
        "line-color": milestoneColor,
        "line-opacity": 0,
        "line-width": 0,
      },
      fillOpacity: TEMPORAL_BUFFER_FILL_OPACITY,
      lineOpacity: 0,
    };
  }

  return {
    fillPaint: {
      "fill-color": milestoneColor,
      "fill-opacity": 0.88,
      "fill-outline-color": milestoneColor,
    },
    linePaint: {
      "line-color": milestoneColor,
      "line-opacity": 1,
      "line-width": 1,
    },
    fillOpacity: 0.88,
    lineOpacity: 1,
  };
}
